"""候选关联兼容：原始失败单次回放、严格边界和真实 Session 提交/送达。"""
import asyncio
import json
from pathlib import Path

import httpx
import pytest

from backend import config
from backend.llm import ModelClient
from backend.policy import (QuestionReleaseError, RestrictedKnowledgeError, apply_result,
                            mark_delivered, student_result_validator)
from backend.schemas import (Candidate, Knowledge, KnowledgeReview, Question, ResponseRequest,
                             ResumeResult, StudentResult, StudentState)
from test_mute_resume import start, teach, replies, request, answer
from test_session import until

CASES = json.loads((Path(__file__).parent / 'fixtures/candidate_reference_failures.json').read_text(encoding='utf-8'))['cases']
SOURCES = {'p1': {}, 't1': {'revision': 1}, 't2': {'revision': 2}}


def knowledge(status='understood', sources=None):
    return Knowledge(id='k1', text='规则', status=status, sources=sources or ['p1', 't1'],
                     reason='教师解释，已有基础支持推断')


def candidate(**changes):
    return Candidate.model_validate(dict(kind='answer', text='按规则计算。', sources=['k1', 't2'],
                                        knowledge_ids=['k1'], **changes))


@pytest.mark.parametrize('case', CASES, ids=lambda case: case['id'])
def test_original_failure_first_attempt_preserves_raw_and_learning(monkeypatch, case):
    async def run():
        monkeypatch.setattr(config, 'API_KEY', 'offline')
        state = StudentState.model_validate(case['state'])
        raw = json.dumps(case['result'], ensure_ascii=False)
        original_state = state.model_dump()
        events, requests, recovery = [], [], []
        async def emit(kind, data): events.append((kind, data))
        def respond(req):
            requests.append(req)
            return httpx.Response(200, json={'choices': [{'message': {'content': raw}}]})
        model = ModelClient(emit)
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            result = await model.generate('student', 'offline replay', {}, StudentResult,
                result_validator=student_result_validator(state, case['sources'], set(case['current_ids']),
                    recovery, input_revision=case['input_revision']))
        finally:
            await model.close()
        assert len(requests) == 1
        assert not [data for kind, data in events if kind == 'student_validation_error']
        assert next(data['raw'] for kind, data in events if kind == 'llm_output') == raw
        assert state.model_dump() == original_state
        original = StudentResult.model_validate(case['result'])
        for field in StudentResult.model_fields:
            if field != 'candidate':
                assert getattr(result, field) == getattr(original, field)
        assert result.candidate.text == original.candidate.text
        assert result.candidate.knowledge_ids == original.candidate.knowledge_ids
        assert result.candidate.response_request_ids == original.candidate.response_request_ids
        assert len(recovery) == 1 and recovery[0]['code'] == 'candidate_normalized'
        assert recovery[0]['attempt'] == 1
        next_state, committed = apply_result(state, result, case['sources'], set(case['current_ids']),
                                             input_revision=case['input_revision'])
        assert committed == result.candidate and committed.kind != 'silence'
        assert next_state.version > state.version
        # Second validation must not generate another repair log or change content.
        again = student_result_validator(state, case['sources'], set(case['current_ids']), recovery,
                                        input_revision=case['input_revision'])(result, False)
        assert again == result and len(recovery) == 1
    asyncio.run(run())


@pytest.mark.parametrize('new', [False, True])
@pytest.mark.parametrize('status', ['understood', 'tentative'])
def test_expands_current_and_new_knowledge_deduplicates_and_does_not_mutate(new, status):
    k = knowledge(status)
    state = StudentState(knowledge=[] if new else [k])
    result = StudentResult(candidate=candidate())
    result.candidate.sources = ['t2', 'k1', 'p1', 'k1']
    if new: result.knowledge_updates = [k]
    before = result.model_dump()
    records = []
    updated, reply = apply_result(state, result, SOURCES, {'t1', 't2'}, normalizations=records)
    assert reply.sources == ['t2', 'p1', 't1']
    assert result.model_dump() == before and state.version == 0
    assert updated.knowledge[0].id == 'k1' and len(records) == 1
    _, same = apply_result(state, result.model_copy(update={'candidate': reply}), SOURCES, {'t1', 't2'})
    assert same == reply


