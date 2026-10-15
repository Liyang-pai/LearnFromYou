"""说明困惑可以发言，不可用知识作为确定前提时须修复并保留课堂进度。"""
import asyncio
from copy import deepcopy

import pytest

from backend.policy import QuestionReleaseError, RestrictedKnowledgeError, apply_result, invited, student_result_validator
from backend.schemas import Question, StudentResult, StudentState
from test_question_recovery import make_session
from test_session import until


SOURCES = {'t1': {}, 't2': {}, 't3': {}}
TEXT = '老师，我在听。前面说规则是加3，现在说加5，我不知道该用哪一个。'


def conflict_state():
    return StudentState.model_validate({'version': 2, 'knowledge': [
        {'id': 'a', 'text': '规则是加3', 'status': 'conflict', 'sources': ['t1', 't2'], 'reason': '加3与加5不能同时成立'},
        {'id': 'b', 'text': '规则是加5', 'status': 'conflict', 'sources': ['t1', 't2'], 'reason': '加3与加5不能同时成立'}],
        'conflicts': [{'id': 'c1', 'left_id': 'a', 'right_id': 'b', 'conditions': '同一输入',
                       'reason': '加3与加5不能同时成立', 'sources': ['t1', 't2']}]})


def reply(knowledge_ids, kind='answer'):
    return {'addressed': True, 'candidate': {'kind': kind, 'text': TEXT,
            'sources': ['t1', 't2', 't3'], 'knowledge_ids': knowledge_ids,
            'question_id': 'q1' if kind == 'question' else None}}


@pytest.mark.parametrize('status', ['unclear', 'conflict', 'invalid', 'pending_review'])
def test_unusable_premise_requests_repair_without_changing_state(status):
    state = conflict_state() if status == 'conflict' else StudentState.model_validate({
        'knowledge': [{'id': 'a', 'text': '规则', 'status': 'understood' if status == 'pending_review' else status,
                       'sources': ['t1'], 'reason': '依据'}],
        'pending_reviews': [{'knowledge_id': 'a', 'revision': 1, 'reason': '前提变化'}]
                           if status == 'pending_review' else []})
    before = state.model_dump()
    result = StudentResult.model_validate(reply(['a']))
    recovery = []
    validate = student_result_validator(state, SOURCES, {'t3'}, recovery)
    with pytest.raises(RestrictedKnowledgeError, match='描述冲突'):
        validate(result, False)
    assert state.model_dump() == before and not recovery
    assert result.candidate.kind == 'answer'
    repaired = StudentResult.model_validate(reply([]))
    _, candidate = apply_result(state, validate(repaired, True), SOURCES, {'t3'})
    assert candidate.kind == 'answer' and candidate.text == TEXT


@pytest.mark.parametrize('kind', ['answer', 'question'])
def test_model_repairs_conflict_description_and_reply_is_delivered(tmp_path, monkeypatch, kind):
    invalid = reply(['a', 'b'], kind)
    # 同时复现原日志中追问漏登记，修复必须补齐两种关联。
    repaired = reply([], kind)
    if kind == 'question':
        repaired['question_updates'] = [{'id': 'q1', 'topic': '规则冲突', 'text': TEXT,
                                        'status': 'pending', 'sources': ['t1', 't2', 't3']}]

    async def run():
        session, events, requests = await make_session(tmp_path, monkeypatch, [invalid, repaired])
        session.state = conflict_state()
        before = session.state.model_dump()
        try:
            await session.add_transcript('说句话吧'); await session.flush()
            await until(lambda: session.state.version == 3)
            assert len(requests) == 2
            correction = requests[1]['messages'][-1]['content']
            assert ('knowledge_ids' if kind == 'answer' else 'question_updates') in correction
            assert session.state.conflicts[0].status == 'open'
            assert session.state.knowledge == StudentState.model_validate(before).knowledge
            await session.try_speak(10)
            delivered = [e['data'] for e in events if e['type'] == 'reply']
            assert len(delivered) == 1 and delivered[0]['text'] == TEXT
            assert delivered[0]['knowledge_ids'] == []
            if kind == 'question':
                assert session.state.questions[0].attempts == 1
            assert len([e for e in events if e['type'] == 'state']) == 1
            assert not [e for e in events if e['type'] in ('error', 'student_recovery')]
        finally:
            await session.close()
    asyncio.run(run())


