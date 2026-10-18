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
    with path.open('a', encoding='utf-8') as stream: stream.write('\n{"truncated":')
    async def run():
        service = HistoryService(); meta = await indexed(service)
        assert meta['student_profile'] == '' and meta['grade'] == '未记录'
        assert meta['warnings'] and '记录不完整' in meta['report_unavailable']
        assert (await service.dialogue(SID))['total'] == 6
        directory = service.location(SID)[2]
        (directory / 'meta.json').write_text('broken', encoding='utf-8')
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
        with path.open('a', encoding='utf-8') as stream: stream.write('\n' + json.dumps({'type': 'reply', 'data': {'kind': 'ack', 'text': '新增回复'}}))
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


def test_history_rejects_external_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path); seed(tmp_path)
    outside = tmp_path / 'outside.jsonl'; outside.write_text('{}', encoding='utf-8')
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
    with path.open('a', encoding='utf-8') as stream: stream.write('\n')
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


def event(kind, data, second=0):
    return {'type': kind, 'data': data, 'time': f'2026-10-18T01:00:{second:02d}+00:00', 'elapsed': second}


def student_request(segments, second=10, *, multimodal=False, phase='student', attempt=1):
    payload = json.dumps({'new_teacher_segments': segments, 'current_state': {'note': '状态不是新讲授'},
                          'teacher_sources': [{'id': 'old', 'text': '旧上下文不应出现'}]}, ensure_ascii=False)
    content = [{'type': 'text', 'text': payload}, {'type': 'image_url', 'image_url': {'url': '[图片二进制已省略]'}}] if multimodal else payload
    return event('llm_request', {'phase': phase, 'attempt': attempt, 'body': {'messages': [
        {'role': 'system', 'content': '系统提示词不应出现'},
        {'role': 'user', 'content': json.dumps({'teacher_segments': [{'id': 'old', 'text': '旧对话不应出现'}]})},
        {'role': 'assistant', 'content': '未发言的模型文本不应出现'}, {'role': 'user', 'content': content}]}}, second)


def write_events(root, events):
    path = root / 'logs' / f'{SID}.jsonl'; path.parent.mkdir(exist_ok=True)
    path.write_text('\n'.join(json.dumps(item, ensure_ascii=False) for item in events), encoding='utf-8')
    return path


async def all_dialogue(service):
    await indexed(service); rows, before = [], None
    while True:
        page = await service.dialogue(SID, before)
        assert len(page['items']) <= 50
        rows = page['items'] + rows
        before = page['before']
        if before is None: return rows


@pytest.mark.parametrize('multimodal', [False, True])
def test_actual_batches_use_submitted_text_and_request_order(tmp_path, monkeypatch, multimodal):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    first = [{'id': 't1', 'text': '第一段实际提交的校正文本。'}, {'id': 't2', 'text': '第二段实际提交的文本。'}]
    write_events(tmp_path, [event('transcript', {'id': 't1', 'text': '第一段原始转写'}, 1),
        event('transcript', {'id': 't2', 'text': '第二段原始转写'}, 2),
        event('segment', {'sources': ['t1'], 'text': '队列片段不等于请求批次'}, 3),
        event('segment', {'sources': ['t2'], 'text': '另一个队列片段'}, 4),
        student_request(first, 5, multimodal=multimodal),
        event('transcript', {'id': 't3', 'text': '先于学生回复收到的下一段'}, 6),
        event('state', {'processed_sources': ['t2', 't1']}, 7),
        event('reply', {'kind': 'answer', 'text': '真实学生回复'}, 8),
        student_request([{'id': 't3', 'text': '下一批实际提交文本'}], 9),
        event('review_completed', {'segments': [{'id': 't1', 'text': '事后审核不能覆盖实际发送文本'}]}, 10)])
    async def run():
        rows = await all_dialogue(HistoryService())
        assert [row['role'] for row in rows] == ['teacher', 'student', 'teacher']
        assert rows[0]['text'] == '\n'.join(item['text'] for item in first)
        assert rows[0]['processing_status'] == 'processed'
        assert rows[0]['time'].endswith('05+00:00')
        assert [part['raw_text'] for part in rows[0]['segments']] == ['第一段原始转写', '第二段原始转写']
        assert rows[0]['segments'][0]['time'].endswith('01+00:00')
        assert rows[2]['text'] == '下一批实际提交文本'
        assert rows[2]['processing_status'] == 'submitted'
        assert '不应出现' not in json.dumps(rows, ensure_ascii=False)
    asyncio.run(run())


