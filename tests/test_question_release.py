"""One current teacher invitation permits one delivered exception, never a type rename."""
import asyncio
from copy import deepcopy

import pytest

from backend.policy import (QuestionReleaseError, apply_result, expire_invitations,
                            mark_delivered, release_error, student_result_validator)
from backend.schemas import Candidate, Question, ResponseRequest, StudentResult, StudentState
from backend.runtime import RUNTIME
from test_mute_resume import start, teach, replies
from test_session import until


SOURCES = {'t1': {'text': '原来的问题', 'revision': 1},
           't2': {'text': '解释尚未理解', 'revision': 2},
           't3': {'text': '还有什么疑问吗？', 'revision': 3},
           't4': {'text': '继续讲新的内容', 'revision': 4}}


def state():
    return StudentState(questions=[Question(id='q1', topic='规则原因', text='规则为什么成立？',
        status='deferred', sources=['t1', 't2'], attempts=2)])


def invitation(sid='t3', rid='r1'):
    return {'id': rid, 'text': '邀请说明疑问', 'kind': 'invite_questions', 'sources': [sid],
            'invitation_source_id': sid, 'invitation_quote': SOURCES.get(sid, SOURCES['t3'])['text']}


def candidate(rid='r1'):
    return {'kind': 'question', 'text': '规则为什么成立？', 'question_id': 'q1', 'sources': ['t3'],
            'response_request_ids': [rid], 'question_release_request': {
                'question_id': 'q1', 'response_request_id': rid, 'reason': '教师明确邀请说明疑问'}}


def proposed():
    return {'response_request_updates': [invitation()], 'candidate': candidate()}


def test_approval_is_atomic_and_only_delivery_consumes():
    before = state()
    after, c = apply_result(before, StudentResult.model_validate(proposed()), SOURCES, {'t3'}, input_revision=3)
    assert before.response_requests == [] and before.questions[0].attempts == 2
    assert after.questions[0].attempts == 2 and after.response_requests[0].status == 'pending'
    assert release_error(after, c, 3) is None
    mark_delivered(after, c)
    assert after.questions[0].attempts == 3 and after.questions[0].status == 'deferred'
    assert after.response_requests[0].status == 'answered'
    assert release_error(after, c, 3)
    with pytest.raises(QuestionReleaseError):
        apply_result(after, StudentResult(candidate=c), SOURCES, {'t3'}, input_revision=3)


@pytest.mark.parametrize('fault', ['missing', 'wrong_question', 'wrong_request', 'resolved', 'not_deferred',
                                  'generic', 'old_source', 'false_quote', 'no_quote', 'no_source',
                                  'no_link', 'answer_disguise'])
def test_denials_do_not_change_state(fault):
    original = state(); data = proposed()
    if fault == 'missing': data['candidate'].pop('question_release_request')
    elif fault == 'wrong_question': data['candidate']['question_release_request']['question_id'] = 'other'
    elif fault == 'wrong_request': data['candidate']['question_release_request']['response_request_id'] = 'other'
    elif fault == 'resolved': original.questions[0].status = 'resolved'
    elif fault == 'not_deferred': original.questions[0].status = 'asked'; original.questions[0].attempts = 1
    elif fault == 'generic': data['response_request_updates'][0]['kind'] = 'check_understanding'
    elif fault == 'old_source': data['response_request_updates'] = [invitation('t1')]
    elif fault == 'false_quote': data['response_request_updates'][0]['invitation_quote'] = '教师没有说过'
    elif fault == 'no_quote': data['response_request_updates'][0]['invitation_quote'] = ''
    elif fault == 'no_source': data['candidate']['sources'] = ['t1']
    elif fault == 'no_link': data['candidate']['response_request_ids'] = []
    elif fault == 'answer_disguise': data['candidate']['kind'] = 'answer'
    snapshot = original.model_dump()
    with pytest.raises(QuestionReleaseError):
        apply_result(original, StudentResult.model_validate(data), SOURCES, {'t3'}, input_revision=3)
    assert original.model_dump() == snapshot


