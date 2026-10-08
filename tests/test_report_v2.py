"""报告 V2 回归：确定性统计、课堂文本版本、隔离验证和失败回退。"""
import asyncio
from copy import deepcopy

import pytest

from backend.report_data import build_snapshot
from backend.report_v2 import ReportService
from backend.llm import ModelError
from backend.report_v2 import markdown


class SimulatedModel:
    """用固定课堂内容模拟四阶段，记录输入以检查隔离，不访问网络。"""
    def __init__(self, calls, *, fail=None, forged=False, repetition=False):
        self.calls, self.fail, self.forged, self.repetition = calls, fail, forged, repetition

    async def generate(self, phase, system, payload, output_type):
        self.calls.append((phase, deepcopy(payload)))
        if self.fail == phase:
            raise ModelError('模拟服务不可用')
        teacher = next(s for s in payload['sources'] if s['kind'] == 'teacher')
        citation = {'event_id': teacher['event_id'], 'quote': teacher['text']}
        if self.forged: citation['event_id'] = '000000000000:e000001'
        if phase.endswith('recall'):
            doubts = [{'text': '我还不清楚：' + q['text'], 'question_ids': [q['id']],
                       'citations': [citation]} for q in payload['questions'] if q['status'] != 'resolved']
            data = {'explained': [{'text': '我根据今天的讲法知道：' + teacher['text'], 'knowledge_ids': ['k1'], 'citations': [citation]}],
                    'doubts': doubts, 'uncertain': [],
                    'probes': [{'id': 'v1', 'knowledge_id': 'k1', 'question': '换一个场景，请说明这个规则如何使用？', 'citations': [citation]}]}
        elif phase.endswith('answers'):
            data = {'answers': [{'id': 'v1', 'text': teacher['text'] if self.repetition else '我会先保存数据，再沿引用找到后继节点；依据是老师给的节点规则。', 'citations': [citation]}]}
        elif phase.endswith('verification'):
            data = {'results': [{'id': 'v1', 'status': '有理解证据', 'reasoning': '解释或应用',
                     'explanation': '在本课堂规则内给出了应用思路，不证明知识客观正确。', 'citations': [citation]}]}
        else:
            data = {'strengths': [{'id': 'f1', 'observation': '教师明确讲述了节点组成。', 'interpretation': '这提供了可追溯的解释依据。', 'citations': [citation]}],
                    'weaknesses': [], 'suggestions': []}
        return output_type.model_validate(data)

    async def close(self): pass


def service_for(tmp_path, monkeypatch, **kwargs):
    from backend import config
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []
    service = ReportService(lambda emit: SimulatedModel(calls, **kwargs))
    service.register(build_snapshot(SID, classroom(question=True), {'topic': '链表'}))
    return service, calls


def test_generation_isolated_idempotent_and_exports(tmp_path, monkeypatch):
    service, calls = service_for(tmp_path, monkeypatch)
    original = deepcopy(service.get(SID)['snapshot'])
    async def run():
        service.start(SID); service.start(SID)
        await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready'
        service.start(SID)
    asyncio.run(run())
    assert len(calls) == 4
    answer_input = next(payload for phase, payload in calls if phase.endswith('answers'))
    assert set(answer_input['public_questions'][0]) == {'id', 'question'}
    assert not {'recall', 'verification', 'reference_answer', 'criteria', 'knowledge_id'} & answer_input.keys()
    assert service.get(SID)['snapshot'] == original
    exported = markdown(service.get(SID))
    for section in ('本次试讲结算', '学生说，我学到了什么', '学生真的理解了吗', '本次试讲的优点', '本次试讲的不足', '下次应该怎么改', '课堂证据'):
        assert section in exported
    assert SID+':e000002' in exported and '原始转写' in exported and '模拟验证' in exported
    assert '（当前理解）' in exported and '（understood）' not in exported


