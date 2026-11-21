import pytest
from pydantic import ValidationError

from backend.report_scoring import RubricAssessment, score_assessment


def accuracy_check(source, quote, *, topic='广义表深度', verdict='correct', core=True):
    return {'topic': topic, 'claim': quote, 'core': core, 'verdict': verdict,
            'basis': '((a),(b)) 有三对括号，最大嵌套深度为二。',
            'evidence': [{'source_id': source, 'actor': 'teacher', 'quote': quote}]}


def test_accuracy_uses_the_final_statement_by_classroom_order():
    turns = [
        {'id': 't9', 'role': 'teacher', 'mode': 'text', 'text': '广义表的深度等于括号对数。'},
        {'id': 't10', 'role': 'teacher', 'mode': 'text', 'text': '更正，广义表的深度是最大嵌套层数。'},
        {'id': 't11', 'role': 'teacher', 'mode': 'text', 'text': '所以深度还是等于括号对数。'},
    ]
    wrong = accuracy_check('t9', turns[0]['text'], verdict='core_error')
    corrected = accuracy_check('t10', turns[1]['text'])
    repeated = accuracy_check('t11', turns[2]['text'], verdict='core_error')

    base = score_assessment(RubricAssessment(accuracy=[wrong]), turns[:1])['accuracy']
    assert base['score'] == 2.0
    unexplained = [turns[0], {**turns[1], 'text': '广义表的深度是最大嵌套层数。'}]
    inconsistent = score_assessment(
        RubricAssessment(accuracy=[wrong, accuracy_check('t10', unexplained[1]['text'])]), unexplained)['accuracy']
    assert inconsistent['score'] == 2.0 and not inconsistent['checks'][0]['corrected']
    corrected['corrects_earlier'] = True
    wrong['topic'] = '广义表深度（错误说法）'
    corrected['topic'] = '广义表深度（更正）'
    after = score_assessment(RubricAssessment(accuracy=[corrected, wrong]), turns[:2])['accuracy']
    assert after['score'] == 5.0
    assert after['checks'][0]['corrected'] is True
    assert [e['source_id'] for e in after['checks'][0]['evidence']] == ['t9', 't10']
    final = score_assessment(RubricAssessment(accuracy=[repeated, corrected, wrong]), turns)['accuracy']
    assert final['score'] == 2.0 and len(final['checks']) == 1
    assert final['checks'][0]['evidence'][0]['source_id'] == 't11'


def test_accuracy_marks_ambiguous_core_and_keeps_verified_teacher_evidence():
    t5 = '它的元素可以是单元素也可以是子表'
    t10 = '如果有多个括号比如说有有两个括号'
    turns = [
        {'id': 't5', 'role': 'teacher', 'text': t5, 'mode': 'microphone', 'review_status': 'disabled'},
        {'id': 't10', 'role': 'teacher', 'text': t10, 'mode': 'microphone', 'review_status': 'disabled'},
    ]
    definition = accuracy_check('t5', t5, topic='广义表元素')
    ambiguity = accuracy_check('t10', t10, verdict='unverified')
    result = score_assessment(RubricAssessment(accuracy=[definition, ambiguity]), turns)['accuracy']
    assert result['score'] is None
    assert result['pending_label'] == '已核对 1/2'
    assert [check['verdict'] for check in result['checks']] == ['correct', 'unverified']
    assert result['preview_check_index'] == 1
    assert '1/2' in result['reason']
    ambiguity['core'] = False
    result = score_assessment(RubricAssessment(accuracy=[definition, ambiguity]), turns)['accuracy']
    assert result['score'] == 5.0 and result['preview_check_index'] == 0


