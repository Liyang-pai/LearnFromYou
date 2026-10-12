"""离线验证追问漏登记的定向修复、限定降级、共享预算和课堂队列连续性。"""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from backend import config, prompts
from backend.llm import ModelError
from backend.policy import MissingQuestionError, apply_result, student_result_validator
from backend.schemas import Lesson, StudentResult, StudentState, Question
from backend.session import Session
from test_session import until


def result(question_id='q2', source='t3'):
    # 固定复现：此前 q1 已解决，这轮准备问 q2，却未登记它。
    return {'knowledge_updates': [{'id': 'k_new', 'text': '去括号后得到2x+6=4x-6。',
            'status': 'tentative', 'sources': [source]}], 'question_updates': [],
            'event_updates': [{'id': 'e_new', 'kind': 'task', 'text': '教师示范第二道方程。', 'sources': [source]}],
            'candidate': {'kind': 'question', 'question_id': question_id,
                          'text': '为什么负6移到另一边变成正6？', 'sources': [source]}}


def previous_state():
    return StudentState(version=2, open_questions=[Question(id='q1', topic='移项依据',
        text='移项是不是等式两边同时减去5？', status='resolved', sources=['t1'],
        resolution_sources=['t2'], attempts=1)])


def test_missing_reference_is_distinct_and_preview_does_not_commit():
    state = previous_state(); before = state.model_dump()
    invalid = StudentResult.model_validate(result())
    with pytest.raises(MissingQuestionError) as exc:
        apply_result(state, invalid, {'t1': {}, 't2': {}, 't3': {}}, {'t3'})
    assert exc.value.question_id == 'q2'
    assert 'question_updates' in str(exc.value)
    recovery = []
    validate = student_result_validator(state, {'t1': {}, 't2': {}, 't3': {}}, {'t3'}, recovery)
    cleaned = validate(invalid, True)
    assert state.model_dump() == before
    assert invalid.candidate.kind == 'question'
    assert cleaned.candidate.kind == 'silence'
    assert recovery[0]['question_id'] == 'q2'


async def make_session(tmp_path, monkeypatch, outputs, before_response=None):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(config, 'API_KEY', 'offline-test-key')
    events, requests = [], []
    async def send(event): events.append(event)
    session = Session(send, None)
    await session.llm.http.aclose()
    async def respond(request):
        body = json.loads(request.content)
        if body['messages'][0]['content'].startswith(prompts.PREPARE):
            raw = {'scope': ['一元一次方程'], 'boundary_note': '待学范围'}
        else:
            requests.append(deepcopy(body))
            if before_response:
                await before_response(session, len(requests))
            raw = outputs[len(requests) - 1]
            if isinstance(raw, Exception): raise raw
        return httpx.Response(200, json={'choices': [{'message': {
            'content': raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False)}}]})
    session.llm.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    await session.start(Lesson(topic='一元一次方程', student_profile='知道简单加减法，尚未学习本课内容。'),
                        tts=False, asr_review=False)
    session.state = previous_state()
    for i in (1, 2):
        sid = f't{i}'
        session.sources[sid] = {'id': sid, 'text': '此前已讲授的移项依据。', 'mode': 'text', 'revision': i, 'at': i}
        session.processed_ids.add(sid)
    return session, events, requests


async def teach(session):
    await session.add_transcript('2(x+3)=4x-6，去括号后2x+6=4x-6，再移项得到12=2x，所以x=6。')
    await session.flush()


def test_repair_registers_question_and_commits_only_once(tmp_path, monkeypatch):
    repaired = result()
    repaired['question_updates'] = [{'id': 'q2', 'topic': '两边含未知数的移项',
        'text': repaired['candidate']['text'], 'status': 'pending', 'sources': ['t3']}]
    async def run():
        async def inspect(session, number):
            assert session.state.version == 2
            assert not session.state.knowledge
        session, events, requests = await make_session(tmp_path, monkeypatch, [result(), repaired], inspect)
        try:
            await teach(session); await until(lambda: session.state.version == 3)
            assert len(requests) == 2
            correction = requests[1]['messages'][-1]['content']
            assert 'q2' in correction and 'question_updates' in correction and 'silence' in correction
            assert len([e for e in events if e['type'] == 'state']) == 1
            assert len(session.conversation) == 1
            q = next(q for q in session.state.questions if q.id == 'q2')
            assert q.attempts == 0 and q.status == 'pending'
            assert not session.failed_batch
            await session.try_speak(10)
            assert q.attempts == 1 and q.status == 'asked'
            assert len([e for e in events if e['type'] == 'reply']) == 1
            assert len([e for e in events if e['type'] == 'student_validation_error']) == 1
            assert not [e for e in events if e['type'] in ('error', 'student_recovery')]
        finally: await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('missing_id', ['q2', None])