@pytest.mark.parametrize('refs,adopted', [(['k1'], []), (['missing'], ['k1']), (['k2'], ['k2']), ([], ['k1'])])
def test_does_not_guess_missing_adoption_or_unknown_sources(refs, adopted):
    state = StudentState(knowledge=[knowledge()])
    proposed = StudentResult(candidate=Candidate(kind='answer', text='答案', sources=refs, knowledge_ids=adopted))
    records = []
    with pytest.raises(ValueError):
        apply_result(state, proposed, SOURCES, {'t2'}, normalizations=records)
    assert state.version == 0 and not records


def test_missing_underlying_source_and_expansion_over_limit_rejected():
    for refs in (['missing'], [f't{i}' for i in range(13)]):
        sources = {f't{i}': {} for i in range(13)} if len(refs) > 1 else SOURCES
        # A valid historical knowledge can expand to 12 sources plus one explicit source.
        k = knowledge(sources=refs[:12])
        proposed = StudentResult(candidate=Candidate(kind='answer', text='答案', sources=['k1', 't12'], knowledge_ids=['k1']))
        with pytest.raises(ValueError):
            apply_result(StudentState(knowledge=[k]), proposed, sources, set())


@pytest.mark.parametrize('status,review', [('unclear', False), ('conflict', False), ('invalid', False), ('understood', True)])
def test_unusable_knowledge_still_rejected_atomically(status, review):
    state = StudentState(knowledge=[knowledge(status)], pending_reviews=[
        KnowledgeReview(knowledge_id='k1', revision=1, reason='待复核')] if review else [])
    before = state.model_dump()
    recovery = []
    validate = student_result_validator(state, SOURCES, {'t2'}, recovery)
    with pytest.raises(RestrictedKnowledgeError):
        validate(StudentResult(candidate=candidate()), False)
    assert state.model_dump() == before and not recovery


@pytest.mark.parametrize('field,update', [
    ('knowledge_updates', {'id': 'k2', 'text': '新知识', 'status': 'understood', 'sources': ['k1']}),
    ('question_updates', {'id': 'q1', 'topic': '规则', 'text': '为何', 'status': 'pending', 'sources': ['k1']}),
    ('response_request_updates', {'id': 'r1', 'text': '请求', 'sources': ['k1']}),
    ('event_updates', {'id': 'e1', 'kind': 'task', 'text': '任务', 'sources': ['k1']}),
])
def test_only_candidate_sources_are_compatible(field, update):
    state = StudentState(knowledge=[knowledge()])
    with pytest.raises(ValueError, match='来源'):
        apply_result(state, StudentResult.model_validate({field: [update]}), SOURCES, {'t2'})
    assert state.version == 0


@pytest.mark.parametrize('kind', ['answer', 'ack'])
@pytest.mark.parametrize('status,attempts', [('pending', 0), ('asked', 1), ('deferred', 2), ('resolved', 1)])
def test_answer_retains_obstacle_request_and_never_counts_question(kind, status, attempts):
    q = Question(id='q1', topic='规则含义', text='为什么这样？', status=status,
                 attempts=attempts, sources=['t1'], resolution_sources=['t2'] if status == 'resolved' else [])
    state = StudentState(questions=[q], response_requests=[ResponseRequest(**request())])
    result = StudentResult.model_validate({'candidate': answer(kind=kind, question_id='q1'),
        'question_updates': [{key: value for key, value in q.model_dump().items() if key != 'attempts'}]})
    updated, reply = apply_result(state, result, SOURCES, {'t2'})
    assert reply.question_id is None and reply.text == result.candidate.text
    assert updated.questions[0] == q
    assert updated.response_requests[0].status == 'pending'
    mark_delivered(updated, reply)
    assert updated.questions[0] == q and updated.response_requests[0].status == 'answered'


@pytest.mark.parametrize('kind', ['answer', 'ack', 'silence'])
def test_non_question_cannot_carry_release(kind):
    proposed = StudentResult(candidate=Candidate(kind=kind, text='这里不懂', question_id='q1', sources=['t1'],
        question_release_request={'question_id': 'q1', 'response_request_id': 'r1', 'reason': '请求例外'}))
    with pytest.raises(QuestionReleaseError):
        apply_result(StudentState(), proposed, SOURCES, {'t1'})


