"""静音告知、请求生命周期、恢复串行调度及过期结果的离线验收。"""
import asyncio
from copy import deepcopy

import pytest
from pydantic import ValidationError

from backend import config
from backend.llm import ModelError
from backend.policy import apply_result, mark_delivered
from backend.schemas import Lesson, Preparation, ResponseRequest, ResumeResult, StudentResult, StudentState
from backend.session import Session
from test_session import until


SOURCES = {'t1': {'text': '请回答这个问题'}, 't2': {'text': '问题不用回答了'}, 'p1': {'text': '基础'}}


def request(rid='r1', source='t1', **extra):
    return {'id': rid, 'text': '教师请求' + rid, 'sources': [source], **extra}


def answer(ids=('r1',), source='t1', **extra):
    return {'kind': 'answer', 'text': '我还没学会，但在听。', 'sources': [source],
            'response_request_ids': list(ids), **extra}


def waiting(*ids):
    return StudentState(response_requests=[ResponseRequest(**request(rid)) for rid in ids])


class ScriptModel:
    def __init__(self, script):
        self.script = list(script)
        self.calls = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, phase, system, payload, output_type, *, history=(), result_validator=None):
        if phase == 'preparation':
            return Preparation(scope=['课堂主题'], boundary_note='范围不是知识')
        self.calls.append((phase, deepcopy(payload)))
        if not self.script:
            raise AssertionError('发生未计划的模型调用：' + phase)
        item = self.script.pop(0)
        if item == 'block':
            self.entered.set()
            await self.release.wait()
            item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        result = output_type.model_validate(item)
        return result_validator(result, True) if result_validator else result

    async def close(self):
        pass


async def start(tmp_path, monkeypatch, script, muted=True):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(config, 'DIRECT_PAUSE', .01)
    monkeypatch.setattr(config, 'SPEAK_PAUSE', .01)
    events = []
    async def emit(event):
        events.append(event)
    session = Session(emit, None)
    await session.llm.close()
    model = ScriptModel(script)
    session.llm = model
    await session.start(Lesson(topic='规则', student_profile='已有知识：知道整数加法。'),
                        muted=muted, tts=False, asr_review=False)
    return session, model, events


async def teach(session, text='请回答这个问题'):
    await session.add_transcript(text)
    await session.flush()


def resume_events(events, status):
    return [e for e in events if e['type'] == 'resume_response' and e['data']['status'] == status]


def replies(events):
    return [e for e in events if e['type'] == 'reply']


def test_request_only_actual_delivery_closes_and_advances_version():
    state, candidate = apply_result(StudentState(), StudentResult.model_validate({
        'response_request_updates': [request()], 'candidate': answer()}), SOURCES, {'t1'})
    assert state.response_requests[0].status == 'pending'
    before = state.version
    mark_delivered(state, candidate)
    assert state.response_requests[0].status == 'answered' and state.version == before + 1
    mark_delivered(state, candidate)
    assert state.version == before + 1


@pytest.mark.parametrize('updates', [
    [request(status='dismissed')],
    [request(sources=['p1'])],
    [request(sources=['missing'])],
    [request(), request()],
    [request('r1'), request('r2', text='教师请求r1')],
])
def test_invalid_request_is_atomic(updates):
    state = StudentState()
    before = state.model_dump()
    with pytest.raises(ValueError):
        apply_result(state, StudentResult.model_validate({'response_request_updates': updates}), SOURCES, {'t1'})
    assert state.model_dump() == before


@pytest.mark.parametrize('candidate', [answer(('missing',)), answer(('r1', 'r1')),
                                       answer(kind='question', question_id='q1')])
def test_invalid_reply_request_link(candidate):
    with pytest.raises(ValueError):
        apply_result(waiting('r1'), StudentResult.model_validate({'candidate': candidate}), SOURCES, {'t1'})


def test_dismissal_needs_evidence_and_cannot_resurrect():
    state, _ = apply_result(waiting('r1'), StudentResult.model_validate({
        'response_request_updates': [request(status='dismissed', reason='教师撤回', resolution_sources=['t2'])]}),
        SOURCES, {'t2'})
    assert state.response_requests[0].status == 'dismissed'
    with pytest.raises(ValueError):
        apply_result(state, StudentResult.model_validate({'response_request_updates': [request()]}), SOURCES, {'t1'})


