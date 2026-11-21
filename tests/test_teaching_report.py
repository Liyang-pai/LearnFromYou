import asyncio
from copy import deepcopy
import json

import pytest

from backend import config
from backend.teaching_report import read_classroom


SID = '012345abcdef'


def save_classroom(root, *, replies=True, finished=True):
    events = [
        {'type': 'ready', 'data': {'session_id': SID}},
        {'type': 'transcript', 'data': {'id': 't1', 'text': '练表由节点组成。', 'mode': 'microphone'}},
        {'type': 'review_completed', 'data': {'segments': [{'id': 't1', 'text': '链表由节点组成。', 'review_status': 'reviewed'}]}},
        {'type': 'state', 'data': {'candidate': {'kind': 'answer', 'text': '未发出的回答'}}},
        {'type': 'transcript', 'data': {'id': 't2', 'text': '沿 next 指针逐个访问节点。', 'mode': 'text'}},
        {'type': 'transcript', 'data': {'id': 't3', 'text': '最后一个节点指向空。', 'mode': 'text'}},
    ]
    if replies:
        events += [{'type': 'reply', 'data': {'kind': kind, 'text': text}} for kind, text in
                   [('question', '从哪里开始？'), ('ack', '好的。'), ('answer', '从头节点开始。')]]
    if finished:
        events.append({'type': 'finished', 'data': {'session_id': SID, 'lesson': {'topic': '链表'},
            'unprocessed_sources': ['t3'], 'state': {'open_questions': [], 'questions': [{'status': 'asked'}]}}})
    path = root / 'logs' / f'{SID}.jsonl'
    path.parent.mkdir(exist_ok=True)
    path.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events), encoding='utf-8')
    return path


def test_facts_use_delivered_replies_and_canonical_teacher_text(tmp_path):
    save_classroom(tmp_path)
    classroom = read_classroom(tmp_path, SID)
    assert classroom['teacher_segments'][0]['text'] == '链表由节点组成。'
    assert classroom['facts'] == {'teacher_segments': 3, 'ai_questions': 1, 'ai_answers': 1,
        'ai_acknowledgements': 1, 'unprocessed_segments': 1, 'open_ai_questions': 0}
    assert classroom['teacher_segments'][-1]['processed'] is False
    assert '未发出的回答' not in json.dumps(classroom, ensure_ascii=False)


@pytest.mark.parametrize(('status', 'review_text', 'expected'), [
    ('reviewed', '链表尾节点指向空。', '链表尾节点指向空。'),
    ('corrected', '链表尾节点指向空。', '链表尾节点指向空。'),
    ('unchanged', '链表尾节点指向空。', '链表尾节点指向空。'),
    ('reviewed', '', '尾节点的指针指向空。'),
    ('reviewed', '  ', '尾节点的指针指向空。'),
    ('reviewed', None, '尾节点的指针指向空。'),
    ('fallback', '失败结果不能采用。', '尾节点的指针指向空。'),
    ('failed', '失败结果不能采用。', '尾节点的指针指向空。'),
    ('disabled', '未审核结果不能采用。', '尾节点的指针指向空。'),
    ('pending', '未完成结果不能采用。', '尾节点的指针指向空。'),
    (None, '无成功审核状态。', '尾节点的指针指向空。'),
])
def test_backend_selects_one_teacher_text_before_report_analysis(tmp_path, status, review_text, expected):
    from backend.report_pipeline import compact
    events = [
        {'type': 'ready', 'data': {'session_id': SID}},
        {'type': 'transcript', 'data': {'id': 't1', 'text': '尾节点的指针指向空。',
                                      'mode': 'microphone', 'review_status': 'pending'}},
        {'type': 'review_completed', 'data': {'segments': [
            {'id': 't1', 'text': review_text, 'review_status': status}]}},
        {'type': 'reply', 'data': {'kind': 'answer', 'text': '所以看到空指针就结束。'}},
        {'type': 'finished', 'data': {'session_id': SID, 'lesson': {'topic': '链表'}}},
    ]
    path = tmp_path / 'logs' / f'{SID}.jsonl'
    path.parent.mkdir()
    path.write_text('\n'.join(json.dumps(event, ensure_ascii=False) for event in events), encoding='utf-8')
    before = path.read_bytes()
    classroom = read_classroom(tmp_path, SID)
    payload = compact(classroom)
    assert payload['turns'][0] == {'id': 't1', 'role': 'teacher', 'text': expected, 'mode': 'microphone'}
    assert payload['turns'][1]['text'] == '所以看到空指针就结束。'
    assert not any(field in json.dumps(payload) for field in ('review_status', 'raw_text', 'original_text'))
    assert path.read_bytes() == before


