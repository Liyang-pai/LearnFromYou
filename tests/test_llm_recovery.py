"""Offline transport tests: bounded recovery and honest failure classification."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from backend import config, report_contract
from backend.llm import ModelClient, ModelError
from backend.policy import apply_result, mark_delivered, student_result_validator
from backend.schemas import ResponseRequest, ResumeResult, StudentResult, StudentState


async def client(monkeypatch, responses):
    monkeypatch.setattr(config, 'API_KEY', 'offline-key')
    calls, events = [], []
    async def emit(kind, data):
        events.append((kind, deepcopy(data)))
    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json=responses[len(calls)-1])
    model = ModelClient(emit)
    await model.http.aclose()
    model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return model, calls, events


def envelope(raw, finish='stop'):
    return {'choices':[{'finish_reason':finish, 'message':{'content':raw}}],
            'usage':{'completion_tokens':3500, 'completion_tokens_details':{'reasoning_tokens':2928}}}


@pytest.mark.parametrize('raw', ['{"candidate":', '{}'])
def test_student_truncation_retries_even_if_partial_json_parses(monkeypatch, raw):
    async def run():
        model, calls, events = await client(monkeypatch, [envelope(raw,'length'), envelope('{}')])
        try:
            result = await model.generate('student', '学生规则', {}, StudentResult)
            assert isinstance(result, StudentResult) and len(calls) == 2
            assert 3500 < calls[0]['max_tokens'] < calls[1]['max_tokens']
            assert sum(c['max_tokens'] for c in calls) <= 14000
            assert '截断' in calls[1]['messages'][-1]['content']
            errors = [d for k,d in events if k=='student_validation_error']
            assert len(errors)==1 and errors[0]['kind']=='truncated'
            assert [d['finish_reason'] for k,d in events if k=='llm_output']==['length','stop']
        finally:
            await model.close()
    asyncio.run(run())


def test_student_second_truncation_fails_without_returning_partial_state(monkeypatch):
    async def run():
        model, calls, events = await client(monkeypatch, [envelope('{}','length')]*2)
        try:
            with pytest.raises(ModelError, match='截断'):
                await model.generate('student', '', {}, StudentResult)
            assert len(calls)==2
            errors=[d for k,d in events if k=='student_validation_error']
            assert [d['attempt'] for d in errors]==[1,2]
            assert all(d['kind']=='truncated' for d in errors)
        finally:
            await model.close()
    asyncio.run(run())


def test_json_error_does_not_increase_student_budget(monkeypatch):
    async def run():
        model, calls, events=await client(monkeypatch,[envelope('broken'),envelope('{}')])
        try:
            await model.generate('student','',{},StudentResult)
            assert len(calls)==2 and calls[0]['max_tokens']==calls[1]['max_tokens']
            assert [d['kind'] for k,d in events if k=='student_validation_error']==['json']
        finally:
            await model.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase,result_type', [('student', StudentResult), ('student_resume', ResumeResult)])
def test_misplaced_candidate_fields_get_targeted_retry_and_existing_validation(monkeypatch, phase, result_type):
    flat = '{"kind":"answer","text":"回复","sources":["t1"],"question_updates":[]}'
    nested = '{"candidate":{"kind":"answer","text":"回复","sources":["t1"]},"question_updates":[]}'

    async def run():
        model, calls, events = await client(monkeypatch, [envelope(flat), envelope(nested)])
        validated = []

        def validate(value, retry):
            validated.append((value.candidate.text, retry))
            return value

        try:
            result = await model.generate(phase, '学生规则', {}, result_type, result_validator=validate)
            assert result.candidate.text == '回复' and validated == [('回复', True)]
            assert len(calls) == 2 and calls[0]['max_tokens'] == calls[1]['max_tokens']
            retry_text = calls[1]['messages'][-1]['content']
            assert 'kind、text、sources 移入 candidate' in retry_text and '更新字段仍放在顶层' in retry_text
            assert [d['kind'] for k, d in events if k == 'student_validation_error'] == ['schema']
        finally:
            await model.close()

    asyncio.run(run())


@pytest.mark.parametrize(('update_fields', 'expected'), [
    ({'input_revision': 29}, '删除所有 response_request_updates 条目中的 input_revision'),
    ({'sources': ['t1', 't2']}, "sources 原值为 ['t1']"),
    ({'kind': None}, "kind 原值为 'check_understanding'"),
    ({'status': 'dismissed', 'reason': '教师撤回', 'resolution_sources': ['t1']},
     "取消教师请求 'rr1' 需要本轮教师依据"),
])
def test_teacher_request_retry_pinpoints_bad_update_and_delivers_only_corrected_reply(
        monkeypatch, update_fields, expected):
    request = ResponseRequest(id='rr1', text='能否看到图片', kind='check_understanding',
                              sources=['t1'], input_revision=25)
    state = StudentState(response_requests=[request])
    original = state.model_dump()
    sources = {'t1': {'text': '能看到图片吗？'}, 't2': {'text': '回答刚才的问题。'}}
    update = request.model_dump(exclude={'input_revision'}) | update_fields
    if update['kind'] is None:
        del update['kind']
    candidate = {'kind': 'ack', 'text': '能看到。', 'sources': ['t1', 't2'], 'response_request_ids': ['rr1']}

    async def run():
        model, calls, events = await client(monkeypatch, [
            envelope(json.dumps({'candidate': candidate, 'response_request_updates': [update]})),
            envelope(json.dumps({'candidate': candidate})),
        ])
        try:
            validate = student_result_validator(state, sources, {'t2'}, [], input_revision=29)
            result = await model.generate('student', '学生规则', {}, StudentResult, result_validator=validate)
            assert len(calls) == 2
            retry_text = calls[1]['messages'][-1]['content']
            assert expected in retry_text
            assert [data['attempt'] for kind, data in events if kind == 'student_validation_error'] == [1]
            assert state.model_dump() == original
            updated, reply = apply_result(state, result, sources, {'t2'}, input_revision=29)
            assert updated.response_requests[0] == request
            mark_delivered(updated, reply)
            assert updated.response_requests[0].status == 'answered'
        finally:
            await model.close()

    asyncio.run(run())


def test_final_report_grounding_error_is_logged_and_not_called_structure(monkeypatch):
    from test_report_grounding import output, classroom_turns
    obj=output(); obj['keep']['text']='教师示范换算和代入计算'
    async def run():
        model,calls,events=await client(monkeypatch,[envelope(json.dumps(obj))]*2)
        try:
            bound=report_contract.source_bound(report_contract.TeachingReport,{'t1','t2'},classroom_turns())
            with pytest.raises(ModelError,match='内容或来源'):
                await model.generate('teaching_report','',{},bound)
            errors=[d for k,d in events if k=='report_validation_error']
            assert [d['attempt'] for d in errors]==[1,2]
            assert all(d['kind']=='content' for d in errors)
            assert all(c['max_tokens']==3500 for c in calls)
        finally:
            await model.close()
    asyncio.run(run())


def test_specific_interaction_report_needs_one_request(monkeypatch):
    from test_report_grounding import output
    obj=output(); obj['dimensions']['checking'].update(label='可改进',reason='学生追问被开方数为0，教师未澄清')
    turns=[{'id':'t1','role':'teacher','text':'根号内必须大于0。'},
           {'id':'r1','role':'ai','kind':'question','text':'0为什么不行？'},
           {'id':'t2','role':'teacher','text':'不解释，先下课。'}]
    async def run():
        model,calls,events=await client(monkeypatch,[envelope(json.dumps(obj))]*2)
        try:
            bound=report_contract.source_bound(report_contract.TeachingReport,{'t1','t2'},turns)
            await model.generate('teaching_report','',{},bound)
            assert len(calls)==1
        finally:
            await model.close()
    asyncio.run(run())


@pytest.mark.parametrize('recovers', [True, False])
def test_truncation_commits_state_only_after_full_valid_recovery(tmp_path, monkeypatch, recovers):
    from backend.session import Session
    from backend.schemas import Lesson
    from test_session import until
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(config, 'API_KEY', 'offline-key')
    async def run():
        events, calls = [], []
        async def send(event): events.append(event)
        session = Session(send, None)
        await session.llm.http.aclose()
        async def respond(request):
            body = json.loads(request.content)
            if 'new_teacher_segment_ids' not in str(body['messages']):
                return httpx.Response(200, json=envelope('{"scope":["根式"],"boundary_note":"待学"}'))
            calls.append(body)
            assert session.state.version == 0
            assert not session.state.knowledge
            raw = json.dumps({'knowledge_updates':[{'id':'k1','text':'被开方数必须非负。',
                    'status':'tentative','reason':'教师给出规则，尚待理解依据','sources':['t1']}],
                    'addressed':True,'candidate':{'kind':'answer','text':'0也可以。','sources':['t1']}})
            finish = 'stop' if recovers and len(calls)==2 else 'length'
            return httpx.Response(200, json=envelope(raw, finish))
        session.llm.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            await session.start(Lesson(topic='根式', student_profile='会基础代入。'),tts=False,asr_review=False)
            await session.add_transcript('√0=0，被开方数必须非负。'); await session.flush()
            await until(lambda: session.state.version==1 if recovers else session.failed_batch is not None)
            assert len(calls)==2
            if recovers:
                assert len(session.state.knowledge)==1
                assert len([e for e in events if e['type']=='state'])==1
                assert session.processed_ids=={'t1'} and len(session.conversation)==1
            else:
                assert session.state.version==0 and not session.state.knowledge
                assert not session.processed_ids and not session.conversation
                assert not [e for e in events if e['type'] in ('state','reply')]
                assert '截断' in [e for e in events if e['type']=='error'][-1]['data']['message']
        finally:
            await session.close()
    asyncio.run(run())
