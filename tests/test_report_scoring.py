import pytest
from pydantic import ValidationError

from backend.report_scoring import RubricAssessment, score_assessment


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
    assert '可靠教材' in result['accuracy']['reason']


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