def test_accuracy_does_not_promote_an_ambiguous_bracket_example_to_a_rule():
    definition = '它的元素可以是单元素也可以是子表'
    ambiguous = '如果有多个括号比如说有有两个括号那么就意味着呃有两有两个这个呃呃这个广义表然后一个嵌在另一个里面'
    clarified = '有多个括号，比如两个括号，也要分清并列与嵌套，深度看最大嵌套层数。'

    def run(statement):
        turns = [{'id': source, 'role': 'teacher', 'text': text, 'mode': 'microphone', 'review_status': 'disabled'}
                 for source, text in [('t5', definition), ('t10', statement)]]
        assessments = [
            {**accuracy_check('t5', definition, topic='广义表元素'), 'basis': '广义表可容纳原子和子表。'},
            {**accuracy_check('t10', statement), 'basis': '括号表示嵌套，教师说法正确。'},
        ]
        return score_assessment(RubricAssessment(accuracy=assessments), turns)['accuracy']

    result = run(ambiguous)
    assert result['score'] is None
    assert [row['verdict'] for row in result['checks']] == ['correct', 'unverified']
    assert '并列' in result['checks'][1]['basis']
    assert run(clarified)['score'] == 5.0


@pytest.mark.parametrize(('texts', 'expected', 'fact'), [
    (['例如 L=(((a)),(b),(c,(d)))，所以这个广义表的深度是四。'], 3.0, '最大嵌套为3层'),
    (['例如 L=(((a)),(b),(c,(d)))，最深处有四层，所以这个广义表的深度是四。'], 3.0, '最大嵌套为3层'),
    (['例如 L=((a),(b))，L 的深度是三。再说一次，L 的深度是三。'], 3.0, '最大嵌套为2层'),
    (['看 L=((a),(b))。', '深度就是括号的总对数。这个式子有三对括号，所以它的深度是三。'],
     2.0, '最大嵌套为2层'),
    (['例如 L=(((a)),(b),(c,(d)))，所以这个广义表的深度是三。'], 5.0, None),
    (['设 C=(a,(b))。看 L=(C,a)，它的深度是三。'], 5.0, None),
])
def test_accuracy_recomputes_explicit_generalized_lists_even_when_model_agrees_with_teacher(texts, expected, fact):
    turns = [{'id': f't{i}', 'role': 'teacher', 'mode': 'text', 'text': text} for i, text in enumerate(texts, 1)]
    incorrect_model = accuracy_check(turns[-1]['id'], texts[-1])
    report = score_assessment(RubricAssessment(accuracy=[incorrect_model]), turns,
                              lesson={'topic': '广义表与深度'})['accuracy']
    assert report['score'] == expected
    if fact:
        assert fact in report['checks'][0]['basis']


@pytest.mark.parametrize('split_turns', [False, True])
@pytest.mark.parametrize(('next_example', 'quote'), [
    ('另一个例子是空表，空表的深度是一。', '空表的深度是一。'),
    ('再来看空表，它的深度是一。', '它的深度是一。'),
    ('也可以看看 (a)，这个广义表的深度是一。', '这个广义表的深度是一。'),
    ('第二题改用单层结构，这个广义表的深度是一。', '这个广义表的深度是一。'),
    ('B 表只有外层一对括号，深度是一。', '深度是一。'),
])
def test_other_list_example_is_not_recomputed_with_the_first_literal(split_turns, next_example, quote):
    first = '例如 L=((a),(b))，L 的深度是二。'
    texts = [first, next_example] if split_turns else [first + next_example]
    turns = [turn(f't{i}', text, mode='text') for i, text in enumerate(texts, 1)]
    later = accuracy_check(turns[-1]['id'], quote, topic='第二个例子')
    later['basis'] = '外层括号为一层。'
    assessment = RubricAssessment(accuracy=[accuracy_check('t1', first), later])
    result = score_assessment(assessment, turns, lesson={'topic': '广义表与深度'})['accuracy']
    assert result['score'] == 5.0
    assert '程序复算' in result['checks'][0]['basis']
    assert result['checks'][1]['basis'] == later['basis']


def test_accuracy_blocks_fabricated_support_and_simulated_answers():
    turns = [
        {'id': 't1', 'role': 'teacher', 'text': '你认为这里的深度是多少？'},
        {'id': 'r1', 'role': 'ai', 'text': '深度是二。'},
        {'id': 't2', 'role': 'teacher', 'text': '确实，答案是二。'},
        {'id': 't3', 'role': 'teacher', 'text': '深度等于括号对数。', 'mode': 'microphone', 'review_status': 'fallback'},
    ]
    answer = {'source_id': 'r1', 'actor': 'ai', 'quote': '深度是二。'}
    for judge in (
        accuracy_check('t1', '深度是二。'),
        {**accuracy_check('t1', turns[0]['text']), 'evidence': [answer]},
        {**accuracy_check('t1', turns[0]['text']), 'evidence': [answer, accuracy_check('t1', turns[0]['text'])['evidence'][0]]},
        accuracy_check('t3', turns[3]['text'], verdict='core_error'),
    ):
        accuracy = score_assessment(RubricAssessment(accuracy=[judge]), turns)['accuracy']
        assert accuracy['score'] is None and accuracy['checks'][0]['verdict'] == 'unverified'
    adopted = accuracy_check('t2', turns[2]['text'])
    adopted['evidence'].insert(0, answer)
    assert score_assessment(RubricAssessment(accuracy=[adopted]), turns)['accuracy']['score'] == 5.0