def test_mixed_teacher_sources_keep_review_selection_and_input_types(tmp_path):
    from backend.report_pipeline import compact
    path = save_classroom(tmp_path)
    events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    events.insert(3, {'type': 'transcript', 'data': {
        'id': 't4', 'text': '错误的待审核副本。', 'raw_text': '先判断当前指针是否为空。',
        'mode': 'microphone', 'review_status': 'disabled'}})
    path.write_text('\n'.join(json.dumps(event, ensure_ascii=False) for event in events), encoding='utf-8')
    payload = compact(read_classroom(tmp_path, SID))
    teachers = [turn for turn in payload['turns'] if turn['role'] == 'teacher']
    assert [(turn['id'], turn['text'], turn['mode']) for turn in teachers] == [
        ('t1', '链表由节点组成。', 'microphone'),
        ('t4', '先判断当前指针是否为空。', 'microphone'),
        ('t2', '沿 next 指针逐个访问节点。', 'text'),
        ('t3', '最后一个节点指向空。', 'text'),
    ]


def test_no_answers_is_an_observation_not_a_mastery_judgment(tmp_path):
    save_classroom(tmp_path, replies=False)
    classroom = read_classroom(tmp_path, SID)
    assert classroom['facts']['ai_answers'] == 0
    assert classroom['ai_replies'] == []
    assert 'mastery' not in classroom['facts']


def test_report_receives_reviewed_question_actual_reply_and_later_clarification(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService
    path = save_classroom(tmp_path, replies=False)
    events = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
    events[1]['data']['text'] = '链表遍历会一直走下去吗？'
    events[2]['data']['segments'][0]['text'] = '沿next遍历链表会一直走下去吗？请说明结束条件。'
    events[4:6] = [
        {'type': 'reply', 'data': {'kind': 'answer', 'text': '会，因为每个节点都有下一个节点。'}},
        {'type': 'transcript', 'data': {'id': 't2', 'text': '尾节点的next为空，访问完它后应结束。', 'mode': 'text'}},
        {'type': 'reply', 'data': {'kind': 'answer', 'text': '懂了。'}},
        {'type': 'transcript', 'data': {'id': 't3', 'text': '若头指针已经为空，是否还需要访问节点？为什么？', 'mode': 'text'}},
        {'type': 'reply', 'data': {'kind': 'answer', 'text': '不需要，因为一开始就满足结束条件，没有节点可访问。'}},
    ]
    path.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events), encoding='utf-8')
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []
    async def run():
        service = ReportService(lambda emit: FakeModel(emit, calls))
        await service.start(SID)
        await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready'
        assert len(calls) == 1
        turns = calls[0][1]['turns']
        assert [(t['role'], t['text']) for t in turns] == [
            ('teacher', events[2]['data']['segments'][0]['text']),
            ('ai', events[4]['data']['text']), ('teacher', events[5]['data']['text']),
            ('ai', events[6]['data']['text']), ('teacher', events[7]['data']['text']),
            ('ai', events[8]['data']['text'])]
        assert '未发出的回答' not in json.dumps(turns, ensure_ascii=False)
    asyncio.run(run())


@pytest.mark.parametrize('sid', ['../outside', SID + '.jsonl', 'bad'])
def test_reject_path_traversal(tmp_path, sid):
    with pytest.raises(ValueError):
        read_classroom(tmp_path, sid)


