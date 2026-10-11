import asyncio
from copy import deepcopy
import json
from pathlib import Path
import runpy

import pytest
from pydantic import ValidationError

from backend import config
from backend.llm import ModelError
from backend.report_contract import TeachingReport
from backend import report_pipeline as pipeline
from backend.teaching_report import ReportService, read_classroom, export_markdown

fixtures = runpy.run_path(str(Path(__file__).with_name('test_teaching_report.py')))
SID, save_classroom, model_output = (fixtures[k] for k in ('SID', 'save_classroom', 'model_output'))


def long_class(root, length=68000):
    path = save_classroom(root, replies=False)
    events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    # The early statement and final correction deliberately span separate chunks.
    events[2]['data']['segments'][0]['text'] = '暂说尾节点还指向下一节点，稍后纠正。'
    events[4]['data']['text'] = ('沿next访问节点。' * (length // 9)) + '中段末尾哨兵。'
    events[5]['data']['text'] = '纠正开头说法：尾节点指向空，不再访问下一节点。复杂优化留到下节。后半课哨兵。'
    path.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events), encoding='utf-8')
    return read_classroom(root, SID)


class NotesModel:
    def __init__(self, calls, *, fail_at=None, gate=None):
        self.calls, self.fail_at, self.gate = calls, fail_at, gate
        self.closed = False

    async def generate(self, phase, prompt, payload, schema):
        self.calls.append((phase, deepcopy(payload)))
        if self.gate and phase == 'teaching_report':
            await self.gate.wait()
        if len(self.calls) == self.fail_at:
            raise ModelError('模拟阶段失败')
        if phase == 'teaching_report_chunk':
            notes = []
            for turn in payload['turns']:
                if turn['role'] != 'teacher':
                    continue
                kind = '更正' if '纠正开头' in turn['text'] else '要点'
                text = turn['text'] if len(turn['text']) < 100 else '沿next逐个访问节点，属于遍历解释。'
                notes.append({'kind': kind, 'text': text, 'source_ids': [turn['id']]})
            return schema.model_validate({'notes': notes})
        if phase == 'teaching_report_merge':
            notes = []
            seen = set()
            for group in payload['groups']:
                for note in group['notes']:
                    key = (note['text'], tuple(note['source_ids']))
                    if key not in seen:
                        notes.append(note); seen.add(key)
            return schema.model_validate({'notes': notes})
        return schema.model_validate(model_output())

    async def close(self):
        self.closed = True


def test_long_class_covers_all_characters_and_keeps_ids_and_order(tmp_path):
    classroom = long_class(tmp_path)
    payload = pipeline.compact(classroom)
    parts = list(pipeline.chunks(payload))
    assert len(parts) > 3
    assert all(len(pipeline.encode(part)) <= pipeline.CHUNK_LIMIT for part in parts)
    reconstructed = ''.join(t['text'] for part in parts for t in part['turns'] if not t.get('context_only'))
    assert reconstructed == ''.join(t['text'] for t in payload['turns'])
    assert '后半课哨兵' in parts[-1]['turns'][-1]['text']
    assert all(t['id'] in {'t1', 't2', 't3'} for part in parts for t in part['turns'])


def test_long_class_keeps_ai_misunderstanding_and_later_teacher_response_in_order():
    turns = [
        {'id': 't1', 'role': 'teacher', 'text': '解释节点与next。' * 1400},
        {'id': 'r1', 'role': 'ai', 'kind': 'answer', 'text': '尾节点还会有下一个节点。' * 1200},
        {'id': 't2', 'role': 'teacher', 'text': '澄清：尾节点next为空，不能再访问。'},
        {'id': 'r2', 'role': 'ai', 'kind': 'answer', 'text': '为空说明已满足结束条件。'},
    ]
    parts = list(pipeline.chunks({'lesson': {'topic': '链表'}, 'turns': turns}))
    assert len(parts) > 2
    originals = [t for part in parts for t in part['turns'] if not t.get('context_only')]
    for turn in turns:
        assert ''.join(t['text'] for t in originals if t['id'] == turn['id']) == turn['text']
    positions = {source: next(i for i, t in enumerate(originals) if t['id'] == source)
                 for source in ('t1', 'r1', 't2', 'r2')}
    assert positions['t1'] < positions['r1'] < positions['t2'] < positions['r2']
    assert all(len(pipeline.encode(part)) <= pipeline.CHUNK_LIMIT for part in parts)


