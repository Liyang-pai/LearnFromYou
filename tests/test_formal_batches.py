"""验证课堂批次、历史投影、配置快照与日志脱敏，全部使用离线替身。"""
import asyncio
import json

from backend import config
from backend.dialogue import StudentView
from backend.history import HistoryDialogue, HistoryService
from backend.session import Session
from tests.test_history import SID, event, student_request, write_events, indexed


def valid_state():
    return {'knowledge': [
        {'id': 'k1', 'text': '确定理解', 'status': 'understood', 'sources': ['t1']},
        {'id': 'k2', 'text': '暂定理解', 'status': 'tentative', 'sources': ['t1']},
        {'id': 'k3', 'text': '复核中的理解', 'status': 'understood', 'sources': ['t1']},
        {'id': 'k4', 'text': '尚未理解', 'status': 'unclear', 'sources': ['t1']},
        {'id': 'k5', 'text': '已经失效', 'status': 'invalid', 'sources': ['t1']}],
        'pending_reviews': [{'knowledge_id': 'k3', 'revision': 1, 'reason': '依赖变化'}]}


def test_live_batch_retry_and_history_share_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)

    async def run():
        events = []
        async def send(value): events.append(value)
        session = Session(send, None)
        try:
            for item in [event('transcript', {'id': 't1', 'text': '原文'}),
                         student_request([{'id': 't1', 'text': '实际提交'}]),
                         event('error', {'code': 'model_failure', 'sources': ['other']}),
                         event('error', {'code': 'model_failure', 'sources': ['t1']}),
                         student_request([{'id': 't1', 'text': '实际提交'}]),
                         event('state', {'state': valid_state(), 'processed_sources': ['other']}),
                         event('state', {'state': valid_state(), 'processed_sources': ['t1']})]:
                await session.emit(item['type'], item['data'])
            batches = [e['data'] for e in events if e['type'] == 'teacher_batch']
            assert [b['processing_status'] for b in batches] == ['submitted', 'failed', 'submitted', 'processed']
            assert len({b['batch_id'] for b in batches}) == 1
            assert batches[-1]['request_count'] == 2 and batches[-1]['had_failure']
            assert batches[-1]['segments'][0]['raw_text'] == '原文'
            assert batches[0]['time'] == batches[-1]['time']
            write_events(tmp_path, events)
            # The history reader checks persisted session identity independently.
            service = HistoryService()
            await indexed(service)
            row = (await service.dialogue(SID))['items'][0]
            assert row['batch_id'] == batches[-1]['batch_id']
        finally:
            await session.close()
    # Fix the generated session id so both projections see exactly the same scope.
    monkeypatch.setattr('backend.session.uuid.uuid4', lambda: type('Id', (), {'hex': SID})())
    asyncio.run(run())


def test_batch_identity_scopes_session_and_order():
    first = HistoryDialogue('aaaaaaaaaaaa')
    second = HistoryDialogue('bbbbbbbbbbbb')
    request = student_request([{'id': 't1', 'text': 'A'}, {'id': 't2', 'text': 'B'}])
    first.request(request); second.request(request)
    assert first.finish()[0]['batch_id'] != second.finish()[0]['batch_id']
    first.request(student_request([{'id': 't2', 'text': 'B'}, {'id': 't1', 'text': 'A'}]))
    assert first.finish()[0]['batch_id'] != first.finish()[1]['batch_id']


def test_unscoped_model_failure_does_not_fail_teacher_batch():
    dialogue = HistoryDialogue(SID)
    dialogue.request(student_request([{'id': 't1', 'text': '实际提交'}]))
    dialogue.failed({'code': 'model_failure'})
    assert dialogue.finish()[0]['processing_status'] == 'submitted'