@pytest.mark.parametrize('failed_phase', ['recall', 'answers', 'verification', 'diagnosis'])
def test_stage_failure_never_removes_settlement(tmp_path, monkeypatch, failed_phase):
    service, calls = service_for(tmp_path, monkeypatch, fail='report_v2_'+failed_phase)
    async def run():
        service.start(SID); await service.tasks[SID]
    asyncio.run(run())
    view = service.get(SID)
    assert view['status'] == 'partial'
    assert view['stages'][failed_phase]['status'] == 'failed'
    assert view['snapshot']['settlement']['metrics']['teacher_segments']['value'] == 1
    assert view[failed_phase] is None


def test_forged_evidence_rejected(tmp_path, monkeypatch):
    service, _ = service_for(tmp_path, monkeypatch, forged=True)
    async def run():
        service.start(SID); await service.tasks[SID]
    asyncio.run(run())
    assert service.get(SID)['recall'] is None
    assert service.get(SID)['diagnosis'] is None


def test_mechanical_recall_not_proof(tmp_path, monkeypatch):
    service, _ = service_for(tmp_path, monkeypatch, repetition=True)
    async def run():
        service.start(SID); await service.tasks[SID]
    asyncio.run(run())
    assert service.get(SID)['verification']['results'][0]['status'] == '尚未验证'


def test_retry_after_recall_failure_runs_verification(tmp_path, monkeypatch):
    service, calls = service_for(tmp_path, monkeypatch, fail='report_v2_recall')
    async def run():
        service.start(SID); await service.tasks[SID]
        service.client_factory = lambda emit: SimulatedModel(calls)
        service.start(SID); await service.tasks[SID]
    asyncio.run(run())
    view = service.get(SID)
    assert view['status'] == 'ready'
    assert len(view['answers']['answers']) == 1
    assert len(view['verification']['results']) == 1


def test_no_new_knowledge_or_missing_questions_in_recall():
    from backend.report_v2 import validate_recall
    snapshot = build_snapshot(SID, classroom(question=True))
    item = {'text': '我懂了未讲的算法。', 'knowledge_ids': ['unknown'], 'question_ids': [],
            'citations': [{'event_id': SID+':e000002', 'quote': snapshot['sources'][0]['text']}]}
    with pytest.raises(ValueError):
        validate_recall({'explained': [item], 'doubts': [], 'uncertain': [], 'probes': []}, snapshot)
    item['knowledge_ids'] = ['k1']
    with pytest.raises(ValueError, match='遗漏'):
        validate_recall({'explained': [item], 'doubts': [], 'uncertain': [], 'probes': []}, snapshot)


def test_wrong_quote_and_unprocessed_evidence_rejected():
    from backend.report_v2 import validate_citations
    events = classroom(); events[-1]['data']['unprocessed_sources'] = ['t1']
    snapshot = build_snapshot(SID, events)
    with pytest.raises(ValueError):
        validate_citations([{'event_id': SID+':e000002', 'quote': '不存在的原文'}], snapshot)
    with pytest.raises(ValueError):
        validate_citations([{'event_id': SID+':e000002', 'quote': events[1]['data']['text']}], snapshot, learning_only=True)


def test_answers_extra_reference_field_is_forbidden():
    from backend.report_models import Answers
    with pytest.raises(ValueError):
        Answers.model_validate({'answers': [], 'reference_answer': '秘密答案'})


def test_get_and_invalid_session_api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app as web
    service, _ = service_for(tmp_path, monkeypatch)
    monkeypatch.setattr(web, 'report_service', service)
    with TestClient(web.app) as client:
        first = client.get(f'/api/sessions/{SID}/report-v2')
        assert first.status_code == 200
        assert first.json()['snapshot']['settlement']['metrics']['teacher_segments']['value'] == 1
        assert client.get('/api/sessions/invalid/report-v2').status_code == 400
        assert client.get('/api/sessions/000000000000/report-v2').status_code == 404
        assert client.post(f'/api/sessions/{SID}/report-v2', headers={'Origin': 'https://evil.example'}).status_code == 403
        assert client.get(f'/api/sessions/{SID}/report-v2').json() == first.json()