@pytest.mark.parametrize(('verdict', 'points'), [
    ('imprecise', 4.5), ('secondary_error', 4.0), ('key_error', 3.0), ('core_error', 2.0),
])
def test_accuracy_caps_use_the_worst_verified_defect(verdict, points):
    turns = [
        {'id': 't1', 'role': 'teacher', 'text': '广义表可以包含子表。'},
        {'id': 't2', 'role': 'teacher', 'text': '广义表的深度等于括号对数。'},
        {'id': 't3', 'role': 'teacher', 'text': '空表有一个元素。'},
    ]
    definition = accuracy_check('t1', turns[0]['text'], topic='广义表元素')
    issue = accuracy_check('t2', turns[1]['text'], verdict=verdict)
    duplicate = {**issue, 'topic': '括号深度'}
    result = score_assessment(RubricAssessment(accuracy=[definition, issue, duplicate]), turns)['accuracy']
    assert result['score'] == points
    assert result['checks'][result['preview_check_index']]['verdict'] == verdict
    if verdict == 'core_error':
        independent = accuracy_check('t3', turns[2]['text'], topic='空表长度', verdict='core_error')
        assert score_assessment(RubricAssessment(accuracy=[issue, duplicate, independent]), turns)['accuracy']['score'] == 1.0


def test_accuracy_keeps_the_verified_error_until_an_explicit_correction():
    wrong = '广义表的深度等于括号对数。'
    later = '同层有多个子表时需要看每个子表。'
    turns = [{'id': source, 'role': 'teacher', 'text': quote}
             for source, quote in [('t1', wrong), ('t2', later)]]
    old = accuracy_check('t1', wrong, verdict='core_error')
    imprecise = accuracy_check('t2', later, verdict='imprecise')
    result = score_assessment(RubricAssessment(accuracy=[old, imprecise]), turns)['accuracy']
    assert result['score'] == 2.0 and result['checks'][0]['evidence'][0]['source_id'] == 't1'
    pending = {**imprecise, 'verdict': 'unverified'}
    result = score_assessment(RubricAssessment(accuracy=[old, pending]), turns)['accuracy']
    assert result['score'] is None and '0/1' in result['reason']
    assert [row['verdict'] for row in result['checks']] == ['core_error', 'unverified']


def test_accuracy_does_not_count_paraphrases_of_the_same_error_twice():
    wrong = '广义表的深度等于括号对数。'
    turns = [{'id': key, 'role': 'teacher', 'text': wrong} for key in ('t1', 't2')]
    first = accuracy_check('t1', wrong, verdict='core_error')
    shorter = {**accuracy_check('t1', '深度等于括号对数', topic='数括号', verdict='core_error'), 'claim': '按对数'}
    repeated = {**accuracy_check('t2', wrong[:-1], topic='深度算法', verdict='core_error'), 'claim': '用总括号数'}
    result = score_assessment(RubricAssessment(accuracy=[first, shorter, repeated]), turns)['accuracy']
    assert result['score'] == 2.0
    assert len(result['checks']) == 1


@pytest.mark.parametrize('separator', ['。', '，以及', '以及'])
def test_independent_core_errors_can_share_a_classroom_quote(separator):
    circle = '圆的面积等于二乘圆周率乘半径'
    triangle = '三角形的内角和是三百六十度'
    whole = circle + separator + triangle + '。'
    result = score([turn('t1', whole, mode='text')], accuracy=[
        {**accuracy_check('t1', whole, topic='圆面积', verdict='core_error'),
         'claim': circle, 'basis': '圆的面积为 πr²。'},
        {**accuracy_check('t1', whole, topic='三角形内角和', verdict='core_error'),
         'claim': triangle, 'basis': '平面三角形内角和为 180 度。'},
    ])['accuracy']
    assert result['score'] == 1.0
    assert {row['claim'] for row in result['checks']} == {circle, triangle}


