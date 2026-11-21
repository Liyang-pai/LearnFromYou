import asyncio
from copy import deepcopy
import json

import pytest

from backend import config
from backend.report_contract import ReflectionReport, source_bound
from backend.teaching_report import ReportService, export_markdown


TURNS = [
    {'id': 't1', 'role': 'teacher', 'mode': 'text', 'text': '((()))深度是多少？'},
    {'id': 'r1', 'role': 'ai', 'text': '最里层的空表深度是1，外面再套两层，所以是3。'},
    {'id': 't2', 'role': 'teacher', 'mode': 'text',
     'text': ('中间材料。' * 70) + '同层有多个子表时，取最大子表深度再加1。'},
    {'id': 't3', 'role': 'teacher', 'mode': 'microphone', 'review_status': 'reviewed',
     'text': '所以所以所以就是先保存外层结点，再进入子表。'},
]


def quote(source_id, text=None):
    turn = next(t for t in TURNS if t['id'] == source_id)
    return {'source_id': source_id, 'actor': turn['role'], 'quote': text or turn['text']}


def example_report():
    return {
        'overview': {'text': '你用例题问出了计算过程。', 'evidence': [quote('t1'), quote('r1')]},
        'keep': {'text': '保留先出题、再听计算理由的做法。', 'evidence': [quote('t1'), quote('r1')]},
        'findings': [{
            'kind': '可选提升', 'dimension': 'understanding_check', 'title': '把例题和通用规则连起来',
            'fact': '你先出题，小纸解释了算法，后来提供的材料写出了通用规则。',
            'action': '换题前补一句，取最深的子表再加一。',
            'evidence': [quote('t1'), quote('r1'), quote('t2', '取最大子表深度再加1。')],
        }],
        'expression': {'text': '讲子表时重复起头。', 'action': '先说保存外层结点。',
                       'evidence': [quote('t3', '所以所以所以就是')]},
        'practice': {'minutes': 2, 'task': '设计一道并排子表题。',
                     'example': '三个深度为1的子表并排，整张表深度为什么是2？',
                     'criterion': '教师能讲清取最大值与数个数的区别。'},
    }


def validate(report, turns=TURNS):
    return source_bound(ReflectionReport, {'t1', 't2', 't3'}, turns).model_validate(report)


def test_exact_evidence_from_later_in_a_long_source():
    report = validate(example_report())
    assert report.findings[0].evidence[-1].quote == '取最大子表深度再加1。'
    assert report.findings[0].evidence[1].actor == 'ai'


@pytest.mark.parametrize(('fault', 'error'), [
    ('wrong_role', '角色'), ('fabrication', '逐字'), ('mastery', '模拟'),
    ('duplicate', '重复'), ('typed_expression', '语音转写'), ('resolved', '已处理'),
    ('demonstration', '演示'), ('passivity', '被动'), ('practice', '独立完成'),
])
def test_teacher_feedback_rejects_unsupported_claims(fault, error):
    data, turns = example_report(), deepcopy(TURNS)
    finding = data['findings'][0]
    if fault == 'wrong_role':
        finding['evidence'][1]['actor'] = 'teacher'
    elif fault == 'fabrication':
        finding['evidence'][2]['quote'] = '编造的教师原话'
    elif fault == 'mastery':
        data['overview']['text'] = '学生已经掌握广义表深度。'
    elif fault == 'duplicate':
        data['findings'].append({**finding, 'title': '连接计算和规则'})
    elif fault == 'typed_expression':
        turns[-1]['mode'] = 'text'
    elif fault == 'resolved':
        turns[1]['question_status'] = 'resolved'
        finding['kind'] = '重点改进'
        finding['dimension'] = 'interaction'
        finding['fact'] = '教师一直没有澄清疑问。'
    elif fault == 'demonstration':
        data['overview']['text'] = '教师演示了如何计算深度。'
    elif fault == 'passivity':
        finding['fact'] = '教师直接归纳规则，学生只是被动接收。'
    elif fault == 'practice':
        data['practice']['criterion'] = '教师能让学生独立说出规则。'
    with pytest.raises(ValueError, match=error):
        validate(data, turns)


def test_extra_understanding_check_is_an_optional_improvement():
    data = example_report()
    data['findings'][0]['kind'] = '重点改进'
    assert validate(data).findings[0].kind == '可选提升'


@pytest.mark.parametrize('review_status', ['reviewed', 'disabled', 'fallback', 'pending', None])
@pytest.mark.parametrize('dimension', ['accuracy', 'clarity'])
def test_teacher_text_supports_primary_findings_independent_of_review_status(review_status, dimension):
    data, turns = example_report(), deepcopy(TURNS)
    turns[-1]['review_status'] = review_status
    finding = data['findings'][0]
    finding.update(kind='重点改进', dimension=dimension, evidence=[quote('t3', '所以所以所以就是')])
    assert validate(data, turns).findings[0].kind == '重点改进'


def test_reflection_service_shows_short_facts_and_exports_without_internal_sources(tmp_path, monkeypatch):
    sid = '012345abcdef'
    records = [
        {'type': 'transcript', 'data': turn} if turn['role'] == 'teacher'
        else {'type': 'reply', 'data': {'kind': 'answer', 'text': turn['text']}}
        for turn in TURNS
    ]
    records.append({'type': 'finished', 'data': {'session_id': sid, 'lesson': {'topic': '广义表'}}})
    logs = tmp_path / 'logs'
    logs.mkdir()
    path = logs / f'{sid}.jsonl'
    path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in records), encoding='utf-8')
    original = path.read_bytes()
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []

    class Model:
        async def generate(self, phase, prompt, payload, contract):
            calls.append((phase, prompt, payload))
            return contract.model_validate({**example_report(), 'assessment': {}})

        async def close(self):
            pass

    async def run():
        service = ReportService(lambda _: Model())
        await service.start(sid)
        await service.tasks[sid]
        return service.get(sid)

    result = asyncio.run(run())
    assert result['status'] == 'ready', result['error']
    assert result['topic'] == '广义表'
    assert len(calls) == 1 and '理解检查设计' in calls[0][1]
    report = result['report']
    assert report['expression']['quote'] == '所以所以所以就是'
    assert '后来提供的材料写出了通用规则' in report['findings'][0]['fact']
    assert report['findings'][0]['evidence']
    assert {row['id'] for row in result['transcript']} >= {'t1', 't2', 't3'}
    markdown = export_markdown(result)
    assert '课堂依据' in markdown and '下次试试' in markdown and 'source_ids' not in markdown
    assert path.read_bytes() == original