def test_generation_blocked_during_active_classroom(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app as web
    service, calls = service_for(tmp_path, monkeypatch)
    monkeypatch.setattr(web, 'report_service', service)
    class Active:
        closed = False
        async def close(self): pass
    monkeypatch.setattr(web, 'active_session', Active())
    with TestClient(web.app) as client:
        assert client.post(f'/api/sessions/{SID}/report-v2').status_code == 409
    assert not calls


def test_service_restart_retains_settlement_and_completed_stages(tmp_path, monkeypatch):
    service, calls = service_for(tmp_path, monkeypatch)
    async def run():
        service.start(SID); await service.tasks[SID]
    asyncio.run(run())
    restored = ReportService(lambda emit: SimulatedModel(calls))
    assert restored.get(SID) == service.get(SID)
    before = len(calls)
    async def again(): restored.start(SID)
    asyncio.run(again())
    assert len(calls) == before


def test_pause_preserves_completed_settlement(tmp_path, monkeypatch):
    service, calls = service_for(tmp_path, monkeypatch)
    started = None
    class WaitingModel(SimulatedModel):
        async def generate(self, *args):
            started.set()
            await asyncio.Event().wait()
    async def run():
        nonlocal started
        started = asyncio.Event()
        service.client_factory = lambda emit: WaitingModel(calls)
        service.start(SID); await started.wait(); await service.pause()
    asyncio.run(run())
    assert service.get(SID)['status'] == 'failed'
    assert service.get(SID)['snapshot']['settlement']['metrics']['teacher_segments']['value'] == 1


def test_actual_session_finish_delivers_settlement_without_llm(tmp_path, monkeypatch):
    from backend import config
    from backend.session import Session
    from tests.test_session import FakeModel, until
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    async def run():
        from backend.schemas import Lesson
        events = []
        async def send(event): events.append(event)
        session = Session(send, None)
        await session.llm.close(); session.llm = FakeModel()
        await session.start(Lesson(topic='课堂'), tts=False, asr_review=False)
        await session.add_transcript('节点保存数据', question_count=0); await session.flush()
        await until(lambda: session.state.version == 1)
        await session.finish()
        finished = next(e for e in events if e['type'] == 'finished')
        assert finished['data']['settlement']['metrics']['teacher_questions']['value'] == 0
        assert all(e['id'].startswith(session.id+':') for e in events)
        assert session.closed
    asyncio.run(run())


def test_output_truncation_does_not_repeat_same_request(monkeypatch):
    import httpx
    from backend import config
    from backend.llm import ModelClient
    from backend.report_models import RecallPlan
    monkeypatch.setattr(config, 'API_KEY', 'test-only-key')
    calls = []
    async def run():
        async def emit(kind, data): pass
        async def handler(request):
            calls.append(request)
            return httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]})
        client = ModelClient(emit); await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            with pytest.raises(ModelError, match='长度上限'):
                await client.generate('report_v2_recall', '', {}, RecallPlan)
        finally: await client.close()
    asyncio.run(run())
    assert len(calls) == 1


@pytest.mark.parametrize('case_name', ['正确讲解', '错误讲解', '不完整讲解'])
def test_three_public_classroom_cases(tmp_path, monkeypatch, case_name):
    from scripts.report_v2_examples import fixture, ExampleModel
    from backend import config
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    snapshot, case = fixture(case_name)
    service = ReportService(lambda emit: ExampleModel(case))
    async def run():
        service.register(snapshot); service.start(case['sid']); await service.tasks[case['sid']]
    asyncio.run(run())
    view = service.get(case['sid'])
    assert view['status'] == 'ready'
    if case_name == '正确讲解':
        assert view['verification']['results'][0]['status'] == '有理解证据'
    elif case_name == '错误讲解':
        assert '七' in view['recall']['explained'][0]['text']
        assert view['verification']['results'][0]['status'] == '尚未验证'
        assert not view['diagnosis']['strengths']
    else:
        assert view['recall']['doubts'][0]['question_ids'] == ['q1']
        assert not view['recall']['probes']
        assert view['snapshot']['settlement']['metrics']['unanswered_questions']['value'] == 1
        assert view['diagnosis']['suggestions'][0]['finding_id'] == 'f1'