def test_repeated_core_error_with_number_separators_counts_once():
    wrong = '一千米等于 1,000,000 米。'
    result = score([turn('t1', wrong), turn('t2', wrong)], accuracy=[
        {**accuracy_check('t1', wrong, topic='米与千米的换算', verdict='core_error'), 'claim': '把千米换算为一百万米'},
        {**accuracy_check('t2', wrong, topic='长度单位换算', verdict='core_error'), 'claim': '千米转换为米时用错进率'},
    ])['accuracy']
    assert result['score'] == 2.0 and len(result['checks']) == 1


def test_accuracy_repeated_quote_within_turn_requires_unique_context():
    wrong = '深度等于括号对数。'
    correction = '我说错了，应取最大嵌套层数。'
    turns = [{'id': 't1', 'role': 'teacher', 'text': wrong + correction + wrong}]
    old = accuracy_check('t1', wrong, verdict='core_error')
    update = {**accuracy_check('t1', correction), 'corrects_earlier': True}
    assert score_assessment(RubricAssessment(accuracy=[old, update]), turns)['accuracy']['score'] is None


def test_accuracy_requires_reliable_wrong_statement_and_rejects_ai_only_error():
    wrong = '广义表的深度等于括号对数。'
    turns = [
        {'id': 't1', 'role': 'teacher', 'text': wrong, 'mode': 'microphone', 'review_status': 'disabled'},
        {'id': 'r1', 'role': 'ai', 'text': wrong},
        {'id': 't2', 'role': 'teacher', 'text': '你的说法不对，请再想想。', 'mode': 'text'},
    ]
    check = accuracy_check('t1', wrong, verdict='core_error')
    context = accuracy_check('t2', turns[-1]['text'])['evidence'][0]
    check['evidence'].append(context)
    reject = {**check, 'evidence': [{'source_id': 'r1', 'actor': 'ai', 'quote': wrong}, context]}
    for raw in (check, reject):
        assert score_assessment(RubricAssessment(accuracy=[raw]), turns)['accuracy']['score'] is None


def turn(source_id, text, *, kind=None, **metadata):
    return {'id': source_id, 'role': 'ai' if kind else 'teacher', 'text': text,
            **({'kind': kind} if kind else {}), **metadata}


def reference(row, quote=None):
    return {'source_id': row['id'], 'actor': row['role'], 'quote': quote or row['text']}


def score(turns, **assessment):
    return score_assessment(RubricAssessment.model_validate(assessment), turns)


@pytest.mark.parametrize('question, answer, feedback', [
    ('换个例子：(b,c) 的表尾是什么？说明理由。',
     '表尾是(c)，因为去掉了第一个元素b。',
     '去掉b这一步正确，记得剩余元素还要组成表，所以保留括号。'),
    ('拿(b,c)试试，怎么确定它的表尾？把你的判断过程讲一下。',
     '先删掉首元素b，再用余下的c组成表，得到(c)。',
     '你把剩余元素组成了表，这一步区分了原子c和表(c)。'),
])
def test_generalized_list_reason_and_feedback_raise_interaction(question, answer, feedback):
    length = turn('t16', '这张广义表的长度是多少？')
    depth = turn('t18', '那么((()))的深度是多少？')
    example = turn('r10', '我可以用tail(C)取出表尾。', kind='answer')
    transition = turn('t26', '我们接着看多层结构。')
    coding_doubt = turn('r12', '怎么从代码里看出它是原子还是表？', kind='question')
    followup = turn('t53', '先看tag的值，tag等于0时走哪个分支？')
    explanation = turn('t54', 'tag等于0时是原子，等于1时看子表。')
    turns = [length, depth, example, transition, coding_doubt, followup, explanation]
    observed = dict(simple_question_or_reply=[reference(length)], specific_question=[reference(depth)],
                    addresses_question=[reference(explanation)], understanding_question=[reference(depth)],
                    followup_or_feedback=[reference(coding_doubt), reference(followup)])

    before = score(turns, interaction=observed)['interaction']
    assert before['score'] == 4.0
    assert {item['source_id'] for item in before['evidence']} == {'t18', 'r12', 't53'}

    loop = [turn('t65', question), turn('r13', answer, kind='answer'), turn('t66', feedback)]
    with_check = score(turns[:4] + loop[:2] + turns[4:], interaction={
        **observed, 'checks_reason': [reference(row) for row in loop[:2]]})['interaction']
    assert with_check['score'] == 4.5
    assert '说明关键知识点的理由' in with_check['reason']

    with_feedback = score(turns[:4] + loop + turns[4:], interaction={
        **observed, 'checks_reason': [reference(row) for row in loop[:2]],
        'specific_reason_feedback': [reference(row) for row in loop]})['interaction']
    assert with_feedback['score'] == 5.0
    assert '具体反馈' in with_feedback['reason']
    assert [item['source_id'] for item in with_feedback['evidence']] == ['t65', 'r13', 't66']


