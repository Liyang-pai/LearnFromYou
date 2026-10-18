import asyncio
import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.history import HistoryService, classroom_path
from backend.teaching_report import ReportService
from tests.test_teaching_report import save_classroom, SID, FakeModel


def seed(root, sid=SID, *, count=121, finished=True):
    lesson = {'topic': '惯性的概念', 'grade': '初中八年级', 'subject': '物理', 'student_profile': '当时确认的学生画像'}
    events = [{'type': 'ready', 'time': '2026-10-18T01:00:00+00:00', 'data': {'session_id': sid, 'lesson': lesson}}]
    for index in range(count):
        events.append({'type': 'transcript', 'data': {'id': f't{index}', 'text': f'老师讲授 {index}'}})
        if index % 3 == 0:
            events.append({'type': 'reply', 'data': {'kind': 'answer', 'text': f'学生回答 {index}'}})
    events += [{'type': 'state', 'data': {'candidate': {'text': '未实际发出的候选'}}},
               {'type': 'review_completed', 'data': {'segments': [{'id': 't0', 'text': '老师审核后的讲授', 'review_status': 'reviewed'}]}}]
    if finished:
        events.append({'type': 'finished', 'data': {'session_id': sid, 'lesson': lesson, 'state': {}}})
    path = root / 'logs' / f'{sid}.jsonl'; path.parent.mkdir(exist_ok=True)
    path.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events), encoding='utf-8')
    return path


async def indexed(service, sid=SID):
    result = await service.detail(sid)
    if result['status'] != 'ready':
        await service.jobs[sid]
    return await service.detail(sid)


def test_history_complete_pagination_and_raw_review(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path); seed(tmp_path)
    async def run():
        service = HistoryService(); meta = await indexed(service)
        assert meta['student_profile'] == '当时确认的学生画像'
        assert meta['turn_count'] == 162 and not meta['report_unavailable']
        rows = []; before = None
        while True:
            page = await service.dialogue(SID, before)
            assert len(page['items']) <= 50
            rows = page['items'] + rows
            before = page['before']
            if before is None: break
        assert len(rows) == 162
        assert rows[0]['text'] == '老师审核后的讲授'
        assert rows[0]['raw_text'] == '老师讲授 0'
        assert rows[1]['text'] == '学生回答 0'
        assert '未实际发出的候选' not in json.dumps(rows, ensure_ascii=False)
        assert set((await service.listing())['items'][0]) == {'session_id', 'topic', 'grade', 'subject', 'started_at'}
    asyncio.run(run())


@pytest.mark.parametrize('finished,count,reason', [(False, 1, '未正常结束'), (True, 0, '没有教师')])
def test_history_report_eligibility(tmp_path, monkeypatch, finished, count, reason):
    monkeypatch.setattr(config, 'ROOT', tmp_path); seed(tmp_path, finished=finished, count=count)
    async def run():
        result = await indexed(HistoryService())
        assert reason in result['report_unavailable']
    asyncio.run(run())


def test_old_missing_profile_corruption_and_invalid_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    path = save_classroom(tmp_path)
    with path.open('a') as stream: stream.write('\n{"truncated":')
    async def run():
        service = HistoryService(); meta = await indexed(service)
        assert meta['student_profile'] == '' and meta['grade'] == '未记录'
        assert meta['warnings'] and '记录不完整' in meta['report_unavailable']
        assert (await service.dialogue(SID))['total'] == 6
        directory = service.location(SID)[2]
        (directory / 'meta.json').write_text('broken')
        restored = HistoryService(); assert (await indexed(restored))['status'] == 'ready'
    asyncio.run(run())


def test_history_cache_survives_restart_and_source_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path); path = seed(tmp_path)
    async def run():
        service = HistoryService(); await indexed(service)
        restored = HistoryService()
        def forbidden(*args): raise AssertionError('unchanged log was rescanned')
        monkeypatch.setattr(restored, 'build', forbidden)
        assert (await restored.detail(SID))['status'] == 'ready'
        monkeypatch.undo(); monkeypatch.setattr(config, 'ROOT', tmp_path)
        with path.open('a') as stream: stream.write('\n' + json.dumps({'type': 'reply', 'data': {'kind': 'ack', 'text': '新增回复'}}))
        meta = await indexed(service); assert meta['turn_count'] == 163
    asyncio.run(run())


def test_history_duplicate_indexing_job(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path); seed(tmp_path)
    async def run():
        service = HistoryService(); original = service.build; calls = []
        def build(sid): calls.append(sid); time.sleep(.03); original(sid)
        monkeypatch.setattr(service, 'build', build)
        await asyncio.gather(*(service.detail(SID) for _ in range(12)))
        await service.close(); assert calls == [SID]
    asyncio.run(run())


def test_history_routes_download_and_same_origin(tmp_path, monkeypatch):
    import backend.app as module
    monkeypatch.setattr(config, 'ROOT', tmp_path); path = seed(tmp_path)
    monkeypatch.setattr(module, 'histories', HistoryService())
    with TestClient(module.app) as client:
        response = client.get('/api/history'); assert response.status_code == 200
        assert client.get(f'/api/history/{SID}/log').content == path.read_bytes()
        assert client.get('/api/history/invalid/log').status_code == 400
        assert client.get('/api/history/ffffffffffff').status_code == 404
        assert client.get('/api/history?offset=-1').status_code == 422
        assert client.get('/api/history', headers={'sec-fetch-site': 'cross-site'}).status_code == 403
        assert client.get(f'/api/history/{SID}/log', headers={'origin': 'https://evil.test'}).status_code == 403
    outside = tmp_path / 'outside.jsonl'; outside.write_text('{}')
    (tmp_path / 'logs' / 'ffffffffffff.jsonl').symlink_to(outside)
    with pytest.raises(ValueError): classroom_path('ffffffffffff')


def test_report_material_memo_and_source_prompt_invalidation(tmp_path, monkeypatch):
    import backend.teaching_report as module
    monkeypatch.setattr(config, 'ROOT', tmp_path); path = save_classroom(tmp_path)
    original = module.read_classroom; scans = []
    def read(*args): scans.append(1); return original(*args)
    monkeypatch.setattr(module, 'read_classroom', read)
    service = ReportService()
    service.get(SID); service.get(SID); assert len(scans) == 1
    with path.open('a') as stream: stream.write('\n')
    service.get(SID); assert len(scans) == 2
    monkeypatch.setattr(module, 'TEACHING_REPORT', module.TEACHING_REPORT + '更新')
    service.get(SID); assert len(scans) == 3


def test_report_parallel_start_uses_one_task(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path); save_classroom(tmp_path)
    async def run():
        calls = []; service = ReportService(lambda emit: FakeModel(emit, calls))
        await asyncio.gather(*(service.start(SID) for _ in range(8)))
        await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready'
        assert len(calls) == 1
    asyncio.run(run())