def test_second_missing_reference_silences_and_continues_next_batch(tmp_path, monkeypatch, missing_id):
    following = {'knowledge_updates': [{'id': 'k_next', 'text': '教师继续解释同类项合并。',
                  'status': 'tentative', 'sources': ['t4']}], 'candidate': {'kind': 'silence'}}
    async def run():
        session, events, requests = await make_session(tmp_path, monkeypatch,
            [result(), result(missing_id), following])
        try:
            await teach(session); await until(lambda: session.state.version == 3)
            assert len(requests) == 2
            assert session.candidate is None or session.candidate['reply'].kind == 'silence'
            assert [q.id for q in session.state.questions] == ['q1']
            assert session.state.questions[0].status == 'resolved'
            assert session.state.knowledge[0].sources == ['t3']
            assert session.state.recent_events[0].id == 'e_new'
            assert not session.failed_batch
            await session.try_speak(10)
            await session.add_transcript('继续讲解合并同类项。'); await session.flush()
            await until(lambda: session.state.version == 4)
            assert session.processed_ids == {'t1','t2','t3','t4'}
            assert len(session.conversation) == 2
            assert not [e for e in events if e['type'] in ('error','reply')]
            recovery = [e['data'] for e in events if e['type'] == 'student_recovery']
            assert len(recovery) == 1 and recovery[0]['question_id'] == missing_id
            assert recovery[0]['message'] == '异常追问已跳过，课堂继续'
            raw = [json.loads(e['data']['raw']) for e in events
                   if e['type'] == 'llm_output' and e['data']['phase'] == 'student']
            assert raw[1]['candidate']['kind'] == 'question'
        finally: await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('first,last', [('json','missing'),('missing','json')])
def test_json_and_reference_repair_share_two_request_budget(tmp_path, monkeypatch, first, last):
    values = {'json': '{broken', 'missing': result()}
    async def run():
        session, events, requests = await make_session(tmp_path, monkeypatch, [values[first], values[last]])
        try:
            await teach(session)
            await until(lambda: session.state.version == 3 if last == 'missing' else session.failed_batch is not None)
            assert len(requests) == 2
            assert session.state.version == (3 if last == 'missing' else 2)
            assert bool([e for e in events if e['type'] == 'student_recovery']) == (last == 'missing')
        finally: await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['knowledge_source','candidate_source','resolution','supersedes','network'])
def test_other_failures_remain_atomic_and_paused(tmp_path, monkeypatch, failure):
    invalid = result()
    if failure == 'knowledge_source': invalid['knowledge_updates'][0]['sources'] = ['unknown']
    elif failure == 'candidate_source': invalid['candidate']['sources'] = ['unknown']
    elif failure == 'resolution':
        invalid['question_updates'] = [{'id':'q1','topic':'移项依据','text':'移项是什么？',
            'status':'resolved','sources':['t3'],'resolution_sources':[]}]
    elif failure == 'supersedes': invalid['knowledge_updates'][0]['supersedes'] = ['unknown_knowledge']
    elif failure == 'network': invalid = httpx.ConnectError('模拟修复时断网')
    async def run():
        session, events, requests = await make_session(tmp_path, monkeypatch, [result(), invalid])
        before = session.state.model_dump()
        try:
            await teach(session); await until(lambda: session.failed_batch is not None)
            assert len(requests) == 2 and session.state.model_dump() == before
            assert 't3' not in session.processed_ids
            assert not session.conversation
            assert not [e for e in events if e['type'] in ('state','student_recovery','reply')]
            assert [e for e in events if e['type'] == 'error'][-1]['data']['retryable']
        finally: await session.close()
    asyncio.run(run())


def test_new_input_during_repair_invalidates_repaired_reply(tmp_path, monkeypatch):
    async def run():
        gate, repairing = asyncio.Event(), asyncio.Event()
        async def hold(session, number):
            if number == 2:
                repairing.set(); await gate.wait()
        repaired = result()
        repaired['question_updates'] = [{'id':'q2','topic':'两边含未知数的移项',
            'text':repaired['candidate']['text'],'status':'pending','sources':['t3']}]
        session, events, requests = await make_session(tmp_path, monkeypatch, [result(), repaired], hold)
        try:
            await teach(session); await repairing.wait()
            await session.add_transcript('稍等，我再补充说明。')
            gate.set(); await until(lambda: session.state.version == 3)
            await session.try_speak(10)
            assert not [e for e in events if e['type'] == 'reply']
            assert session.state.version == 3 and len(requests) == 2
            assert next(q for q in session.state.questions if q.id == 'q2').attempts == 0
        finally: await session.close()
    asyncio.run(run())