def test_same_source_cannot_be_reissued_with_another_id_or_question():
    after, c = apply_result(state(), StudentResult.model_validate(proposed()), SOURCES, {'t3'}, input_revision=3)
    mark_delivered(after, c)
    data = proposed(); data['response_request_updates'] = [invitation(rid='r2')]; data['candidate'] = candidate('r2')
    with pytest.raises(QuestionReleaseError, match='同一教师来源'):
        apply_result(after, StudentResult.model_validate(data), SOURCES, {'t3'}, input_revision=3)
    after.questions.append(Question(id='q2', topic='另一个问题', text='另一个原因？', status='deferred', attempts=2, sources=['t1']))
    data = {'candidate': candidate()}
    data['candidate']['question_id'] = data['candidate']['question_release_request']['question_id'] = 'q2'
    with pytest.raises(QuestionReleaseError):
        apply_result(after, StudentResult.model_validate(data), SOURCES, {'t3'}, input_revision=3)


def test_new_invitation_allows_new_exception_without_reset():
    after, c = apply_result(state(), StudentResult.model_validate(proposed()), SOURCES, {'t3'}, input_revision=3)
    mark_delivered(after, c)
    data = proposed(); data['response_request_updates'] = [invitation('t4', 'r2')]; data['candidate'] = candidate('r2')
    data['candidate']['sources'] = ['t4']
    newer, c = apply_result(after, StudentResult.model_validate(data), SOURCES, {'t4'}, input_revision=4)
    mark_delivered(newer, c)
    assert newer.questions[0].attempts == 4 and newer.questions[0].status == 'deferred'


def test_normal_question_can_respond_without_exception():
    original = state(); original.questions[0].status = 'pending'; original.questions[0].attempts = 0
    data = proposed(); data['candidate'].pop('question_release_request')
    after, c = apply_result(original, StudentResult.model_validate(data), SOURCES, {'t3'}, input_revision=3)
    mark_delivered(after, c)
    assert after.questions[0].attempts == 1 and after.response_requests[0].status == 'answered'


def test_expiration_only_dismisses_invitation():
    after, c = apply_result(state(), StudentResult.model_validate(proposed()), SOURCES, {'t3'}, input_revision=3)
    after.response_requests.append(ResponseRequest(id='ordinary', text='回答题目', sources=['t1']))
    assert expire_invitations(after, 4) == ['r1']
    assert after.response_requests[1].status == 'pending'
    assert after.questions[0].attempts == 2 and release_error(after, c, 4)


def test_invalid_invitation_downgrade_retains_learning_but_not_bad_records():
    data = proposed(); data['response_request_updates'][0]['invitation_quote'] = '虚构'
    data['knowledge_updates'] = [{'id': 'k1', 'text': '新知识', 'status': 'understood', 'reason': '直接讲授', 'sources': ['t3']}]
    recoveries = []
    cleaned = student_result_validator(state(), SOURCES, {'t3'}, recoveries, input_revision=3)(StudentResult.model_validate(data), True)
    after, c = apply_result(state(), cleaned, SOURCES, {'t3'}, input_revision=3)
    assert c.kind == 'silence' and after.knowledge[0].id == 'k1' and not after.response_requests
    assert recoveries[0]['code'] == 'question_release_denied'
    data['knowledge_updates'][0]['sources'] = ['missing']
    with pytest.raises(ValueError):
        student_result_validator(state(), SOURCES, {'t3'}, [], input_revision=3)(StudentResult.model_validate(data), True)


def test_old_request_defaults_do_not_authorize_exception():
    old = ResponseRequest(id='old', text='还有问题吗', sources=['t3'])
    assert old.kind == 'answer'


def test_runtime_identity_is_fixed_and_covers_current_rules():
    assert len(RUNTIME['rules_fingerprint']) == 64 and 'question_release_v1' in RUNTIME['capabilities']
    assert RUNTIME['pid'] and RUNTIME['code_root']


@pytest.mark.parametrize('change', ['new_input', 'mute', 'knowledge', 'resolved'])
def test_waiting_candidate_cancelled_without_consumption(tmp_path, monkeypatch, change):
    async def run():
        session, model, events = await start(tmp_path, monkeypatch, [], muted=False)
        try:
            session.sources = deepcopy(SOURCES); session.revision = 3
            session.state, c = apply_result(state(), StudentResult.model_validate(proposed()), SOURCES, {'t3'}, input_revision=3)
            session.candidate = {'reply': c, 'revision': 3, 'addressed': True, 'control_revision': 0}
            if change == 'new_input': await session.add_transcript('新的讲解')
            elif change == 'mute': await session.set_muted(True)
            elif change == 'resolved': session.state.questions[0].status = 'resolved'
            else: c.knowledge_ids = ['missing']
            await session.try_speak(10)
            assert not replies(events) and session.state.questions[0].attempts == 2
            assert session.state.response_requests[0].status != 'answered'
            assert any(e['type'] == 'question_release' and e['data']['status'] == 'cancelled' for e in events)
        finally: await session.close()
    asyncio.run(run())