def test_student_view_and_history_keep_last_valid_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    expected = {'understood': ['确定理解'], 'tentative': ['暂定理解'],
                'unclear': ['复核中的理解', '尚未理解'], 'questions': ['为什么？']}

    async def run():
        events = []
        async def send(value): events.append(value)
        session = Session(send, None)
        try:
            await session.emit('state', {'state': valid_state(), 'candidate': {'kind': 'question', 'text': '未发出'}})
            await session.emit('reply', {'kind': 'question', 'text': '为什么？'})
            await session.emit('knowledge_review', {'status': 'running', 'state': {}})
            await session.emit('state', {'state': {'knowledge': '损坏'}})
            await session.emit('finished', {'session_id': SID, 'state': {'knowledge': '损坏'}})
            views = [e['data'] for e in events if e['type'] == 'student_view']
            assert views[-1] == expected
            write_events(tmp_path, events)
            assert (await indexed(HistoryService()))['review'] == expected
        finally: await session.close()
    asyncio.run(run())


def test_student_view_excludes_open_conflicts_and_applies_completed_review():
    view = StudentView()
    state = valid_state()
    state['conflicts'] = [{'id': 'c1', 'left_id': 'k1', 'right_id': 'k2',
                           'conditions': '同一范围', 'reason': '说法矛盾', 'sources': ['t1']}]
    result = view.consume('state', {'state': state})
    assert result['understood'] == [] and result['tentative'] == []
    assert result['unclear'] == ['确定理解', '暂定理解', '复核中的理解', '尚未理解']
    state['conflicts'][0]['status'] = 'resolved'
    state['pending_reviews'] = []
    assert view.consume('knowledge_review', {'status': 'running', 'state': state}) is None
    result = view.consume('knowledge_review', {'status': 'completed', 'state': state})
    assert result['understood'] == ['确定理解', '复核中的理解']
    assert result['questions'] == []


def test_history_rejected_session_record_cannot_replace_valid_review(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    write_events(tmp_path, [event('state', {'state': valid_state()}),
                           event('finished', {'session_id': 'ffffffffffff', 'state': {}})])
    async def run():
        result = await indexed(HistoryService())
        assert result['review']['understood'] == ['确定理解']
        assert result['warnings']
    asyncio.run(run())


def test_session_redacts_snapshotted_key(monkeypatch):
    monkeypatch.setattr(config, 'API_KEY', 'original-secret')
    async def run():
        events = []
        async def send(value): events.append(value)
        session = Session(send, None)
        session.llm.api_key = 'snapshot-secret'
        try:
            await session.emit('error', {'message': 'original-secret snapshot-secret'})
            assert events[-1]['data']['message'] == '[REDACTED] [REDACTED]'
        finally: await session.close()
    asyncio.run(run())


def test_history_and_reports_use_explicit_data_root(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService
    selected = tmp_path / 'isolated'; selected.mkdir()
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(config, 'data_root', lambda: selected, raising=False)
    write_events(selected, [event('transcript', {'id': 't1', 'text': '隔离课堂'})])
    async def run():
        service = HistoryService()
        assert (await indexed(service))['turn_count'] == 1
        assert service.location(SID)[2].is_relative_to(selected)
        assert ReportService().path(SID).is_relative_to(selected)
    asyncio.run(run())


def test_queued_report_freezes_model_before_waiting_for_worker(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService
    from tests.test_teaching_report import save_classroom, FakeModel
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(config, 'MODEL', 'before')
    save_classroom(tmp_path)

    async def run():
        snapshots = []
        def factory(emit):
            snapshots.append(config.MODEL)
            return FakeModel(emit, [])
        service = ReportService(factory)
        await service.lock.acquire()
        await service.start(SID)
        monkeypatch.setattr(config, 'MODEL', 'after')
        service.lock.release()
        await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready'
        assert snapshots == ['before']
    asyncio.run(run())
def test_direct_session_event_redacts_json_escaped_snapshot_key(monkeypatch):
    from backend import config
    from backend.session import Session
    import asyncio
    import json
    secret = 'private-"quoted"-\\slash'
    monkeypatch.setattr(config, 'API_KEY', '')
    async def run():
        events = []
        async def send(event): events.append(event)
        session = Session(send, None)
        session.llm.api_key = secret
        try:
            await session.emit('warning', {'message': secret})
            assert events[-1]['data']['message'] == '[REDACTED]'
            assert secret not in json.dumps(events)
        finally:
            await session.close()
    asyncio.run(run())
