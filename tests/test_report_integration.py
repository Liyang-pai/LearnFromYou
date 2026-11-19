import json
from time import monotonic, sleep

import pytest
from fastapi.testclient import TestClient

from backend import app, config
from backend.history import HistoryService
from backend.teaching_report import ReportService, read_classroom, source_hash
from test_teaching_report import model_output, save_classroom


SID = '012345abcdef'
REPORT = f'/api/teaching-report/{SID}'
DIMENSIONS = {'accuracy', 'clarity', 'coherence', 'interaction', 'expression'}
RAW = '就是就是链表最后一格的指针指向空。'
REVIEWED = '链表尾节点的指针指向空。'
QUESTION = '就是沿 next 访问下一个节点。空链表需要访问任何节点吗？请说说理由。'
ANSWER = '就是不用访问。因为头指针一开始就是空。'
FEEDBACK = '对，头指针为空就没有可访问节点。'


def event(kind, data, elapsed=None):
    return {'type': kind, 'data': data, **({'elapsed': elapsed} if elapsed is not None else {})}


def ready():
    return event('ready', {'session_id': SID, 'lesson': {'topic': '链表遍历'}}, 0)


def finished(**changes):
    return event('finished', {
        'session_id': SID, 'lesson': {'topic': '链表遍历'}, 'log_file': f'logs/{SID}.jsonl',
        'state': {'open_questions': []}, 'unprocessed_sources': [], 'unreviewed_knowledge': [],
        **changes,
    }, 25)


def write_log(root, events):
    path = root / 'logs' / f'{SID}.jsonl'
    path.parent.mkdir(exist_ok=True)
    path.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in events), encoding='utf-8')
    return path


def client_for(root, monkeypatch, factory):
    monkeypatch.setattr(config, 'DATA_ROOT', root)
    monkeypatch.setattr(app, 'reports', ReportService(factory))
    monkeypatch.setattr(app, 'histories', HistoryService())
    return TestClient(app.app)


def reject_model(_):
    pytest.fail('读取、导出或处理没有可评材料的报告时，不应调用模型')


def wait_until_ready(client):
    deadline = monotonic() + 3
    while monotonic() < deadline:
        response = client.get(REPORT)
        assert response.status_code == 200, response.text
        result = response.json()
        if result['status'] != 'generating':
            assert result['status'] == 'ready', result
            return result
        sleep(.01)
    pytest.fail('离线模型未在预期时间内生成报告')


def test_report_route_exposes_reviewed_transcript_statistics_and_score_evidence(tmp_path, monkeypatch):
    path = write_log(tmp_path, [
        ready(),
        event('transcript', {'id': 't1', 'mode': 'microphone', 'text': RAW}, 0),
        event('review_completed', {'segments': [
            {'id': 't1', 'text': REVIEWED, 'review_status': 'reviewed'},
        ]}, 6.2),
        event('transcript', {'id': 't2', 'mode': 'text', 'text': QUESTION}),
        event('reply', {'kind': 'answer', 'text': ANSWER}, 18),
        event('transcript', {'id': 't3', 'mode': 'text', 'text': FEEDBACK}, 20.5),
        finished(),
    ])
    original = path.read_bytes()
    calls = []

    class Model:
        async def generate(self, phase, prompt, payload, contract):
            calls.append(phase)
            teacher = {'source_id': 't1', 'actor': 'teacher', 'quote': '链表尾节点的指针指向空。'}
            question = {'source_id': 't2', 'actor': 'teacher', 'quote': '空链表需要访问任何节点吗？请说说理由。'}
            response = {'source_id': 'r1', 'actor': 'ai', 'quote': '因为头指针一开始就是空。'}
            feedback = {'source_id': 't3', 'actor': 'teacher', 'quote': FEEDBACK}
            return contract.model_validate({
                'overview': {'text': '你讲解了链表节点的访问结束条件。', 'evidence': [teacher]},
                'keep': {'text': '保留提问后请小纸说明理由的做法。', 'evidence': [question, response]},
                'findings': [{
                    'kind': '可选提升', 'dimension': 'clarity', 'title': '对照空链表',
                    'fact': '你分别提到尾节点和空链表。', 'action': '用两张小图并排展示结束条件。',
                    'evidence': [teacher, feedback],
                }],
                'expression': None,
                'practice': {
                    'minutes': 2, 'task': '画出空链表和单节点链表。',
                    'example': '先圈出头指针，再标记停止的位置。',
                    'criterion': '教师能指出两种情形的停止条件。',
                },
                'assessment': {
                    'clarity': {'some_steps': [question], 'main_steps': [teacher, question]},
                    'coherence': {'identifiable_topics': [teacher], 'ordered_explanation': [teacher, question]},
                    'interaction': {
                        'simple_question_or_reply': [question], 'specific_question': [question],
                        'addresses_question': [feedback], 'understanding_question': [question],
                        'followup_or_feedback': [response, feedback], 'checks_reason': [question, response],
                        'specific_reason_feedback': [question, response, feedback],
                    },
                    'expression': {'partial_meaning': [teacher], 'main_meaning': [teacher]},
                },
            })

        async def close(self):
            pass

    with client_for(tmp_path, monkeypatch, lambda _: Model()) as client:
        assert client.get(REPORT).json()['status'] == 'idle'
        assert calls == []
        assert client.post(REPORT).status_code == 200
        result = wait_until_ready(client)
        assert result['schema_version'] == 3 and result['topic'] == '链表遍历'
        transcript = {turn['id']: turn for turn in result['transcript']}
        assert list(transcript) == ['t1', 't2', 'r1', 't3']
        assert transcript['t1']['original_text'] == RAW
        assert transcript['t1']['text'] == REVIEWED
        assert transcript['t1']['elapsed'] == 0
        assert transcript['t2'].get('elapsed') is None
        assert transcript['r1']['role'] == 'ai' and transcript['r1']['elapsed'] == 18
        assert transcript['t3']['role'] == 'teacher' and transcript['t3']['elapsed'] == 20.5
        assert {row['word']: row['count'] for row in result['word_counts']}['就是'] == 2
        assert set(result['scores']) == DIMENSIONS
        assert {key: item['score'] for key, item in result['scores'].items()} == {
            'accuracy': None, 'clarity': 3.0, 'coherence': 3.0, 'interaction': 5.0, 'expression': 3.0,
        }
        for dimension in DIMENSIONS - {'accuracy'}:
            assert result['scores'][dimension]['reason']
            assert result['scores'][dimension]['evidence']
        assessed = [result['report']['overview'], result['report']['keep'],
                    *result['report']['findings'], *result['scores'].values()]
        assert any(quote['actor'] == 'ai' for quote in result['scores']['interaction']['evidence'])
        for item in assessed:
            for quote in item['evidence']:
                turn = transcript[quote['source_id']]
                assert quote['actor'] == turn['role'] and quote['quote'] in turn['text']
        markdown = client.get(REPORT + '/export')
        assert markdown.status_code == 200 and '对照空链表' in markdown.text
        assert 'source_id' not in markdown.text and path.read_bytes() == original
        assert client.post(REPORT).json()['status'] == 'ready' and calls == ['teaching_report']

    with client_for(tmp_path, monkeypatch, reject_model) as restarted:
        assert restarted.get(REPORT).json() == result