def test_resume_cannot_modify_cognition_or_create_requests():
    with pytest.raises(ValidationError):
        ResumeResult.model_validate({'knowledge_updates': []})
    with pytest.raises(ValueError):
        apply_result(StudentState(), StudentResult.model_validate({'response_request_updates': [request()]}),
                     SOURCES, set(), resume=True)
    state = waiting('r1', 'r2')
    with pytest.raises(ValueError, match='合并'):
        apply_result(state, StudentResult.model_validate({'candidate': answer()}), SOURCES, set(), resume=True)
    next_state, _ = apply_result(state, StudentResult.model_validate({'candidate': answer(('r1', 'r2'))}),
                                SOURCES, set(), resume=True)
    for field in ('knowledge', 'conflicts', 'knowledge_history', 'pending_reviews', 'recent_events'):
        assert getattr(next_state, field) == getattr(state, field)


def test_muted_learning_then_fresh_merged_response_without_input(tmp_path, monkeypatch):
    async def run():
        knowledge = {'id': 'k1', 'text': '加法规则', 'status': 'understood', 'reason': '教师直接解释', 'sources': ['t1']}
        script = [{'knowledge_updates': [knowledge], 'response_request_updates': [request('r1'), request('r2')],
                   'candidate': answer(('r1', 'r2'), text='这条旧候选不该补发')},
                  {'candidate': answer(('r1', 'r2'), text='根据最新内容重新回应。')}]
        session, model, events = await start(tmp_path, monkeypatch, script)
        try:
            await teach(session)
            await until(lambda: session.state.version == 1)
            assert model.calls[0][1]['interaction_context'] == {'muted': True, 'trigger': 'teacher_input'}
            assert not replies(events) and session.candidate is None
            cognition = deepcopy(session.state.knowledge)
            await session.set_muted(False)
            await until(lambda: len(replies(events)) == 1)
            assert model.calls[1][0] == 'student_resume'
            assert model.calls[1][1]['interaction_context'] == {'muted': False, 'trigger': 'unmuted'}
            assert model.calls[1][1]['new_teacher_segments'] == []
            assert replies(events)[0]['data']['text'] == '根据最新内容重新回应。'
            assert all(r.status == 'answered' for r in session.state.response_requests)
            assert session.state.knowledge == cognition
            await session.set_muted(False)
            await session.set_muted(True)
            await session.set_muted(False)
            await until(lambda: resume_events(events, 'skipped'))
            assert len(model.calls) == 2 and len(replies(events)) == 1
        finally:
            await session.close()
    asyncio.run(run())


def test_no_pending_work_does_not_call_model(tmp_path, monkeypatch):
    async def run():
        session, model, events = await start(tmp_path, monkeypatch, [])
        try:
            await session.set_muted(False)
            await until(lambda: resume_events(events, 'skipped'))
            assert not model.calls and not replies(events)
        finally:
            await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('status,attempts,expected', [('pending', 0, True), ('asked', 1, True),
                                                    ('resolved', 0, False), ('deferred', 2, False)])
def test_resume_autonomous_question_limits(tmp_path, monkeypatch, status, attempts, expected):
    async def run():
        script = [{'candidate': {'kind': 'question', 'text': '这里为什么？', 'sources': ['t1'], 'question_id': 'q1'}}] if expected else []
        session, model, events = await start(tmp_path, monkeypatch, script)
        session.sources['t1'] = {'id': 't1', 'text': '课堂讲解', 'mode': 'text', 'revision': 1, 'at': 0.0}
        session.processed_ids.add('t1')
        session.state = StudentState.model_validate({'open_questions': [
            {'id': 'q1', 'topic': '原因', 'text': '这里为什么？', 'status': status, 'attempts': attempts, 'sources': ['t1']}]})
        try:
            await session.set_muted(False)
            await until(lambda: bool(replies(events)) if expected else bool(resume_events(events, 'skipped')))
            assert bool(model.calls) == expected
            assert session.state.questions[0].attempts == attempts + int(expected)
        finally:
            await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('change', ['new_input', 'mute', 'state', 'rapid_toggle'])