@pytest.mark.parametrize('failure', ['role', 'quote', 'missing_id', 'unreviewed'])
def test_invalid_or_unreliable_evidence_does_not_produce_score(failure):
    explain = turn('t1', '先去掉第一个元素，再把剩余元素组成表。')
    anchor = reference(explain)
    if failure == 'role':
        anchor['actor'] = 'ai'
    elif failure == 'quote':
        anchor['quote'] = '先去掉最后一个元素'
    elif failure == 'missing_id':
        anchor['source_id'] = 't2'
    else:
        explain.update(mode='microphone', review_status='fallback')
    result = score([explain], clarity={'main_steps': [anchor]})
    assert result['clarity']['score'] is None
    assert result['clarity']['evidence'] == []
    assert result['accuracy']['score'] is None
    assert result['accuracy']['pending_label'] == '未核对'
    assert result['accuracy']['reason'] == '这份报告没有内容准确的核对记录。'


def test_valid_teacher_evidence_survives_ai_background_and_unrelated_invalid_observation():
    explain = turn('t1', '先去掉首元素，再把剩余元素组成表。')
    answer = turn('r1', '因此剩下(c)。', kind='answer')
    result = score([explain, answer], clarity={
        'main_steps': [reference(explain), reference(answer)], 'example_steps': [reference(answer)],
    })
    assert result['clarity']['score'] == 3.0
    assert result['clarity']['evidence'] == [reference(explain)]


def test_independent_question_uses_exact_excerpt_when_another_quote_contains_line_wrap_error():
    length = turn('t16', '我问你：\n这张广义表的长度是多少？')
    depth = turn('t18', '((()))深度是多少')
    reply = turn('t53', '先看栈里存的是什么。')
    result = score([length, depth, reply], interaction={
        'specific_question': [reference(length, '我问你：这张广义表的长度是多少？'), reference(depth)],
        'addresses_question': [reference(reply)], 'understanding_question': [reference(depth)],
    })
    assert result['interaction']['score'] == 3.5
    assert reference(depth) in result['interaction']['evidence']
    assert all(e['source_id'] != 't16' for e in result['interaction']['evidence'])


def test_only_next_tier_contributes_fraction_and_reason():
    explain = turn('t1', '先数最外层的元素得到长度，再沿嵌套最多的路径数层数得到深度。')
    example = turn('t2', '看(a,((b)))：外层有两个元素，最深处的(b)在第三层。')
    recap = turn('t3', '所以长度看最外层元素，深度看最大嵌套层数。')
    anchors = [reference(explain)]
    result = score([explain, example, recap], clarity={
        'some_steps': anchors, 'main_steps': anchors, 'example_steps': [reference(example)],
        'step_methods': anchors, 'decision_rule': [reference(recap)],
    }, coherence={'goal_recap': [reference(recap)]})
    assert result['clarity']['score'] == 4.5
    assert result['clarity']['reason'] == '用例子走完了关键步骤；讲清了关键步骤各怎么做；说明了如何区分容易混淆的情况。'
    assert result['coherence']['score'] is None