@pytest.mark.parametrize(('before_finish', 'finish_changes', 'has_ready', 'expected'), [
    ([], {}, True, 'empty'),
    ([event('transcript', {'id': 't1', 'mode': 'text', 'text': '同学们好。'}, 4)], {}, True, 'empty'),
    ([event('audio_input', {'samples': 512, 'frames': 1}, 1)], {}, True, 'incomplete'),
    ([event('error', {'code': 'audio_backlog', 'message': '音频处理积压'}, 1)], {}, True, 'incomplete'),
    ([], {'unprocessed_sources': ['t1']}, True, 'incomplete'),
    ([], {}, False, 'incomplete'),
])
def test_finished_without_material_scores_one_only_when_capture_is_verified(
        tmp_path, monkeypatch, before_finish, finish_changes, has_ready, expected):
    path = write_log(tmp_path, [*([ready()] if has_ready else []), *before_finish, finished(**finish_changes)])
    original = path.read_bytes()
    with client_for(tmp_path, monkeypatch, reject_model) as client:
        for request in (client.get, client.post):
            response = request(REPORT)
            assert response.status_code == 200, response.text
            result = response.json()
            assert result['status'] == 'ready' and result['capture_status'] == expected
            assert result['report']['findings'] == []
            assert set(result['scores']) == DIMENSIONS
            target = 1.0 if expected == 'empty' else None
            assert all(row['score'] == target and not row['evidence'] for row in result['scores'].values())
        assert client.get(REPORT + '/export').status_code == 200
        assert path.read_bytes() == original


@pytest.mark.parametrize('state', ['missing', 'corrupt', 'unclosed'])
def test_unavailable_records_do_not_return_scores(tmp_path, monkeypatch, state):
    if state == 'unclosed':
        write_log(tmp_path, [ready()])
    if state == 'corrupt':
        path = write_log(tmp_path, [ready(), finished()])
        with path.open('a', encoding='utf-8') as file:
            file.write('\n{"type":')
    with client_for(tmp_path, monkeypatch, reject_model) as client:
        for request in (client.get, client.post):
            response = request(REPORT)
            assert response.status_code == (404 if state == 'missing' else 400)
            assert 'scores' not in response.json()


def test_legacy_cache_remains_readable_and_exportable_through_api(tmp_path, monkeypatch):
    save_classroom(tmp_path)
    path = tmp_path / 'logs' / f'{SID}.teaching-report-v2.json'
    path.write_text(json.dumps({
        'schema_version': 2, 'status': 'ready', 'report': model_output(),
        'source_hash': source_hash(read_classroom(tmp_path, SID), legacy=True),
    }, ensure_ascii=False), encoding='utf-8')
    original = path.read_bytes()
    with client_for(tmp_path, monkeypatch, reject_model) as client:
        result = client.get(REPORT).json()
        assert result['status'] == 'ready' and result['schema_version'] == 2
        assert result['report']['finding']['kind'] == '可选提升'
        assert '## 总体概括' in client.get(REPORT + '/export').text
    assert path.read_bytes() == original