def test_inflight_resume_is_stale_and_never_speaks(tmp_path, monkeypatch, change):
    async def run():
        following = {'response_request_updates': [request(status='dismissed', reason='老师撤回', resolution_sources=['t2'])]}
        script = [{'response_request_updates': [request()]}, 'block', {'candidate': answer()}]
        if change == 'new_input':
            script.append(following)
        elif change in ('state', 'rapid_toggle'):
            script.append({'candidate': answer(text='新快照回应')})
        session, model, events = await start(tmp_path, monkeypatch, script)
        try:
            await teach(session)
            await until(lambda: session.state.version == 1)
            await session.set_muted(False)
            await model.entered.wait()
            if change == 'new_input':
                await teach(session, '这个问题不用回答了')
            elif change == 'mute':
                await session.set_muted(True)
            elif change == 'state':
                session.state.version += 1
            else:
                await session.set_muted(True)
                await session.set_muted(False)
                await session.set_muted(False)
            model.release.set()
            await until(lambda: resume_events(events, 'stale'))
            if change == 'new_input':
                await until(lambda: 't2' in session.processed_ids)
                await until(lambda: resume_events(events, 'skipped'))
                assert session.state.response_requests[0].status == 'dismissed'
            elif change in ('state', 'rapid_toggle'):
                await until(lambda: len(replies(events)) == 1)
                assert replies(events)[0]['data']['text'] == '新快照回应'
            else:
                assert session.state.response_requests[0].status == 'pending'
            assert all(e['data']['text'] != '我还没学会，但在听。' for e in replies(events))
        finally:
            await session.close()
    asyncio.run(run())


def test_mute_toggle_during_teaching_preserves_learning_and_resumes(tmp_path, monkeypatch):
    async def run():
        script = ['block', {'response_request_updates': [request()], 'candidate': answer(text='旧控制回复')},
                  {'candidate': answer(text='新控制回复')}]
        session, model, events = await start(tmp_path, monkeypatch, script, muted=False)
        try:
            await teach(session)
            await model.entered.wait()
            await session.set_muted(True)
            await session.set_muted(False)
            model.release.set()
            await until(lambda: len(replies(events)) == 1)
            assert session.processed_ids == {'t1'}
            assert replies(events)[0]['data']['text'] == '新控制回复'
            assert len(model.calls) == 2
        finally:
            await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('retry', ['manual', 'new_input'])
def test_resume_failure_retains_requests_and_classroom_continues(tmp_path, monkeypatch, retry):
    async def run():
        script = [{'response_request_updates': [request()]}, ModelError('模拟网络失败')]
        if retry == 'new_input':
            script.append({'candidate': answer(text='课堂继续回应')})
        else:
            script.append({'candidate': answer(text='重试后回应')})
        session, model, events = await start(tmp_path, monkeypatch, script)
        try:
            await teach(session)
            await until(lambda: session.state.version == 1)
            await session.set_muted(False)
            await until(lambda: session.resume_failed)
            assert session.state.response_requests[0].status == 'pending' and not session.failed_batch
            await asyncio.sleep(.35)
            assert len(model.calls) == 2
            if retry == 'manual':
                session.retry_knowledge_reviews()
            else:
                await teach(session, '请按刚才的内容回答')
            await until(lambda: len(replies(events)) == 1)
            assert session.state.response_requests[0].status == 'answered'
            assert not session.resume_failed and not session.failed_batch
        finally:
            await session.close()
    asyncio.run(run())


def test_resume_waits_for_teacher_and_queued_content(tmp_path, monkeypatch):
    async def run():
        session, model, events = await start(tmp_path, monkeypatch, [{'response_request_updates': [request()]}, {'candidate': answer()}])
        try:
            await teach(session)
            await until(lambda: session.state.version == 1)
            session.active = True
            await session.set_muted(False)
            await asyncio.sleep(.35)
            assert len(model.calls) == 1
            session.active = False
            await until(lambda: len(replies(events)) == 1)
        finally:
            await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('ending', ['finish', 'close'])
def test_end_cancels_resume_and_preserves_unanswered_request(tmp_path, monkeypatch, ending):
    async def run():
        session, model, events = await start(tmp_path, monkeypatch, [{'response_request_updates': [request()]}, 'block', {'candidate': answer()}])
        try:
            await teach(session)
            await until(lambda: session.state.version == 1)
            await session.set_muted(False)
            await model.entered.wait()
            await asyncio.wait_for(getattr(session, ending)(), 2)
            assert session.closed and not replies(events)
            assert session.state.response_requests[0].status == 'pending'
            if ending == 'finish':
                finished = next(e for e in events if e['type'] == 'finished')
                assert finished['data']['state']['response_requests'][0]['status'] == 'pending'
        finally:
            await session.close()
    asyncio.run(run())


def test_old_records_default_to_empty_requests():
    state = StudentState.model_validate({'schema_version': 2, 'questions': [], 'knowledge': []})
    assert state.response_requests == [] and StudentResult().response_request_updates == []
    assert ResumeResult().candidate.response_request_ids == []
