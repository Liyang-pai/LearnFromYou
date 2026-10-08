"""真实体验发现的问题：离线专项回归，不连接模型，不依赖私人日志。"""
from copy import deepcopy
import pytest

from backend.report_data import build_snapshot
from backend.report_v2 import validate_verification


def question_classroom():
    """Minimal literal excerpts from the real acceptance: question-kind q1, answer-kind q2."""
    questions = [
        {'id': 'q1', 'topic': '元组不可变与重新赋值', 'text': '这两个改有什么区别？', 'status': 'resolved', 'sources': ['t1'], 'resolution_sources': ['t2']},
        {'id': 'q2', 'topic': '单元素元组逗号的作用', 'text': '为什么这个逗号重要？', 'status': 'deferred', 'sources': ['t3', 't4'], 'resolution_sources': []}]
    events = []
    def add(kind, data):
        events.append({'id': SID + f':e{len(events)+1:06d}', 'type': kind, 'elapsed': len(events), 'data': data})
    add('ready', {'prerequisites': []})
    add('transcript', {'id':'t1', 'text':'先不急着记结论，这里哪一点让你困惑？', 'question_count':1})
    add('reply', {'kind':'question', 'question_id':'q1', 'text':'我有点困惑：这两个改有什么区别？'})
    add('transcript', {'id':'t2', 'text':TEACHER, 'question_count':0})
    add('transcript', {'id':'t3', 'text':'单元素元组要写成 (7,)，为什么有逗号今天先不展开。', 'question_count':0})
    add('reply', {'kind':'answer', 'question_id':None, 'text':'我目前能理解列表和元组怎么创建。刚听到单元素元组要写成 (7,) 而不是 (7)，但为什么这个逗号重要，我还没弄明白。'})
    add('transcript', {'id':'t4', 'text':'你关于单元素元组逗号的疑问先记下来，下节再用代码对比解释，今天不要求你说自己已经理解。', 'question_count':0})
    add('finished', {'session_id':SID, 'state':{'knowledge':[], 'questions':questions}, 'unprocessed_sources':[]})
    return events


def test_real_acceptance_question_counts_and_deferred_response():
    snapshot = build_snapshot(SID, question_classroom())
    values = {k:v['value'] for k,v in snapshot['settlement']['metrics'].items()}
    assert {k:values[k] for k in ('student_questions','question_occurrences','responded_questions','explained_questions','unanswered_questions','resolved_questions','unresolved_questions')} == dict(
        student_questions=2, question_occurrences=2, responded_questions=2, explained_questions=1, unanswered_questions=0, resolved_questions=1, unresolved_questions=1)
    q2 = snapshot['settlement']['questions'][1]
    assert q2['expressed'] and q2['response_sources'] == ['t4'] and q2['explanation_sources'] == []


def test_duplicate_question_events_and_unknown_explicit_doubt():
    events = question_classroom()
    events.insert(-1, {'id':SID+':e000009', 'type':'reply', 'elapsed':8, 'data':{'kind':'answer', 'question_id':'q2', 'text':'逗号的问题我还是不理解。'}})
    snapshot = build_snapshot(SID, events + [deepcopy(events[2])])
    assert snapshot['settlement']['metrics']['student_questions']['value'] == 2
    assert snapshot['settlement']['metrics']['question_occurrences']['value'] == 3
    events.insert(-1, {'id':SID+':e000010', 'type':'reply', 'elapsed':9, 'data':{'kind':'answer', 'text':'为什么地球是圆的？'}})
    snapshot = build_snapshot(SID, events)
    assert snapshot['settlement']['metrics']['student_questions']['value'] is None
    assert snapshot['settlement']['unmatched_questions']


def test_cognitive_conflict_is_not_unsupported_weakness():
    from backend.report_v2 import validate_diagnosis
    snapshot = build_snapshot(SID, question_classroom())
    def cite(n): return {'event_id':SID+f':e{n:06d}', 'quote':snapshot['evidence'][SID+f':e{n:06d}']['text']}
    conflict = {'id':'f1', 'observation':'教师引出元组不可变与重新赋值的困惑。', 'interpretation':'学生困惑，所以教学失败。', 'citations':[cite(2),cite(3)]}
    missing = {'id':'f2', 'observation':'单元素元组逗号没有解释，学生仍不明白。', 'interpretation':'疑问暂缓，课堂未解决。', 'citations':[cite(5),cite(6),cite(7)]}
    diagnosis = {'strengths':[{**conflict,'id':'f3','interpretation':'提问暴露疑问，随后教师解释。'}], 'weaknesses':[conflict,missing], 'suggestions':[{'finding_id':'f1','action':'少提问。'}, {'finding_id':'f2','action':'下次解释逗号。'}]}
    validate_diagnosis(diagnosis, snapshot)
    assert [f['id'] for f in diagnosis['weaknesses']] == ['f2']
    assert [s['finding_id'] for s in diagnosis['suggestions']] == ['f2']
    assert len(diagnosis['strengths']) == 1