def test_long_class_all_stages_feed_final_with_cross_chunk_correction(tmp_path, monkeypatch):
    classroom = long_class(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []
    async def run():
        service = ReportService(lambda emit: NotesModel(calls))
        await service.start(SID); await service.tasks[SID]
        result = service.get(SID)
        assert result['status'] == 'ready', result['error']
        groups = calls[-1][1]['groups']
        count = len(list(pipeline.chunks(pipeline.compact(classroom))))
        assert [group['range'] for group in groups] == [[i, i] for i in range(count)]
        texts = str(groups)
        assert '暂说尾节点' in texts and '纠正开头' in texts and '下节' in texts
        assert result['report']['finding']['kind'] == '可选提升'
    asyncio.run(run())


def test_interaction_notes_keep_legacy_type_and_cache_each_prompt_independently(tmp_path):
    from backend.report_contract import AnalysisNotes
    calls = []
    class InteractionModel:
        async def generate(self, phase, prompt, payload, schema):
            calls.append((phase, prompt, deepcopy(payload)))
            return schema.model_validate({'notes': [
                {'kind': '提问检查', 'text': '教师先设问修改是否等于读取，模拟回应有误解；后段已解释区别。',
                 'source_ids': ['t1', 't2']}]})
    async def run():
        runner = pipeline.ReportPipeline(InteractionModel(), tmp_path, SID, 'a' * 64, lambda _: None)
        merge_payload = {'groups': [
            {'range': [0, 0], 'notes': [{'kind': '候选问题', 'text': '读取与修改的疑惑待后文核对。', 'source_ids': ['t1']}]},
            {'range': [1, 1], 'notes': [{'kind': '提问检查', 'text': '教师澄清可读取但不能替换引用，并换例要求理由。', 'source_ids': ['t2']}]}]}
        for phase, prompt in [('teaching_report_chunk', pipeline.ANALYZE_CHUNK),
                              ('teaching_report_merge', pipeline.MERGE_NOTES)]:
            payload = merge_payload if phase.endswith('merge') else {'lesson': {'topic': '读取与修改'}, 'turns': [
                {'id': 't1', 'role': 'teacher', 'text': '元组不能修改是否不能读取？'},
                {'id': 'r1', 'role': 'ai', 'text': '我认为不能读取。'},
                {'id': 't2', 'role': 'teacher', 'text': '读取不会替换元素引用，元组可以读取。'}]}
            first = await runner.stage(phase, prompt, payload, AnalysisNotes, {'t1', 't2'})
            count = len(calls)
            restored = await runner.stage(phase, prompt, payload, AnalysisNotes, {'t1', 't2'})
            assert restored.model_dump() == first.model_dump()
            assert len(calls) == count
            saved = {path: path.read_bytes() for path in runner.directory.glob('*.json')}
            await runner.stage(phase, prompt + '\n新规则', payload, AnalysisNotes, {'t1', 't2'})
            assert len(calls) == count + 1
            assert all(path.read_bytes() == content for path, content in saved.items())
        assert all(call[2]['groups'][0]['range'] < call[2]['groups'][1]['range']
                   for call in calls if call[0].endswith('merge'))
        assert first.notes[0].kind == '提问检查' and first.notes[0].source_ids == ['t1', 't2']
    asyncio.run(run())


def test_failed_stage_resume_reuses_completed_chunks_after_restart(tmp_path, monkeypatch):
    long_class(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    first, resumed = [], []
    async def run():
        service = ReportService(lambda emit: NotesModel(first, fail_at=3))
        await service.start(SID); await service.tasks[SID]
        assert service.get(SID)['status'] == 'failed'
        with pytest.raises(ValueError): export_markdown(service.get(SID))
        service = ReportService(lambda emit: NotesModel(resumed))
        assert service.get(SID)['status'] == 'failed' and not resumed
        await service.start(SID); await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready'
        all_count = len(list(pipeline.chunks(pipeline.compact(read_classroom(tmp_path, SID)))))
        # Identical repeated passages can reuse the same content-addressed stage.
        assert len(first) == 3 and len(resumed) < all_count + 1
        assert all(c[1] not in [first[0][1], first[1][1]] for c in resumed)
        assert resumed[0][1] == first[2][1]
    asyncio.run(run())


def test_interruption_preserves_stages_and_read_never_resumes(tmp_path, monkeypatch):
    long_class(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls, resumed = [], []
    async def run():
        gate = asyncio.Event()
        service = ReportService(lambda emit: NotesModel(calls, gate=gate))
        await service.start(SID)
        for _ in range(100):
            if calls and calls[-1][0] == 'teaching_report': break
            await asyncio.sleep(.001)
        await service.close()
        service = ReportService(lambda emit: NotesModel(resumed))
        assert service.get(SID)['status'] == 'failed' and not resumed
        await service.start(SID); await service.tasks[SID]
        assert [c[0] for c in resumed] == ['teaching_report']
    asyncio.run(run())


def test_recursive_merge_keeps_last_correction_and_coverage(tmp_path, monkeypatch):
    classroom = long_class(tmp_path)
    # Force multiple merge rounds with the same production ordering algorithm.
    monkeypatch.setattr(pipeline, 'SHORT_LIMIT', 1000)
    monkeypatch.setattr(pipeline, 'CHUNK_LIMIT', 700)
    classroom['teacher_segments'][1]['text'] = '演示遍历。' * 600
    classroom['turns'][1]['text'] = classroom['teacher_segments'][1]['text']
    # Sentence pieces must fit the deliberately small test window.
    original_split = pipeline.split_text
    monkeypatch.setattr(pipeline, 'split_text', lambda text: original_split(text, limit=100))
    calls = []
    async def run():
        report = await pipeline.ReportPipeline(NotesModel(calls), tmp_path, SID, 'recursive', lambda _: None).generate(classroom)
        assert isinstance(report, TeachingReport)
        assert any(c[0] == 'teaching_report_merge' for c in calls)
        groups = calls[-1][1]['groups']
        assert groups[0]['range'][0] == 0
        assert '后半课哨兵' in str(groups) and '纠正开头' in str(groups)
    asyncio.run(run())


def test_sparse_report_can_be_honest_without_inventing_strengths():
    output = model_output()
    for dim in output['dimensions'].values():
        dim.update(label='暂不能判断', reason='目前只有一句课堂问候，无法判断。', source_ids=[])
    output['keep'] = None
    output['finding'].update(kind='材料不足', text='只有问候，尚无可评价的讲解。', impact='无法可靠判断教学能力。')
    assert TeachingReport.model_validate(output).keep is None


def test_missing_interaction_cannot_invalidate_other_observed_dimensions():
    output = model_output()
    for key in ('accuracy', 'clarity', 'coherence'):
        output['dimensions'][key]['label'] = '做得好'
    output['finding'].update(kind='材料不足', text='只有教师讲述，无法判断互动。')
    with pytest.raises(ValidationError, match='互动材料'):
        TeachingReport.model_validate(output)
    output['finding']['kind'] = '可选提升'
    assert TeachingReport.model_validate(output).dimensions.checking.label == '暂不能判断'


def test_long_or_unsupported_reports_cannot_be_marked_complete():
    output = model_output()
    output['practice']['minutes'] = 4
    with pytest.raises(ValidationError): TeachingReport.model_validate(output)
    output = model_output()
    for dim in output['dimensions'].values(): dim['reason'] = '长' * 230
    with pytest.raises(ValidationError, match='过长'): TeachingReport.model_validate(output)
    output = model_output()
    output['dimensions']['accuracy']['source_ids'] = []
    with pytest.raises(ValidationError, match='来源'): TeachingReport.model_validate(output)
    output = model_output()
    output['practice']['criterion'] = '学生能答对三个问题。'
    with pytest.raises(ValidationError): TeachingReport.model_validate(output)


@pytest.mark.parametrize('length', [399, 400, 401])
def test_concise_report_enforces_400_character_body_without_losing_sections(length):
    output = model_output()
    texts = [output['overview']['text'], output['finding']['text'], output['finding']['impact'],
             output['keep']['text'], output['action']['text'], output['practice']['task'],
             output['practice']['criterion']]
    texts += [d['reason'] for d in output['dimensions'].values()]
    output['action']['text'] += '例' * (length - sum(map(len, texts)))
    if length > 400:
        with pytest.raises(ValidationError, match='过长'):
            TeachingReport.model_validate(output)
    else:
        report = TeachingReport.model_validate(output)
        assert report.keep is not None and report.action.text and report.practice.criterion
        assert len(report.dimensions.model_dump()) == 4


@pytest.mark.parametrize('kind', ['首要问题', '可选提升', '材料不足'])
def test_finding_does_not_repeat_the_label_in_the_export(kind):
    output = model_output()
    original = output['finding']['text']
    output['finding'].update(kind=kind, text=f'{kind}：{kind}: {original}')
    if kind == '材料不足':
        for dim in output['dimensions'].values():
            dim.update(label='暂不能判断', source_ids=[])
    report = TeachingReport.model_validate(output)
    assert report.finding.text == original
    markdown = export_markdown({'status': 'ready', 'report': report.model_dump()})
    assert f'{kind}：{original}' in markdown
    assert f'{kind}：{kind}' not in markdown


def test_execution_budget_stops_work_without_losing_successful_stages(tmp_path, monkeypatch):
    from backend import teaching_report as module
    long_class(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(module, 'REPORT_BUDGET', .02)
    calls, resumed = [], []
    async def run():
        service = ReportService(lambda emit: NotesModel(calls, gate=asyncio.Event()))
        await service.start(SID); await service.tasks[SID]
        assert service.get(SID)['status'] == 'failed'
        assert '5分钟' in service.get(SID)['error']
        service.model_factory = lambda emit: NotesModel(resumed)
        await service.start(SID); await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready'
        assert [c[0] for c in resumed] == ['teaching_report']
    asyncio.run(run())


@pytest.mark.parametrize('status', [401, 429])
def test_auth_and_quota_errors_do_not_retry_paid_request(monkeypatch, status):
    import httpx
    from backend.llm import ModelClient
    monkeypatch.setattr(config, 'API_KEY', 'test-key')
    async def run():
        calls = []
        async def emit(*args): pass
        def respond(request):
            calls.append(request)
            return httpx.Response(status, json={'error': 'rejected'})
        model = ModelClient(emit)
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(ModelError, match=str(status)):
                await model.generate('teaching_report_chunk', '', {}, pipeline.AnalysisNotes)
            assert len(calls) == 1
        finally: await model.close()
    asyncio.run(run())


def test_single_report_request_timeout_is_not_retried(monkeypatch):
    import httpx
    from backend import llm
    monkeypatch.setattr(config, 'API_KEY', 'test-key')
    monkeypatch.setattr(llm, 'REPORT_REQUEST_TIMEOUT', .01)
    async def run():
        calls = []
        async def emit(*args): pass
        async def respond(request):
            calls.append(request)
            await asyncio.sleep(1)
            return httpx.Response(200)
        model = llm.ModelClient(emit)
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(ModelError, match='60秒'):
                await model.generate('teaching_report_merge', '', {}, pipeline.AnalysisNotes)
            assert len(calls) == 1
        finally: await model.close()
    asyncio.run(run())


def test_auto_start_happens_after_resources_release_and_survives_browser_close(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app as app_module
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    order, calls = [], []
    class Models:
        async def acquire(self): return None, None, 'fake'
        async def release(self): order.append('release')
        async def shutdown(self): pass
    class Session:
        id, ready, closed = SID, True, False
        def __init__(self, send, *args): self.send = send
        async def start(self, *args): await self.send({'type': 'ready', 'data': {}})
        async def finish(self):
            order.append('finish')
            await self.send({'type': 'finished', 'data': {'session_id': SID}})
            await asyncio.sleep(.02)  # Client closes before registration completes.
            self.closed = True
        async def close(self): self.closed = True
    class Service(ReportService):
        async def start(self, sid):
            order.append('report')
            assert order[-2] == 'release'
            return await super().start(sid)
    monkeypatch.setattr(app_module, 'models', Models())
    monkeypatch.setattr(app_module, 'Session', Session)
    monkeypatch.setattr(app_module, 'active_session', None)
    monkeypatch.setattr(app_module, 'reports', Service(lambda emit: NotesModel(calls)))
    with TestClient(app_module.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            ws.send_json({'type': 'start', 'lesson': {'topic': '链表'}})
            assert ws.receive_json()['type'] == 'status'
            assert ws.receive_json()['type'] == 'ready'
            ws.send_json({'type': 'end'})
            assert ws.receive_json()['type'] == 'finished'
        import time
        for _ in range(100):
            result = client.get(f'/api/teaching-report/{SID}').json()
            if result['status'] == 'ready': break
            time.sleep(.005)
        assert result['status'] == 'ready'
        assert order == ['finish', 'release', 'report']
        assert len(calls) == 1