def test_coherence_and_expression_have_independent_tier_requirements():
    intro = turn('t1', '今天用广义表的元素来认识表头和表尾。')
    lesson = turn('t2', '先数最外层的元素。弄清元素后，再用它们识别表头和表尾。')
    recap = turn('t3', '回到今天的目标：用第一个元素确定表头，其余元素组成表尾。')
    anchors = [reference(intro), reference(recap)]
    result = score([intro, lesson, recap], coherence={
        'identifiable_topics': anchors, 'ordered_explanation': [reference(lesson)],
        'goal_alignment': anchors, 'transition_cue': [reference(lesson)], 'goal_recap': [reference(recap)],
    }, expression={
        'partial_meaning': [reference(recap)], 'main_meaning': [reference(recap)],
        'consistent_terms': anchors, 'direct_sentences': [reference(recap)],
    })
    assert result['coherence']['score'] == 4.5
    assert result['expression']['score'] == 4.0
    assert result['interaction']['score'] is None


def test_valid_higher_observation_supplies_only_implied_lower_criteria():
    question = turn('t1', '表尾是什么？请说一下判断依据。')
    answer = turn('r1', '首元素之后只有c，所以表尾是(c)。', kind='answer')
    feedback = turn('t2', '剩下的元素组成表，因此答案要保留括号。')
    turns = [question, answer, feedback]
    result = score(turns, interaction={'specific_reason_feedback': [reference(row) for row in turns]},
                   expression={'main_meaning': [reference(feedback)], 'direct_sentences': [reference(feedback)]})
    assert result['interaction']['score'] == 5.0
    assert [item['source_id'] for item in result['interaction']['evidence']] == ['t1', 'r1', 't2']
    assert result['expression']['score'] == 3.5


@pytest.mark.parametrize('failure', ['answer_before_question', 'unrelated_reply_in_between', 'different_round', 'ack',
                                     'asks_answer_only', 'explicitly_declines_reason'])
def test_reason_feedback_must_be_a_single_ordered_exchange(failure):
    question = turn('t1', '表尾是什么？你的依据是什么？')
    if failure == 'asks_answer_only':
        question['text'] = '表尾是什么？'
    elif failure == 'explicitly_declines_reason':
        question['text'] = '表尾是什么？不用解释原因。'
    answer = turn('r1', '去掉首元素后，用其余元素组成表。', kind='answer')
    feedback = turn('t2', '注意把余下元素组成表，不能直接写成原子。')
    turns = [question, answer, feedback]
    check = [reference(question), reference(answer)]
    feedback_check = [*check, reference(feedback)]
    if failure == 'answer_before_question':
        turns = [answer, question, feedback]
    elif failure == 'unrelated_reply_in_between':
        turns.insert(2, turn('r2', '嵌套是什么意思？', kind='question'))
    elif failure == 'different_round':
        other_question = turn('t3', '广义表的长度是多少？为什么？')
        other_answer = turn('r2', '长度是2，因为最外层有两个元素。', kind='answer')
        turns.extend([other_question, other_answer])
        check = [reference(other_question), reference(other_answer)]
    elif failure == 'ack':
        answer['kind'] = 'ack'
    assessed = score(turns, interaction={
        'simple_question_or_reply': [reference(question)],
        'checks_reason': check, 'specific_reason_feedback': feedback_check,
    })
    expected = 2.5 if failure in ('unrelated_reply_in_between', 'different_round') else 2.0
    assert assessed['interaction']['score'] == expected
    assert '具体反馈' not in assessed['interaction']['reason']


def test_low_observation_requires_usable_evidence_and_remains_an_actual_grade():
    intro = turn('t1', '大家好，今天先看到这里。', mode='microphone', review_status='reviewed')
    evidence = [reference(intro)]
    assessed = score([intro], coherence={'opening_only': evidence})
    assert assessed['coherence']['score'] == 1.0
    assert all(assessed[name]['score'] is None for name in ('accuracy', 'clarity', 'interaction', 'expression'))
    intro['review_status'] = 'disabled'
    assert score([intro], coherence={'opening_only': evidence})['coherence']['score'] is None


def test_no_evidence_results_in_pending_and_model_cannot_provide_score():
    assert all(entry['score'] is None for entry in score_assessment(None, []).values())
    with pytest.raises(ValidationError):
        RubricAssessment.model_validate({'clarity': {'score': 5.0}})