def test_muted_invitation_is_freshly_applied_after_unmute(tmp_path, monkeypatch):
    async def run():
        invite = invitation('t1'); invite['invitation_quote'] = '还有什么疑问吗？'
        reply = candidate(); reply['sources'] = ['t1']
        session, model, events = await start(tmp_path, monkeypatch,
            [{'response_request_updates': [invite]}, {'candidate': reply}])
        session.state = state()
        try:
            await teach(session, '还有什么疑问吗？')
            await until(lambda: any(e['type'] == 'state' for e in events))
            assert not replies(events) and session.state.questions[0].attempts == 2
            await session.set_muted(False)
            await until(lambda: bool(replies(events)))
            assert replies(events)[0]['data']['kind'] == 'question'
            assert session.state.questions[0].attempts == 3
            assert [x[0] for x in model.calls] == ['student', 'student_resume']
            statuses = [e['data']['status'] for e in events if e['type'] == 'question_release']
            assert 'approved' in statuses and 'delivered' in statuses
        finally: await session.close()
    asyncio.run(run())


def test_silence_on_current_response_request_gets_one_repair():
    data = {'response_request_updates': [{'id': 'r1', 'kind': 'check_audio', 'text': '听不到你说话', 'sources': ['t3']} ]}
    validate = student_result_validator(state(), SOURCES, {'t3'}, [], input_revision=3, require_response=True)
    with pytest.raises(ValueError, match='不能因没有新知识'):
        validate(StudentResult.model_validate(data), False)
    data['candidate'] = {'kind': 'ack', 'text': '老师，我在听。', 'sources': ['t3'], 'response_request_ids': ['r1']}
    assert validate(StudentResult.model_validate(data), True).candidate.kind == 'ack'


def test_model_cannot_anticipate_deferred_before_delivery():
    original = state(); original.questions[0].attempts = 1; original.questions[0].status = 'asked'
    data = proposed()
    data['question_updates'] = [{'id': 'q1', 'topic': '规则原因', 'text': '规则为什么成立？', 'status': 'deferred', 'sources': ['t3']}]
    with pytest.raises(QuestionReleaseError, match='不需要例外申请'):
        apply_result(original, StudentResult.model_validate(data), SOURCES, {'t3'}, input_revision=3)
    data['candidate'].pop('question_release_request')
    after, c = apply_result(original, StudentResult.model_validate(data), SOURCES, {'t3'}, input_revision=3)
    assert after.questions[0].status == 'asked' and after.questions[0].attempts == 1
    mark_delivered(after, c)
    assert after.questions[0].status == 'deferred' and after.questions[0].attempts == 2


def test_delivery_retry_cannot_count_exception_twice():
    after, c = apply_result(state(), StudentResult.model_validate(proposed()), SOURCES, {'t3'}, input_revision=3)
    mark_delivered(after, c); version = after.version
    mark_delivered(after, c)
    assert after.questions[0].attempts == 3 and after.version == version


def test_new_input_during_model_call_expires_uncommitted_invitation(tmp_path, monkeypatch):
    async def run():
        data = proposed(); data['response_request_updates'] = [invitation('t1')]
        data['response_request_updates'][0]['invitation_quote'] = '还有什么疑问吗？'
        data['candidate']['sources'] = ['t1']
        session, model, events = await start(tmp_path, monkeypatch, ['block', data, {}], muted=False)
        session.state = state()
        try:
            await teach(session, '还有什么疑问吗？')
            await model.entered.wait()
            await teach(session, '继续讲新的内容。')
            model.release.set()
            await until(lambda: 't2' in session.processed_ids)
            await session.try_speak(10)
            assert not replies(events) and session.state.questions[0].attempts == 2
            assert session.state.response_requests[0].status == 'dismissed'
        finally: await session.close()
    asyncio.run(run())
