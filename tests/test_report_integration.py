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
RAW = ('就是，就是，链表最后一格的指针指向空。尾节点就是最后一个节点。'
       '嗯，嗯，嗯，嗯。我们先想一下啊，我们先想一下啊，我们先想一下啊。'
       '那个，那个，那个，那个，那个。对吧，对吧。然后接着看。'
       '大家稍微等一下啊，大家稍微等一下啊，大家稍微等一下啊。')
REVIEWED = '链表尾节点的指针指向空。'
QUESTION = '就是沿 next 访问下一个节点。空链表需要访问任何节点吗？请说说理由。我们先看空链表。'
ANSWER = '就是不用访问。因为头指针一开始就是空。不用访问任何节点。'
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


@pytest.mark.parametrize('has_image', [False, True])
@pytest.mark.parametrize('empty_audio', [False, True])
def test_report_route_exposes_reviewed_transcript_statistics_and_score_evidence(tmp_path, monkeypatch, has_image, empty_audio):
    path = write_log(tmp_path, [
        ready(),
        event('transcript', {'id': 't1', 'mode': 'microphone', 'text': RAW}, 0),
        event('review_completed', {'segments': [
            {'id': 't1', 'text': REVIEWED, 'review_status': 'reviewed'},
        ]}, 6.2),
        event('transcript', {'id': 't2', 'mode': 'text', 'text': QUESTION,
                             **({'image_id': 'classroom-image'} if has_image else {})}),
        event('reply', {'kind': 'answer', 'text': ANSWER}, 18),
        event('transcript', {'id': 't3', 'mode': 'text', 'text': FEEDBACK}, 20.5),
        *([event('asr_empty', {'asr_seconds': .045}, 21)] if empty_audio else []),
        finished(),
    ])
    original = path.read_bytes()
    calls = []

    class Model:
        async def generate(self, phase, prompt, payload, contract):
            calls.append(phase)
            assert payload['lesson'].get('has_visual_material', False) is has_image
            assert not {'original_text', 'raw_text', 'review_status'} & payload['turns'][0].keys()
            assert payload['turns'][0]['text'] == REVIEWED
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
                'word_counts': [{'word': word, 'count': count} for word, count in [
                    ('空链表', 2), ('不用访问', 2), ('就是', 2), ('然后', 1), ('我们先想一下啊', 3),
                    ('那个', 5), ('嗯', 4), ('对吧', 2), (' 就是 ', 2),
                    ('大家稍微等一下啊', 3), ('链表最后', 30),
                ]],
                'practice': {
                    'minutes': 2, 'task': '画出空链表和单节点链表。',
                    'example': '先圈出头指针，再标记停止的位置。',
                    'criterion': '教师能指出两种情形的停止条件。',
                },
                'assessment': {
                    'accuracy': [{'topic': '尾节点指针', 'claim': REVIEWED, 'core': True,
                                  'verdict': 'correct', 'basis': '在线性单链表中，末节点的 next 为空。',
                                  'evidence': [teacher]}],
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
        assert result['capture_status'] == 'complete'
        assert result['transcription_notice'] == '语音转写可能存在误差，请结合实际讲授核对。'
        assert result['schema_version'] == 3 and result['topic'] == '链表遍历'
        transcript = {turn['id']: turn for turn in result['transcript']}
        assert list(transcript) == ['t1', 't2', 'r1', 't3']
        assert transcript['t1']['original_text'] == RAW
        assert transcript['t1']['text'] == REVIEWED
        assert transcript['t1']['elapsed'] == 0
        assert transcript['t2'].get('elapsed') is None
        assert transcript['r1']['role'] == 'ai' and transcript['r1']['elapsed'] == 18
        assert transcript['t3']['role'] == 'teacher' and transcript['t3']['elapsed'] == 20.5
        assert result['word_counts'] == []  # 原文、键盘和学生中的口头词不属于最终语音文本。
        assert 'word_counts' not in result['report']
        assert set(result['scores']) == DIMENSIONS
        assert {key: item['score'] for key, item in result['scores'].items()} == {
            'accuracy': 5.0, 'clarity': 3.0, 'coherence': 3.0, 'interaction': 5.0, 'expression': 3.0,
        }
        assert result['scores']['accuracy']['checks'][0]['evidence'][0]['quote'] == REVIEWED
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
        assert '内容准确核对' in markdown.text
        assert '学科依据　在线性单链表中，末节点的 next 为空。' in markdown.text
        assert f'课堂原话（教师）　“{REVIEWED}”' in markdown.text
        assert markdown.text.count(result['transcription_notice']) == 1
        assert 'source_id' not in markdown.text and path.read_bytes() == original
        assert client.post(REPORT).json()['status'] == 'ready' and calls == ['teaching_report']

    with client_for(tmp_path, monkeypatch, reject_model) as restarted:
        assert restarted.get(REPORT).json() == result
        cache = tmp_path / 'logs' / f'{SID}.teaching-report-reflection.json'
        saved = json.loads(cache.read_text(encoding='utf-8'))
        for counts in (None, []):
            saved['report']['word_counts'] = counts
            cache.write_text(json.dumps(saved, ensure_ascii=False), encoding='utf-8')
            assert restarted.get(REPORT).json()['word_counts'] == counts
        del saved['report']['word_counts']
        legacy_counts = [{'word': word, 'count': count} for word, count in [
            ('嗯', 4), ('就是', 3), ('这个', 0), ('呃', 0),
        ]]
        before_assessment = {key: value for key, value in saved['report'].items()
                             if key not in ('assessment', 'accuracy_coverage_limited')}
        for archived in (saved['report'], before_assessment):
            cache.write_text(json.dumps({**saved, 'report': archived}, ensure_ascii=False), encoding='utf-8')
            cached_bytes = cache.read_bytes()
            reopened = restarted.get(REPORT).json()
            assert reopened['status'] == 'ready' and reopened['word_counts'] == legacy_counts
            assert cache.read_bytes() == cached_bytes and path.read_bytes() == original


@pytest.mark.parametrize('incomplete', [False, True])
def test_accuracy_findings_use_checked_conclusions_and_survive_capture_gaps(tmp_path, monkeypatch, incomplete):
    wrong = '广义表的深度等于括号对数。'
    correct = '广义表的元素可以是原子或子表。'
    write_log(tmp_path, [
        ready(),
        event('transcript', {'id': 't1', 'mode': 'text', 'text': wrong}),
        event('transcript', {'id': 't2', 'mode': 'text', 'text': correct}),
        *([event('asr_empty', {})] if incomplete else []),
        finished(),
    ])
    evidence = [{'source_id': source, 'actor': 'teacher', 'quote': text}
                for source, text in [('t1', wrong), ('t2', correct)]]
    basis = '深度取最大嵌套层数。((a),(b)) 有三对括号，深度为二。'

    class Model:
        async def generate(self, phase, system, payload, contract):
            findings = [{'kind': '重点改进', 'dimension': 'accuracy', 'title': title,
                         'fact': '尝试改写原话。', 'action': '请参照其他规则。', 'evidence': [quote]}
                        for title, quote in zip(('核查深度', '错误声称子表不存在'), evidence)]
            return contract.model_validate({
                'overview': {'text': '这次讲了广义表。', 'evidence': [evidence[0]]},
                'keep': None, 'expression': None, 'findings': findings,
                'practice': {'minutes': 2, 'task': '练习深度。', 'example': '深度等于一半的括号数。',
                             'criterion': '教师能计算深度。'},
                'assessment': {'accuracy': [
                    {'topic': '深度', 'claim': wrong, 'core': True, 'verdict': 'core_error',
                     'basis': basis, 'evidence': [evidence[0]]},
                    {'topic': '元素', 'claim': correct, 'core': True, 'verdict': 'correct',
                     'basis': '按广义表定义，元素为原子或子表。', 'evidence': [evidence[1]]},
                ]},
            })

        async def close(self):
            pass

    with client_for(tmp_path, monkeypatch, lambda _: Model()) as client:
        assert client.post(REPORT).status_code == 200
        result = wait_until_ready(client)
        accuracy = result['scores']['accuracy']
        assert accuracy['score'] == 2.0
        assert result['capture_status'] == 'complete'
        assert result['scores']['interaction']['pending_label'] == '未见小纸回应'
        assert result['scores']['expression']['pending_label'] == '仅记录文字'
        assert len(accuracy['checks']) == 2
        assert len(result['report']['findings']) == 1
        finding = result['report']['findings'][0]
        assert finding['evidence'] == accuracy['checks'][0]['evidence'] == evidence[:1]
        assert accuracy['checks'][0]['basis'] == basis
        assert finding['action'] == result['report']['practice']['example'] == '重讲这一处时，说明正确结论和判断依据。' + basis
        markdown = client.get(REPORT + '/export')
        assert wrong in markdown.text and basis in markdown.text and '错误声称子表不存在' not in markdown.text
        assert '未见小纸回应' in markdown.text
        assert '资料待核对' not in markdown.text


def test_accuracy_finding_chooses_the_right_claim_when_two_checks_share_a_quote(tmp_path, monkeypatch):
    circle = '圆的面积等于二乘圆周率乘半径。'
    triangle = '平面三角形的内角和是三百六十度。'
    evidence = [{'source_id': 't1', 'actor': 'teacher', 'quote': circle + triangle}]
    write_log(tmp_path, [ready(), event('transcript', {'id': 't1', 'text': circle + triangle}), finished()])
    report = {
        'overview': {'text': '本次讲了圆和三角形。', 'evidence': evidence},
        'keep': None, 'expression': None,
        'findings': [{'kind': '重点改进', 'dimension': 'accuracy', 'title': '圆面积',
                      'fact': circle, 'action': '请重讲圆面积。', 'evidence': evidence}],
        'practice': {'minutes': 2, 'task': '练习圆面积。', 'example': '比较面积与周长。', 'criterion': '教师能区分公式。'},
        'assessment': {'accuracy': [
            {'topic': topic, 'claim': claim, 'basis': basis, 'core': True, 'verdict': 'core_error', 'evidence': evidence}
            for topic, claim, basis in [('圆面积', circle, '圆面积为 πr²。'),
                                        ('三角形内角和', triangle, '平面三角形内角和为 180 度。')]
        ]},
    }
    cache = tmp_path / 'logs' / f'{SID}.teaching-report-reflection.json'
    cache.write_text(json.dumps({'schema_version': 3, 'source_hash': source_hash(read_classroom(tmp_path, SID)),
                                 'status': 'ready', 'error': '', 'report': report}, ensure_ascii=False), encoding='utf-8')
    with client_for(tmp_path, monkeypatch, reject_model) as client:
        result = client.get(REPORT).json()
        assert result['scores']['accuracy']['score'] == 1.0
        assert result['report']['findings'][0]['action'] == '重讲这一处时，说明正确结论和判断依据。圆面积为 πr²。'
        assert result['report']['practice']['example'] == '重讲这一处时，说明正确结论和判断依据。圆面积为 πr²。'
        markdown = client.get(REPORT + '/export').text
        assert '学科依据　圆面积为 πr²。' in markdown and '学科依据　平面三角形内角和为 180 度。' in markdown


@pytest.mark.parametrize(('before_finish', 'finish_changes', 'has_ready', 'expected'), [
    ([], {}, True, 'empty'),
    ([event('transcript', {'id': 't1', 'mode': 'text', 'text': '同学们好。'}, 4)], {}, True, 'empty'),
    ([event('audio_input', {'samples': 512, 'frames': 1}, 1)], {}, True, 'incomplete'),
    ([event('error', {'code': 'audio_backlog', 'message': '音频处理积压'}, 1)], {}, True, 'incomplete'),
    ([], {'unprocessed_sources': ['t1']}, True, 'incomplete'),
    ([], {}, False, 'incomplete'),
])
def test_finished_without_material_leaves_all_scores_empty(
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
            assert all(row['score'] is None and not row['evidence'] for row in result['scores'].values())
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