def test_unfinished_class_cannot_generate_report(tmp_path):
    save_classroom(tmp_path, finished=False)
    with pytest.raises(ValueError, match='结束'):
        read_classroom(tmp_path, SID)


def model_output():
    return {'overview': {'text': '本次说明链表节点的组成、沿next遍历及末尾为空的结束条件。', 'source_ids': ['t1', 't2', 't3']},
        'dimensions': {
            'accuracy': {'label': '做得好', 'reason': '节点、链接和末尾为空的说明一致。', 'source_ids': ['t1', 't3']},
            'clarity': {'label': '可改进', 'reason': '遍历步骤已有说明，可增加空输入对照。', 'source_ids': ['t2']},
            'coherence': {'label': '做得好', 'reason': '先讲组成，再讲访问和结束，主线连贯。', 'source_ids': ['t1', 't2']},
            'checking': {'label': '暂不能判断', 'reason': '现有片段不足以判断检查设计。', 'source_ids': []}},
        'finding': {'kind': '可选提升', 'text': '用空链表作对照，帮助区分起点与结束条件。',
                    'impact': '能使边界情形更直观；这不是本次漏讲的确定结论。', 'source_ids': ['t2', 't3']},
        'keep': {'text': '保留从节点组成到指针访问的顺序，便于建立过程认识。', 'source_ids': ['t1', 't2']},
        'action': {'text': '可补一句：头指针为空时，没有可访问节点，直接结束；不为空才访问当前节点并沿next继续。', 'source_ids': ['t2']},
        'practice': {'minutes': 2, 'task': '分别用空链表与两个节点演示遍历，边指图边说明条件。',
                     'criterion': '教师能明确说出起点、每步更新和结束条件，空链表演示不访问节点。', 'source_ids': ['t2', 't3']}}


def reflection_output(teacher):
    sid = teacher['id']
    evidence = [{'source_id': sid, 'actor': 'teacher', 'quote': teacher['text'][:80]}]
    return {'assessment': {}, 'overview': {'text': '本次说明链表遍历的步骤。', 'evidence': evidence},
            'keep': {'text': '保留按访问顺序讲解的做法。', 'evidence': evidence},
            'findings': [{'kind': '可选提升', 'dimension': 'clarity', 'title': '对照空链表',
                          'fact': '你讲了节点访问和结束条件。', 'action': '补一句：头指针为空时，直接结束。',
                          'evidence': evidence}],
            'expression': None,
            'practice': {'minutes': 2, 'task': '分别用空链表和两个节点讲遍历。',
                         'example': '若头指针为空，还需要访问节点吗？',
                         'criterion': '教师能明确说出起点和结束条件。'}}


class FakeModel:
    def __init__(self, emit, calls, *, error=None, output=None, gate=None):
        self.calls, self.error, self.output, self.gate = calls, error, output, gate
        self.closed = False

    async def generate(self, phase, system, payload, output_type):
        self.calls.append((phase, deepcopy(payload)))
        if self.gate:
            await self.gate.wait()
        if self.error:
            raise self.error
        if 'findings' in output_type.model_fields:
            teacher = next(turn for turn in reversed(payload['turns']) if turn['role'] == 'teacher')
            return output_type.model_validate(self.output or reflection_output(teacher))
        return output_type.model_validate(self.output or model_output())

    async def close(self):
        self.closed = True