def test_automatic_and_manual_retry_collapse_at_first_request(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    segments = [{'id': 't1', 'text': '实际讲授'}]
    repair = student_request(segments, 3, attempt=2)
    repair['data']['body']['messages'].append({'role': 'user', 'content': '请修复结构，此句不是老师讲授'})
    write_events(tmp_path, [event('transcript', {'id': 't1', 'text': '原始讲授'}, 1),
        student_request(segments, 2), event('student_validation_error', {'attempt': 1}, 3), repair,
        event('error', {'code': 'model_failure', 'sources': ['t1']}, 4),
        student_request(segments, 5), event('state', {'processed_sources': ['t1']}, 6),
        event('reply', {'kind': 'ack', 'text': '收到'}, 7)])
    async def run():
        rows = await all_dialogue(HistoryService()); assert len(rows) == 2
        assert rows[0]['request_count'] == 3 and rows[0]['had_failure']
        assert rows[0]['processing_status'] == 'processed'
        assert rows[0]['time'].endswith('02+00:00')
    asyncio.run(run())


@pytest.mark.parametrize('outcome,status', [
    (event('error', {'code': 'model_failure', 'sources': ['t1']}), 'failed'),
    (event('error', {'code': 'model_failure', 'sources': ['other']}), 'submitted'),
    (event('llm_output', {'phase': 'student', 'raw': '{}'}), 'submitted'),
    (event('state', {'processed_sources': ['other']}), 'submitted'),
])
def test_request_is_not_proof_of_success(tmp_path, monkeypatch, outcome, status):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    write_events(tmp_path, [event('transcript', {'id': 't1', 'text': '原文'}),
                           student_request([{'id': 't1', 'text': '提交文本'}]), outcome])
    async def run():
        rows = await all_dialogue(HistoryService())
        assert len(rows) == 1 and rows[0]['processing_status'] == status
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['student_resume', 'knowledge_review', 'preparation'])
def test_other_phases_and_uncertain_fragments_stay_unconfirmed(tmp_path, monkeypatch, phase):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    write_events(tmp_path, [event('transcript', {'id': 't1', 'text': '第一段未确认'}),
        student_request([{'id': 't1', 'text': '不能把这个阶段当作新讲授'}], phase=phase),
        event('reply', {'kind': 'answer', 'text': '实际发言'}),
        event('transcript', {'id': 't2', 'text': '第二段未确认'})])
    async def run():
        rows = await all_dialogue(HistoryService())
        assert [row['text'] for row in rows] == ['第一段未确认', '实际发言', '第二段未确认']
        assert rows[0]['processing_status'] == rows[2]['processing_status'] == 'unconfirmed'
    asyncio.run(run())


def test_changed_wording_or_order_is_a_distinct_submission(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    first = [{'id': 't1', 'text': 'A'}, {'id': 't2', 'text': 'B'}]
    changed = [{'id': 't1', 'text': '修订A'}, {'id': 't2', 'text': 'B'}]
    write_events(tmp_path, [student_request(first), student_request(changed), student_request(list(reversed(first)))])
    async def run():
        rows = await all_dialogue(HistoryService())
        assert [row['text'] for row in rows] == ['A\nB', '修订A\nB', 'B\nA']
        assert rows[0]['segments'][0]['raw_text'] is None
    asyncio.run(run())


@pytest.mark.parametrize('segments', ['invalid', [{'text': '无来源'}], [{'id': 't1'}],
                                     [{'id': 't1', 'text': 'A'}, {'id': 't1', 'text': 'B'}]])
def test_malformed_batch_preserves_original_fragment(tmp_path, monkeypatch, segments):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    write_events(tmp_path, [event('transcript', {'id': 't1', 'text': '仍然保留的原文'}), student_request(segments)])
    async def run():
        service = HistoryService(); rows = await all_dialogue(service)
        assert len(rows) == 1 and rows[0]['text'] == '仍然保留的原文'
        assert rows[0]['processing_status'] == 'unconfirmed'
        assert (await service.detail(SID))['warnings']
    asyncio.run(run())


def test_batch_pagination_never_splits_or_duplicates_fragments(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    events, expected = [], []
    for batch in range(63):
        segments = [{'id': f't{batch * 3 + n}', 'text': f'提交文本 {batch}-{n}'} for n in range(3)]
        for segment in segments:
            events.append(event('transcript', {'id': segment['id'], 'text': segment['text'] + ' 原文'}))
            expected.append(segment['id'])
        events.append(student_request(segments))
        events.append(event('state', {'processed_sources': [segment['id'] for segment in segments]}))
        if batch % 5 == 0: events.append(event('reply', {'kind': 'ack', 'text': f'回答 {batch}'}))
    write_events(tmp_path, events)
    async def run():
        rows = await all_dialogue(HistoryService())
        assert len(rows) == 76
        teachers = [row for row in rows if row['role'] == 'teacher']
        assert [sid for row in teachers for sid in row['source_ids']] == expected
        assert all(len(row['segments']) == 3 for row in teachers)
        assert all(row['processing_status'] == 'processed' for row in teachers)
    asyncio.run(run())


def test_cache_version_upgrade_rebuilds_without_touching_log_or_report(tmp_path, monkeypatch):
    import backend.history as module
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    path = write_events(tmp_path, [event('transcript', {'id': 't1', 'text': '原文'}),
                                  student_request([{'id': 't1', 'text': '实际提交'}])])
    raw = path.read_bytes(); stat = path.stat()
    old = tmp_path / 'logs/.history' / SID / f'1-{stat.st_size}-{stat.st_mtime_ns}'
    old.mkdir(parents=True); (old / 'meta.json').write_text('{"version":1}', encoding='utf-8')
    report = tmp_path / 'logs' / f'{SID}.teaching-report-v2.json'; report.write_text('报告原样保留', encoding='utf-8')
    async def run():
        service = HistoryService(); assert (await service.detail(SID))['status'] == 'indexing'
        await service.jobs[SID]
        assert module.VERSION == 3 and not old.exists()
        assert (await service.dialogue(SID))['items'][0]['text'] == '实际提交'
        assert path.read_bytes() == raw and report.read_text(encoding='utf-8') == '报告原样保留'
    asyncio.run(run())