def test_resume_keeps_type_and_pending_request_requirement():
    state = StudentState(response_requests=[ResponseRequest(**request())])
    recovery = []
    validate = student_result_validator(state, SOURCES, set(), recovery, resume=True)
    normalized = validate(ResumeResult(candidate=Candidate(**answer(question_id='missing'))), False)
    assert isinstance(normalized, ResumeResult) and normalized.candidate.question_id is None
    assert recovery[0]['code'] == 'candidate_normalized'
    # A stray question ID must not grant permission to skip a pending request.
    with pytest.raises(QuestionReleaseError):
        validate(ResumeResult(candidate=Candidate(kind='answer', text='收到', sources=['t1'], question_id='q1')), False)


@pytest.mark.parametrize('muted,cancel', [(False, False), (True, False), (False, True), (True, True)])
def test_session_commits_canonical_fields_and_counts_only_delivery(tmp_path, monkeypatch, muted, cancel):
    async def run():
        from backend import session as session_module
        original_apply = session_module.apply_result
        resume_permissions = []
        def inspect_apply(*args, **kwargs):
            if kwargs.get('resume'):
                resume_permissions.append(kwargs.get('allow_silence', False))
            return original_apply(*args, **kwargs)
        monkeypatch.setattr(session_module, 'apply_result', inspect_apply)
        k = knowledge(sources=['t1'])
        q = {'id': 'q1', 'topic': '规则含义', 'text': '这里为什么', 'status': 'pending', 'sources': ['t1']}
        proposed = answer(question_id='q1', sources=['k1'], knowledge_ids=['k1'])
        script = [{'knowledge_updates': [k.model_dump()], 'question_updates': [q],
                   'response_request_updates': [request()], 'candidate': proposed}]
        if muted: script.append({'candidate': proposed})
        session, model, events = await start(tmp_path, monkeypatch, script, muted=muted)
        try:
            await teach(session)
            await until(lambda: session.state.version == 1)
            if muted:
                assert session.candidate is None and not replies(events)
                await session.set_muted(False)
                await until(lambda: session.candidate is not None or bool(replies(events)))
            assert session.state.questions[0].attempts == 0
            if cancel:
                await session.cancel_candidate('测试取消')
                await session.try_speak(10)
                assert not replies(events) and session.state.response_requests[0].status == 'pending'
            else:
                await session.try_speak(10)
                await until(lambda: bool(replies(events)))
                delivered = replies(events)[0]['data']
                assert delivered['question_id'] is None and delivered['sources'] == ['t1']
                assert delivered['text'] == proposed['text']
                assert session.state.response_requests[0].status == 'answered'
            assert session.state.questions[0].attempts == 0
            assert session.state.knowledge[0].id == 'k1'
            assert len(model.calls) == 1 + int(muted)
            assert resume_permissions == ([False] if muted else [])
            assert [e for e in events if e['type'] == 'student_recovery'][0]['data']['code'] == 'candidate_normalized'
        finally:
            await session.close()
    asyncio.run(run())


def test_normalized_inflight_candidate_is_stale_but_learning_is_committed(tmp_path, monkeypatch):
    async def run():
        proposed = answer(question_id='q_unknown', sources=['k1'], knowledge_ids=['k1'])
        session, model, events = await start(tmp_path, monkeypatch, ['block', {
            'knowledge_updates': [knowledge(sources=['t1']).model_dump()],
            'response_request_updates': [request()], 'candidate': proposed}], muted=False)
        try:
            await teach(session)
            await model.entered.wait()
            await session.add_transcript('稍等，我再补充。')
            model.release.set()
            await until(lambda: session.state.version == 1)
            await session.try_speak(10)
            assert not replies(events)
            assert session.state.knowledge[0].id == 'k1'
            assert session.state.response_requests[0].status == 'pending'
            assert not session.state.questions and len(model.calls) == 1
        finally:
            await session.close()
    asyncio.run(run())