def test_natural_recall_preserves_real_doubt_but_labels_inference():
    from backend.report_v2 import naturalize_recall
    snapshot = build_snapshot(SID, question_classroom())
    plan = {'explained':[{'text':'我能解释下标。'}, {'text':'我能解释列表。'}], 'doubts':[{'text':'我对逗号仍有疑问。', 'question_ids':['q2']}, {'text':'我还不确定 tentative 是否适合所有情况。', 'question_ids':[]}], 'uncertain':[]}
    naturalize_recall(plan, snapshot)
    assert len(plan['doubts']) == 1 and plan['doubts'][0]['question_ids'] == ['q2']
    assert 'tentative' not in str(plan) and '我能解释' not in str(plan)
    assert '不能确认' in plan['uncertain'][0]['text']


def test_answer_referring_to_question_is_not_another_doubt():
    events = question_classroom()
    events.insert(-1, {'id':SID+':e000009','type':'reply','elapsed':8,'data':{'kind':'answer','question_id':'q1','text':'我理解了，重新赋值改变变量指向。'}})
    snapshot = build_snapshot(SID, events)
    assert snapshot['settlement']['metrics']['question_occurrences']['value'] == 2


def test_only_quoting_a_known_rule_is_not_new_reasoning_evidence():
    text = '说法不成立，原来的元组并没有被改动，但后一步改了内部元素。'
    plan, answers, result = verification_case(text, quote='原来的元组并没有被改动')
    validate_verification(result, plan, answers, snapshot_for())
    assert result['results'][0]['status'] == '尚未验证'


def test_negative_angle_requires_confusion_after_explanation():
    from backend.report_v2 import validate_diagnosis
    events = question_classroom()
    events.insert(-1, {'id':SID+':e000009','type':'reply','elapsed':8,'data':{'kind':'question','question_id':'q1','text':'元组不可变与重新赋值的区别我还是不理解。'}})
    snapshot = build_snapshot(SID, events)
    finding = {'id':'f1','observation':'解释后，学生仍不理解元组不可变与重新赋值的区别。','interpretation':'系统解决标记与后续学生表达有冲突，需要再澄清。','citations':[{'event_id':SID+':e000004','quote':TEACHER},{'event_id':SID+':e000009','quote':events[-2]['data']['text']}]}
    diagnosis = {'strengths':[], 'weaknesses':[finding], 'suggestions':[]}
    validate_diagnosis(diagnosis, snapshot)
    assert diagnosis['weaknesses'] == [finding]


def test_legacy_cache_projection_keeps_original_file_and_export_consistent(tmp_path, monkeypatch):
    import json
    from backend import config
    from backend.report_v2 import ReportService, markdown
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    snapshot = build_snapshot(SID, question_classroom())
    snapshot['settlement']['metrics']['student_questions']['value'] = 1
    plan, answers, result = verification_case(TEACHER)
    result['results'][0].pop('task_checks'); result['results'][0].pop('reasoning_check')
    # Use matching evidence for the legacy probe instead of a different classroom source.
    eid = SID+':e000004'
    for item in plan['probes']+answers['answers']+result['results']: item['citations']=[{'event_id':eid,'quote':TEACHER}]
    view = {'snapshot':snapshot, 'status':'ready', 'recall':{'explained':[],'doubts':[],'uncertain':[],'probes':plan['probes']},'answers':answers,'verification':result,'diagnosis':None,'stages':{}}
    path = tmp_path/'logs'/f'{SID}.report-v2.json'; path.parent.mkdir(); path.write_text(json.dumps(view),encoding='utf-8')
    raw = path.read_bytes(); projected = ReportService().get(SID)
    assert path.read_bytes() == raw
    assert projected['snapshot']['source_hash'] == snapshot['source_hash']
    assert projected['snapshot']['settlement']['metrics']['student_questions']['value'] == 2
    assert projected['verification']['results'][0]['status'] == '尚未验证'
    exported = markdown(projected)
    assert '学生独立疑问数量：2' in exported and '暂缓处理，尚未解决' in exported and '结论：尚未验证' in exported

SID = 'aabbcc112233'
TEACHER = ('point[0] = 4 是想改原来那个元组里面第 0 个位置，所以不允许。'
           'point = (4, 5) 则是先得到一个新的元组，再让变量 point 指向这个新元组，原来的元组并没有被改动。'
           '可以想成给盒子换个标签指向另一个盒子，而不是打开原来的盒子换东西。')