def test_report_needs_only_one_analysis_and_survives_restart(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService, export_markdown
    save_classroom(tmp_path, replies=False)
    calls = []
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    models = []
    def factory(emit):
        model = FakeModel(emit, calls)
        models.append(model)
        return model
    async def run():
        service = ReportService(factory)
        assert service.get(SID)['status'] == 'idle' and not calls
        await service.start(SID)
        await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready'
        await service.start(SID)
        assert len(calls) == 1
        assert calls[0][0] == 'teaching_report'
        assert all(t['role'] == 'teacher' for t in calls[0][1]['turns'])
        restored = ReportService(factory).get(SID)
        assert restored['status'] == 'ready'
        assert restored['schema_version'] == 3
        assert restored['report']['findings'][0]['kind'] == '可选提升'
        markdown = export_markdown(restored)
        assert '课堂依据' in markdown and '下次试试' in markdown
        assert '空链表' in markdown
        assert '课堂统计' not in markdown and '教师讲授 3 段' not in markdown
        assert '未记录到 AI 学生回答' not in markdown
        assert 'source_ids' not in markdown and '练表由节点' not in markdown
        assert all(model.closed for model in models)
    asyncio.run(run())


@pytest.mark.parametrize('phase', ['REFLECTION_REPORT', 'REFLECTION_NOTES', 'ANALYZE_CHUNK', 'MERGE_NOTES'])
def test_changed_prompt_invalidates_cache_without_paid_read_or_rewriting_old_result(tmp_path, monkeypatch, phase):
    from backend import teaching_report, report_prompt
    path = save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []
    async def run():
        service = teaching_report.ReportService(lambda emit: FakeModel(emit, calls))
        await service.start(SID)
        await service.tasks[SID]
        cache = service.path(SID)
        saved, original = cache.read_bytes(), path.read_bytes()
        monkeypatch.setattr(teaching_report, phase, getattr(report_prompt, phase) + '\n新的互动评价规则')
        assert service.get(SID)['status'] == 'idle'
        assert teaching_report.ReportService(lambda emit: FakeModel(emit, calls)).get(SID)['status'] == 'idle'
        assert len(calls) == 1
        assert cache.read_bytes() == saved and path.read_bytes() == original
    asyncio.run(run())


def test_duplicate_start_does_not_duplicate_paid_request(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    async def run():
        calls, gate = [], asyncio.Event()
        service = ReportService(lambda emit: FakeModel(emit, calls, gate=gate))
        await asyncio.gather(service.start(SID), service.start(SID))
        await asyncio.sleep(.01)
        assert len(calls) == 1
        gate.set()
        await service.tasks[SID]
    asyncio.run(run())


def test_invalid_reference_is_failed_and_cannot_be_exported(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService, export_markdown
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    output = reflection_output({'id': 't3', 'text': '最后一个节点指向空。'})
    output['overview']['evidence'][0]['source_id'] = 't999'
    async def run():
        service = ReportService(lambda emit: FakeModel(emit, [], output=output))
        await service.start(SID)
        await service.tasks[SID]
        result = service.get(SID)
        assert result['status'] == 'failed' and result['report'] is None
        with pytest.raises(ValueError):
            export_markdown(result)
    asyncio.run(run())


def test_connection_failure_can_retry_without_simulation(tmp_path, monkeypatch):
    from backend.llm import ModelError
    from backend.teaching_report import ReportService
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []
    async def run():
        service = ReportService(lambda emit: FakeModel(emit, calls, error=ModelError('模拟 HTTP 503')))
        await service.start(SID)
        await service.tasks[SID]
        assert service.get(SID)['status'] == 'failed'
        service.model_factory = lambda emit: FakeModel(emit, calls)
        await service.start(SID)
        await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready' and len(calls) == 2
    asyncio.run(run())


def test_api_view_never_starts_model_and_export_requires_success(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app as service
    from backend.teaching_report import ReportService
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []
    monkeypatch.setattr(service, 'reports', ReportService(lambda emit: FakeModel(emit, calls)))
    with TestClient(service.app) as client:
        assert client.get(f'/api/teaching-report/{SID}').json()['status'] == 'idle'
        assert client.get(f'/api/teaching-report/{SID}/export').status_code == 409
        assert not calls
        assert client.post(f'/api/teaching-report/{SID}', headers={'Origin': 'https://external.test'}).status_code == 403
        response = client.post(f'/api/teaching-report/{SID}')
        assert response.status_code == 200
        for _ in range(50):
            result = client.get(f'/api/teaching-report/{SID}').json()
            if result['status'] == 'ready':
                break
        assert result['status'] == 'ready' and len(calls) == 1
        assert '空链表' in client.get(f'/api/teaching-report/{SID}/export').text
        assert client.get('/api/teaching-report/aaaaaaaaaaaa').status_code == 404


def test_cannot_reuse_report_after_source_changes(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService
    path = save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    async def run():
        service = ReportService(lambda emit: FakeModel(emit, []))
        await service.start(SID)
        await service.tasks[SID]
        path.write_text(path.read_text(encoding='utf-8').replace('链表由节点组成。', '链表由数据和链接组成。'), encoding='utf-8')
        assert service.get(SID)['status'] == 'idle'
    asyncio.run(run())


def test_interrupted_generation_is_failed_then_can_retry(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    async def run():
        calls, gate = [], asyncio.Event()
        service = ReportService(lambda emit: FakeModel(emit, calls, gate=gate))
        await service.start(SID)
        await asyncio.sleep(.01)
        await service.close()
        assert ReportService().get(SID)['status'] == 'failed'
        service.model_factory = lambda emit: FakeModel(emit, calls)
        await service.start(SID)
        await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready'
    asyncio.run(run())


def test_truncated_report_response_is_not_blindly_retried(monkeypatch):
    import httpx
    from backend.llm import ModelClient, ModelError
    from backend.teaching_report import TeachingReport
    monkeypatch.setattr(config, 'API_KEY', 'fake-key')
    async def run():
        calls = []
        async def emit(*args): pass
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': '{"overview":'}}]})
        model = ModelClient(emit)
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with pytest.raises(ModelError, match='截断'):
                await model.generate('teaching_report', '', {}, TeachingReport)
            assert len(calls) == 1
        finally:
            await model.close()
    asyncio.run(run())


def test_missing_interaction_contradiction_is_repaired_once(monkeypatch):
    import httpx
    from backend.llm import ModelClient
    from backend.report_contract import TeachingReport
    monkeypatch.setattr(config, 'API_KEY', 'fake-key')
    async def run():
        calls = []
        async def emit(*args): pass
        def respond(request):
            calls.append(json.loads(request.content))
            output = model_output()
            if len(calls) == 1:
                output['finding'].update(kind='材料不足', text='没有学生回答，整体材料不足。')
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
        model = ModelClient(emit)
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            result = await model.generate('teaching_report', '', {}, TeachingReport)
            assert len(calls) == 2
            assert result.finding.kind == '可选提升'
            assert '互动材料' in calls[1]['messages'][-1]['content']
        finally: await model.close()
    asyncio.run(run())


def test_contradictory_cached_report_is_not_exported_or_repaired_by_read(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService, export_markdown
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []
    async def run():
        service = ReportService(lambda emit: FakeModel(emit, calls))
        await service.start(SID)
        await service.tasks[SID]
        path = service.path(SID)
        saved = json.loads(path.read_text(encoding='utf-8'))
        saved['report']['overview']['text'] = '学生已经掌握链表遍历。'
        path.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
        original = path.read_bytes()
        result = service.get(SID)
        assert result['status'] == 'failed' and result['report'] is None
        with pytest.raises(ValueError): export_markdown(result)
        assert len(calls) == 1 and path.read_bytes() == original
    asyncio.run(run())


def test_more_than_five_valid_sources_does_not_fail_entire_report():
    from backend.teaching_report import TeachingReport, validate_sources
    output = model_output()
    output['overview']['source_ids'] = [f't{i}' for i in range(1, 7)]
    report = TeachingReport.model_validate(output)
    validate_sources(report, {'teacher_segments': [{'id': f't{i}'} for i in range(1, 7)]})


def test_report_structure_retry_identifies_bad_field(monkeypatch):
    import httpx
    from backend.llm import ModelClient
    from backend.teaching_report import TeachingReport
    monkeypatch.setattr(config, 'API_KEY', 'fake-key')
    async def run():
        bodies = []
        async def emit(*args): pass
        def respond(request):
            bodies.append(json.loads(request.content))
            output = model_output()
            if len(bodies) == 1:
                del output['dimensions']
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
        model = ModelClient(emit)
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            await model.generate('teaching_report', '', {}, TeachingReport)
            assert 'dimensions' in bodies[1]['messages'][-1]['content']
            assert len(bodies) == 2
        finally:
            await model.close()
    asyncio.run(run())


def test_unused_reference_annotation_does_not_force_a_paid_retry(monkeypatch):
    import httpx
    from backend.llm import ModelClient
    from backend.teaching_report import TeachingReport
    monkeypatch.setattr(config, 'API_KEY', 'fake-key')
    async def run():
        calls = []
        async def emit(*args): pass
        def respond(request):
            calls.append(request)
            output = model_output()
            output['finding']['source_ids_note'] = '仅说明来源，无需展示'
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(output)}}]})
        model = ModelClient(emit)
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            report = await model.generate('teaching_report', '', {}, TeachingReport)
            assert len(calls) == 1
            assert 'source_ids_note' not in report.model_dump()['finding']
        finally: await model.close()
    asyncio.run(run())


@pytest.mark.parametrize('saved', ['{broken', json.dumps({'source_hash': 'bad'})])
def test_corrupt_cache_does_not_prevent_regeneration(tmp_path, monkeypatch, saved):
    from backend.teaching_report import ReportService
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    async def run():
        service = ReportService(lambda emit: FakeModel(emit, []))
        service.path(SID).write_text(saved, encoding='utf-8')
        assert service.get(SID)['status'] in ('idle', 'failed')
        await service.start(SID)
        await service.tasks[SID]
        assert service.get(SID)['status'] == 'ready'
    asyncio.run(run())


def test_prompt_change_does_not_reuse_old_report(tmp_path, monkeypatch):
    from backend import teaching_report as module
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    async def run():
        service = module.ReportService(lambda emit: FakeModel(emit, []))
        await service.start(SID)
        await service.tasks[SID]
        monkeypatch.setattr(module, 'REFLECTION_REPORT', module.REFLECTION_REPORT + '\n简化措辞。')
        assert service.get(SID)['status'] == 'idle'
    asyncio.run(run())


def test_old_report_is_preserved_and_reflection_uses_a_separate_cache(tmp_path, monkeypatch):
    from backend import teaching_report as module
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    old = tmp_path / 'logs' / f'{SID}.teaching-report.json'
    old.write_text('old report', encoding='utf-8')
    calls = []
    service = module.ReportService(lambda emit: FakeModel(emit, calls))
    async def run():
        assert service.get(SID)['status'] == 'idle'
        await service.start(SID)
        await service.tasks[SID]
        assert service.get(SID)['schema_version'] == 3
        assert old.read_text(encoding='utf-8') == 'old report'
        assert len(calls) == 1
    asyncio.run(run())


def test_reading_cached_report_removes_redundant_label_without_request_or_rewrite(tmp_path, monkeypatch):
    from backend.teaching_report import ReportService, source_hash
    save_classroom(tmp_path)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []
    path = tmp_path / 'logs' / f'{SID}.teaching-report-v2.json'
    result = model_output()
    original = result['finding']['text']
    result['finding']['text'] = result['finding']['kind'] + '：' + original
    path.write_text(json.dumps({
        'schema_version': 2, 'status': 'ready', 'report': result,
        'source_hash': source_hash(read_classroom(tmp_path, SID), legacy=True),
    }, ensure_ascii=False), encoding='utf-8')
    before = path.read_bytes()
    service = ReportService(lambda emit: FakeModel(emit, calls))
    restored = asyncio.run(service.start(SID))
    assert restored['schema_version'] == 2 and restored['status'] == 'ready'
    assert restored['report']['finding']['text'] == original
    assert not calls and path.read_bytes() == before and not service.path(SID).exists()