def test_json_format_error_cannot_create_fake_recall(tmp_path, monkeypatch):
    service, calls = service_for(tmp_path, monkeypatch)
    class InvalidModel(SimulatedModel):
        async def generate(self, phase, system, payload, output_type):
            if phase.endswith('recall'): return {'not_a_report': 'invalid'}
            return await super().generate(phase, system, payload, output_type)
    service.client_factory = lambda emit: InvalidModel(calls)
    async def run():
        service.start(SID); await service.tasks[SID]
    asyncio.run(run())
    assert service.get(SID)['recall'] is None
    assert service.get(SID)['stages']['recall']['status'] == 'failed'


def test_invalid_suggestion_target_is_rejected():
    from backend.report_v2 import validate_diagnosis
    with pytest.raises(ValueError):
        validate_diagnosis({'strengths': [], 'weaknesses': [], 'suggestions': [{'finding_id': 'f1'}]}, build_snapshot(SID, classroom()))


def test_missing_teaching_does_not_spend_model_calls(tmp_path, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []; service = ReportService(lambda emit: SimulatedModel(calls))
    events = classroom(); events[-1]['data']['unprocessed_sources'] = ['t1']
    async def run():
        service.register(build_snapshot(SID, events)); service.start(SID); await service.tasks[SID]
    asyncio.run(run())
    assert not calls
    assert service.get(SID)['status'] == 'failed'
    assert service.get(SID)['snapshot']['settlement']['incomplete']


def test_no_probe_skips_two_model_calls(tmp_path, monkeypatch):
    service, calls = service_for(tmp_path, monkeypatch)
    class NoProbeModel(SimulatedModel):
        async def generate(self, phase, system, payload, output_type):
            result = await super().generate(phase, system, payload, output_type)
            if phase.endswith('recall'): return result.model_copy(update={'probes': []})
            return result
    service.client_factory = lambda emit: NoProbeModel(calls)
    async def run():
        service.start(SID); await service.tasks[SID]
    asyncio.run(run())
    assert len(calls) == 2
    assert service.get(SID)['verification']['results'] == []


def test_no_teacher_question_events_and_manual_count_validation(tmp_path, monkeypatch):
    from backend import config
    from backend.session import Session
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    async def run():
        async def send(event): pass
        session = Session(send, None)
        try:
            for count in (-1, 51, True, 1.5, '1'):
                with pytest.raises(ValueError):
                    await session.add_transcript('讲授', question_count=count)
        finally: await session.close()
    asyncio.run(run())


def test_websocket_to_settlement_and_report_http_pipeline(tmp_path, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from backend import app as web, config, session as sessions
    from backend.schemas import Preparation, StudentResult
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []; service = ReportService(lambda emit: SimulatedModel(calls))
    monkeypatch.setattr(web, 'report_service', service)
    monkeypatch.setattr(sessions, 'report_service', service)
    async def acquire(*args, **kwargs): return None, None, 'fake-asr-no-model'
    async def release(): pass
    async def fake_generate(self, phase, system, payload, output_type):
        if phase == 'preparation': return Preparation(scope=['节点'], boundary_note='课题不是已知知识')
        assert phase == 'student'
        return StudentResult.model_validate({'knowledge_updates': [{'id':'k1', 'text':'节点保存数据和引用', 'status':'understood', 'sources':['t1']}]})
    monkeypatch.setattr(web.models, 'acquire', acquire)
    monkeypatch.setattr(web.models, 'release', release)
    monkeypatch.setattr(sessions.ModelClient, 'generate', fake_generate)
    with TestClient(web.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            ws.send_json({'type':'start', 'lesson':{'topic':'节点'}, 'asr_review':False, 'tts':False})
            while ws.receive_json()['type'] != 'ready': pass
            ws.send_json({'type':'text', 'text':'节点保存数据和下一个节点的引用。', 'question_count':0})
            while ws.receive_json()['type'] != 'state': pass
            ws.send_json({'type':'end'})
            while True:
                event=ws.receive_json()
                if event['type']=='finished': break
            sid=event['data']['session_id']
            assert event['data']['settlement']['metrics']['teacher_questions']['value']==0
            before=client.get(f'/api/sessions/{sid}/report-v2').json()['snapshot']
            for _ in range(100):
                result=client.post(f'/api/sessions/{sid}/report-v2')
                if result.status_code==202: break
                assert result.status_code==409
                time.sleep(.01)
            assert result.status_code==202
            for _ in range(100):
                view=client.get(f'/api/sessions/{sid}/report-v2').json()
                if view['status']!='generating': break
                time.sleep(.01)
            assert view['status']=='ready'
            assert view['snapshot']==before
            assert '学生说，我学到了什么' in view['markdown']
            assert len(calls)==4


def test_model_inputs_exclude_raw_asr_and_unprocessed_text(tmp_path, monkeypatch):
    from backend import config
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls=[]; service=ReportService(lambda emit: SimulatedModel(calls))
    events=classroom(question=True)
    events[1]['data']['raw_text']='不要作为学习依据的识别错误原文'
    events.insert(-1, {'id':SID+':extra', 'type':'transcript', 'elapsed':10,
                     'data':{'id':'t2','text':'不允许学习的未处理公式', 'mode':'text'}})
    events[-1]['data']['unprocessed_sources']=['t2']
    async def run():
        service.register(build_snapshot(SID,events));service.start(SID);await service.tasks[SID]
    asyncio.run(run())
    assert service.get(SID)['status']=='ready'
    for _,payload in calls:
        import json
        encoded=json.dumps(payload,ensure_ascii=False)
        assert '不要作为学习依据的识别错误原文' not in encoded
        assert '不允许学习的未处理公式' not in encoded


def test_asr_failure_without_transcript_marks_possible_gap():
    events=classroom()
    events.insert(-1, {'id':SID+':failure', 'type':'error', 'elapsed':10,
                     'data':{'message':'识别失败，请重讲', 'code':'asr_failure'}})
    summary=build_snapshot(SID,events)['settlement']
    assert summary['incomplete'] and summary['audio_gap_events']==[SID+':failure']

SID = 'abcdef123456'


def test_recall_can_cite_student_doubt_but_knowledge_still_needs_teacher_source():
    from backend.report_v2 import validate_recall
    snapshot = build_snapshot(SID, classroom(question=True))
    teacher = {'event_id': SID + ':e000002', 'quote': snapshot['evidence'][SID + ':e000002']['text']}
    student = {'event_id': SID + ':e000003', 'quote': '删除时为什么看前驱？'}
    plan = {'explained': [], 'doubts': [{'text': '我知道节点的组成，但我还不明白为什么需要前驱。',
            'knowledge_ids': ['k1'], 'question_ids': ['q1'], 'citations': [teacher, student]}],
            'uncertain': [], 'probes': []}
    validate_recall(plan, snapshot)
    plan['doubts'][0]['citations'] = [student]
    with pytest.raises(ValueError, match='知识来源不对应'):
        validate_recall(plan, snapshot)


def classroom(*, text='节点保存数据和下一个节点的引用。', status='understood', question=False):
    events = []
    def add(kind, data, elapsed):
        events.append({'id': f'{SID}:e{len(events)+1:06d}', 'type': kind, 'data': data, 'elapsed': elapsed})
    add('ready', {'prerequisites': [], 'state': {}}, 2)
    add('transcript', {'id': 't1', 'text': text, 'raw_text': text, 'mode': 'text', 'question_count': 0}, 3)
    state = {'version': 1, 'knowledge': [{'id': 'k1', 'text': text, 'status': status, 'sources': ['t1']}], 'questions': []}
    if question:
        state['questions'] = [{'id': 'q1', 'topic': '链表', 'text': '删除时为什么看前驱？', 'status': 'asked', 'sources': ['t1'], 'resolution_sources': [], 'attempts': 1}]
        add('reply', {'kind': 'question', 'question_id': 'q1', 'text': '删除时为什么看前驱？', 'sources': ['t1']}, 5)
    add('state', {'state': state, 'processed_sources': ['t1']}, 6)
    add('finished', {'session_id': SID, 'state': state, 'unprocessed_sources': []}, 12)
    return events


def test_settlement_deduplicates_and_does_not_mutate():
    events = classroom(question=True)
    before = deepcopy(events)
    snapshot = build_snapshot(SID, events + [deepcopy(events[1])])
    stats = snapshot['settlement']['metrics']
    assert stats['duration_seconds']['value'] == 10
    assert stats['teacher_segments']['value'] == 1
    assert stats['teacher_questions']['value'] == 0
    assert stats['student_replies']['value'] == 1
    assert stats['student_questions']['value'] == 1
    assert stats['unanswered_questions']['value'] == 1
    assert stats['verified_understanding']['value'] is None
    assert events == before
    assert snapshot == build_snapshot(SID, events + [deepcopy(events[1])])


def test_reviewed_text_is_used_but_original_retained():
    events = classroom(text='节点保存下个节电。')
    events.insert(2, {'id': SID+':review', 'type': 'review_completed', 'elapsed': 4, 'data': {'segments': [
        {'id': 't1', 'text': '节点保存下个节点。', 'raw_text': '节点保存下个节电。', 'corrected_text': '节点保存下个节点。', 'review_status': 'reviewed'}]}})
    snapshot = build_snapshot(SID, events)
    source = snapshot['sources'][0]
    assert source['text'] == '节点保存下个节点。'
    assert source['raw_text'] == '节点保存下个节电。'
    assert source['review_event_id'] == SID+':review'
    assert snapshot['settlement']['metrics']['teacher_segments']['value'] == 1


def test_missing_counts_are_unknown_and_unprocessed_not_learning():
    events = classroom()
    events[1]['data'].pop('question_count')
    events[-1]['data']['unprocessed_sources'] = ['t1']
    snapshot = build_snapshot(SID, events)
    assert snapshot['settlement']['metrics']['teacher_questions']['value'] is None
    assert not snapshot['learning_sources']
    assert snapshot['settlement']['incomplete']


def test_no_questions_means_zero_not_invented():
    snapshot = build_snapshot(SID, classroom())
    assert snapshot['settlement']['metrics']['student_questions']['value'] == 0
    assert snapshot['settlement']['metrics']['responded_questions']['value'] == 0


def test_only_finished_matching_session_is_accepted():
    with pytest.raises(ValueError):
        build_snapshot(SID, classroom()[:-1])
    with pytest.raises(ValueError):
        build_snapshot('000000000000', classroom())


def test_same_topic_different_question_not_lost():
    from backend.policy import apply_result
    from backend.schemas import Question, StudentState, StudentResult
    state = StudentState(questions=[Question(id='q1', topic='链表', text='节点是什么？', status='resolved', sources=['t1'], resolution_sources=['t1'])])
    result = StudentResult.model_validate({'question_updates': [{'id': 'q2', 'topic': '链表', 'text': '删除操作为什么需要前驱节点？', 'status': 'pending', 'sources': ['t1']}]})
    updated, _ = apply_result(state, result, {'t1': {'text': '课堂'}}, {'t1'})
    assert {q.id for q in updated.questions} == {'q1', 'q2'}