def test_unrepaired_reply_reports_reason_and_next_batch_continues(tmp_path, monkeypatch):
    invalid = reply(['a', 'b'])
    invalid['event_updates'] = [{'id': 'e3', 'kind': 'request', 'text': '要求学生回应', 'sources': ['t3']}]
    following = {'candidate': {'kind': 'silence'}}

    async def run():
        session, events, requests = await make_session(tmp_path, monkeypatch, [invalid, invalid, following])
        session.state = conflict_state()
        try:
            await session.add_transcript('说句话吧'); await session.flush()
            await until(lambda: session.state.version == 3)
            await session.try_speak(10)
            assert len(requests) == 2 and session.state.recent_events[0].id == 'e3'
            recovery = [e['data'] for e in events if e['type'] == 'student_recovery']
            assert len(recovery) == 1 and recovery[0]['code'] == 'restricted_knowledge_reference'
            assert recovery[0]['knowledge_ids'] == ['a', 'b']
            assert not [e for e in events if e['type'] in ('reply', 'error')]
            await session.add_transcript('继续讲课'); await session.flush()
            await until(lambda: session.state.version == 4)
            assert session.failed_batch is None and len(requests) == 3
            assert session.processed_ids == {'t1', 't2', 't3', 't4'}
        finally:
            await session.close()
    asyncio.run(run())


def test_final_reply_recovery_does_not_hide_invalid_knowledge_update():
    state = conflict_state(); before = state.model_dump()
    invalid = deepcopy(reply(['a', 'b']))
    invalid['knowledge_updates'] = [{'id': 'new', 'text': '未讲知识', 'status': 'understood',
                                    'reason': '无效来源', 'sources': ['unknown']}]
    recovery = []
    with pytest.raises(ValueError, match='不存在'):
        student_result_validator(state, SOURCES, {'t3'}, recovery)(StudentResult.model_validate(invalid), True)
    assert state.model_dump() == before and not recovery


def deferred_state():
    state = conflict_state()
    state.questions = [Question(id='q1', topic='规则冲突', text='到底是加3还是加5？',
                                status='deferred', sources=['t1', 't2'], attempts=2)]
    return state


def test_deferred_question_requires_application_even_when_addressed():
    state = deferred_state()
    for addressed in (True, False):
        result = StudentResult.model_validate(reply([], 'question'))
        result.addressed = addressed
        with pytest.raises(QuestionReleaseError, match='缺少例外申请'):
            apply_result(state, result, SOURCES, {'t3'})
    _, candidate = apply_result(state, StudentResult.model_validate(reply([], 'ack')), SOURCES, {'t3'})
    assert candidate.kind == 'ack' and state.questions[0].attempts == 2


@pytest.mark.parametrize('repaired', [True, False])
def test_invitation_repairs_application_or_skips_without_ack(tmp_path, monkeypatch, repaired):
    invalid = reply([], 'question')
    second = deepcopy(invalid)
    if repaired:
        second['response_request_updates'] = [{'id': 'r1', 'text': '还有问题吗', 'sources': ['t3'],
            'kind': 'invite_questions', 'invitation_source_id': 't3', 'invitation_quote': '还有问题吗'}]
        second['candidate']['response_request_ids'] = ['r1']
        second['candidate']['question_release_request'] = {'question_id': 'q1', 'response_request_id': 'r1', 'reason': '教师明确邀请'}

    async def run():
        session, events, requests = await make_session(tmp_path, monkeypatch, [invalid, second])
        session.state = deferred_state()
        try:
            await session.add_transcript('还有问题吗'); await session.flush()
            await until(lambda: bool([e for e in events if e['type'] == 'state']))
            await session.try_speak(10)
            assert len(requests) == 2
            assert 'question_release_request' in requests[1]['messages'][-1]['content']
            delivered = [e['data'] for e in events if e['type'] == 'reply']
            assert len(delivered) == int(repaired)
            if repaired:
                assert delivered[0]['kind'] == 'question'
            assert session.state.questions[0].attempts == (3 if repaired else 2)
            assert session.state.questions[0].status == 'deferred'
            assert not [e for e in events if e['type'] == 'error']
            if not repaired:
                assert any(e['type'] == 'student_recovery' and e['data']['code'] == 'question_release_denied' for e in events)
        finally:
            await session.close()
    asyncio.run(run())


def test_deferred_old_question_does_not_block_new_question():
    state = deferred_state()
    result = StudentResult.model_validate({'question_updates': [
        {'id': 'q2', 'topic': '符号含义', 'text': '这个符号表示什么？', 'status': 'pending', 'sources': ['t3']}],
        'candidate': {'kind': 'question', 'text': '这个符号表示什么？', 'question_id': 'q2', 'sources': ['t3']}})
    after, candidate = apply_result(state, result, SOURCES, {'t3'})
    assert candidate.kind == 'question' and candidate.question_id == 'q2'
    assert after.questions[0].status == 'deferred' and after.questions[1].attempts == 0


def test_direct_invitation_recovery_does_not_hide_invalid_update():
    invalid = reply([], 'question')
    invalid['knowledge_updates'] = [{'id': 'new', 'text': '不存在来源的知识', 'status': 'understood',
                                    'reason': '无效', 'sources': ['unknown']}]
    recovery = []
    with pytest.raises(ValueError, match='不存在'):
        student_result_validator(deferred_state(), SOURCES, {'t3'}, recovery)(
            StudentResult.model_validate(invalid), True)
    assert not recovery