def snapshot_for(text=TEACHER):
    state = {'knowledge': [{'id': 'k1', 'text': text, 'status': 'understood', 'sources': ['t1']}], 'questions': []}
    events = [
        {'id': SID+':e1', 'type': 'ready', 'elapsed': 0, 'data': {'prerequisites': []}},
        {'id': SID+':e2', 'type': 'transcript', 'elapsed': 1,
         'data': {'id': 't1', 'text': text, 'mode': 'text', 'question_count': 0}},
        {'id': SID+':e3', 'type': 'finished', 'elapsed': 3,
         'data': {'session_id': SID, 'state': state, 'unprocessed_sources': []}},
    ]
    return build_snapshot(SID, events)


def verification_case(text, outcome='有依据地完成', kind='新情境推理', *, quote=None):
    citation = {'event_id': SID+':e2', 'quote': TEACHER}
    task = '判断这个说法是否成立并说明理由'
    plan = {'probes': [{'id': 'v1', 'knowledge_id': 'k1',
            'question': '先 a = (1, 2)，再 a = (3, 4)，最后 a[0] = 5。同学说后两步都不允许，请判断这个说法是否成立并说明理由。',
            'task_kind': '发现错误', 'required_tasks': [task], 'citations': [citation]}]}
    answers = {'answers': [{'id': 'v1', 'text': text, 'citations': [citation]}]}
    result = {'results': [{'id': 'v1', 'status': '有理解证据', 'reasoning': '解释或应用',
        'explanation': '模型最初给出积极结论。', 'citations': [citation],
        'task_checks': [{'task': task, 'outcome': outcome, 'answer_quote': text,
                         'explanation': '检查是否判断了两步操作，并依据课堂规则解释。', 'citations': [citation]}],
        'reasoning_check': {'kind': kind, 'answer_quote': text if quote is None else quote,
                            'explanation': '检查新判断的实际推理。', 'citations': [citation],
                            'comparison': {'kind':'实质任务变化','difference':'教师分别说明操作，题目需要识别混合操作的错误说法并作对比判断。',
                                           'answer_quote':text,'citations':[citation],
                                           'teacher_steps':['区分操作对象'], 'task_steps':['区分操作对象','发现矛盾'],
                                           'task_quote':task}}}]}
    return plan, answers, result


@pytest.mark.parametrize('text,outcome,kind,expected', [
    (TEACHER, '有依据地完成', '新情境推理', '尚未验证'),
    ('location = (3, 4) 是先得到一个新的元组，再让变量 location 指向这个新元组，原来的元组并没有被改动。',
     '有依据地完成', '新情境推理', '尚未验证'),
    ('两种容器各有特点，我现在比刚才更清楚了。', '未回答', '无法判断', '尚未验证'),
    ('这个说法不成立。前一次是重新给变量赋值，允许；后一次要替换元组里面的元素，不允许。因此不能把这两步都说成不允许。',
     '有依据地完成', '新情境推理', '有理解证据'),
    ('说法不成立，因为元组里面的元素可以随便修改。', '理由错误', '新情境推理', '存在误解'),
    ('重新赋值是允许的，最后一步我还不确定。', '部分完成', '无法判断', '尚未验证'),
    ('需要的规则还没有讲，我无法判断。', '无法判断', '无法判断', '证据不足'),
])
def test_verdict_requires_task_completion_and_supported_reasoning(text, outcome, kind, expected):
    plan, answers, result = verification_case(text, outcome, kind)
    validate_verification(result, plan, answers, snapshot_for())
    assert result['results'][0]['status'] == expected


def test_overconfident_evaluator_cannot_invent_reasoning_quote():
    plan, answers, result = verification_case('我觉得老师说得清楚。', quote='前一次重新赋值允许，后一次改元素不允许。')
    validate_verification(result, plan, answers, snapshot_for())
    assert result['results'][0]['status'] == '证据不足'


def test_correct_answer_without_classroom_support_is_not_proof():
    plan, answers, result = verification_case('前一次重新赋值允许，后一次改元素不允许。')
    result['results'][0]['task_checks'][0]['citations'] = []
    validate_verification(result, plan, answers, snapshot_for())
    assert result['results'][0]['status'] == '证据不足'


def test_old_positive_verdict_without_task_checks_is_conservative():
    plan, answers, result = verification_case('重新赋值允许。')
    result['results'][0].pop('task_checks')
    result['results'][0].pop('reasoning_check')
    validate_verification(result, plan, answers, snapshot_for())
    assert result['results'][0]['status'] == '证据不足'


def test_similar_rule_wording_with_new_decision_is_allowed():
    text = ('第一次让变量指向新元组，原来的元组并没有被改动。'
            '但第二次针对 a[0]，要改的是元组内部位置，所以这一步不允许；同学把两种动作混为一谈了。')
    plan, answers, result = verification_case(text)
    validate_verification(result, plan, answers, snapshot_for())
    assert result['results'][0]['status'] == '有理解证据'
