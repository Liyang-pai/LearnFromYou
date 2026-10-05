"""State contract/policy tests; semantic classification is separately tested live."""
import asyncio

import pytest
from pydantic import ValidationError

from backend.assessment import classroom_payload, assess
from backend.policy import apply_result
from backend.schemas import ClassroomEvent, Knowledge, StudentResult, StudentState, Lesson, Preparation, Question
from test_assessment import AssessmentModel, fixture_question, SOURCES as ASSESSMENT_SOURCES


SOURCES = {
    't1': {'text': '星星运算就是两个数直接相加。'},
    't2': {'text': '正确规则是两个数字相加以后再减1。'},
    't3': {'text': '3星星3应该怎么算？'},
    't4': {'text': '今天的星星运算学习到这里结束。'},
}


def rule(id='k_star', text='两个数字相加以后再减1。', status='understood', sources=None, **extra):
    return Knowledge(id=id, text=text, status=status, sources=sources or ['t2'], **extra)


def test_a_real_knowledge_enters_knowledge():
    state, _ = apply_result(StudentState(), StudentResult(knowledge_updates=[rule()]), SOURCES, {'t2'})
    assert state.knowledge == [rule()]
    assert not state.open_questions and not state.recent_events


def test_b_teacher_question_is_a_request_not_a_fact():
    result = StudentResult(event_updates=[ClassroomEvent(id='e_question', kind='request', text=SOURCES['t3']['text'], sources=['t3'])])
    state, _ = apply_result(StudentState(), result, SOURCES, {'t3'})
    assert not state.knowledge
    assert state.recent_events[0].kind == 'request'


def test_question_lifecycle_uses_canonical_open_questions_with_old_input_compatibility():
    update = {'id':'q1', 'topic':'规则缺失', 'text':'还需要减1吗？', 'status':'pending', 'sources':['t3']}
    state, _ = apply_result(StudentState(), StudentResult(question_updates=[update]), SOURCES, {'t3'})
    assert not state.knowledge
    assert state.open_questions == state.questions
    assert 'questions' not in state.model_dump()
    assert StudentState.model_validate({'questions':state.model_dump()['open_questions']}).open_questions == state.open_questions


def test_c_lesson_end_is_event_not_knowledge():
    result = StudentResult(event_updates=[ClassroomEvent(id='e_end', kind='lesson_end', text=SOURCES['t4']['text'], sources=['t4'])])
    state, _ = apply_result(StudentState(), result, SOURCES, {'t4'})
    assert not state.knowledge
    assert state.recent_events[0].kind == 'lesson_end'


def test_event_window_keeps_twenty_most_recent_and_does_not_duplicate_ids():
    state = StudentState()
    for i in range(25):
        event = ClassroomEvent(id=f'e{i}', kind='task', text=f'观察任务{i}', sources=['t3'])
        state, _ = apply_result(state, StudentResult(event_updates=[event]), SOURCES, {'t3'})
    assert len(state.recent_events) == 20 and state.recent_events[0].id == 'e5'
    event = state.recent_events[0]
    state, _ = apply_result(state, StudentResult(event_updates=[event]), SOURCES, {'t3'})
    assert len(state.recent_events) == 20 and state.recent_events[-1].id == 'e5'


@pytest.mark.parametrize('same_id', [True, False])
def test_d_correction_retains_history_but_old_rule_is_not_current(same_id):
    old = rule(text=SOURCES['t1']['text'], sources=['t1'])
    original = StudentState(knowledge=[old])
    corrected = rule(id='k_star' if same_id else 'k_new', supersedes=[] if same_id else ['k_star'])
    state, _ = apply_result(original, StudentResult(knowledge_updates=[corrected]), SOURCES, {'t2'})
    assert original.knowledge == [old] and not original.knowledge_history
    assert any(h.previous.text == old.text for h in state.knowledge_history)
    assert state.learning_context()['knowledge'] == [corrected.model_dump()]
    if not same_id:
        assert state.knowledge[0].status == 'conflict'
    assert classroom_payload(state, [])['current_state']['knowledge'] == [corrected.model_dump()]


def test_incomplete_correction_does_not_require_an_invented_new_rule():
    old = rule(text=SOURCES['t1']['text'], sources=['t1'])
    conflict = old.model_copy(update={'status':'conflict', 'sources':['t2']})
    state, _ = apply_result(StudentState(knowledge=[old]), StudentResult(knowledge_updates=[conflict]), SOURCES, {'t2'})
    assert state.learning_context()['knowledge'] == []
    assert state.knowledge_history[0].previous == old


def test_e_verified_is_not_a_model_writable_status():
    with pytest.raises(ValidationError):
        rule(status='verified')
    state, _ = apply_result(StudentState(), StudentResult(knowledge_updates=[rule()]), SOURCES, {'t2'})
    assert state.knowledge[0].status == 'understood'


def test_passed_assessment_does_not_guess_knowledge_mapping():
    state = StudentState(knowledge=[rule()])
    before = state.model_dump()
    from backend.schemas import Lesson
    _, evaluation = asyncio.run(assess(AssessmentModel(), fixture_question(), Lesson(topic='链表'), state, ASSESSMENT_SOURCES))
    assert evaluation.verdict == 'passed'
    assert state.model_dump() == before


def test_bad_event_source_rejects_whole_result_atomically():
    original = StudentState()
    result = StudentResult(knowledge_updates=[rule()], event_updates=[ClassroomEvent(id='e_bad', kind='task', text='观察', sources=['t_missing'])])
    with pytest.raises(ValueError):
        apply_result(original, result, SOURCES, {'t2'})
    assert original.model_dump() == StudentState().model_dump()


def test_invalid_correction_reference_rejected():
    with pytest.raises(ValueError):
        apply_result(StudentState(), StudentResult(knowledge_updates=[rule(supersedes=['missing'])]), SOURCES, {'t2'})


def test_same_id_self_reference_is_normalized_without_losing_history():
    old = rule(text=SOURCES['t1']['text'], sources=['t1'])
    state, _ = apply_result(StudentState(knowledge=[old]), StudentResult(
        knowledge_updates=[rule(supersedes=['k_star'])]), SOURCES, {'t2'})
    assert state.knowledge[0].supersedes == []
    assert state.knowledge_history[0].previous == old
    assert len(state.learning_context()['knowledge']) == 1


def test_self_reference_cannot_invent_an_existing_knowledge():
    with pytest.raises(ValueError):
        apply_result(StudentState(), StudentResult(knowledge_updates=[rule(supersedes=['k_star'])]), SOURCES, {'t2'})


@pytest.mark.parametrize('number',[6,9,12])
def test_blue_box_answer_context_preserves_boundaries_without_blanket_refusal(number):
    """Contract test, not a fake model pretending to prove natural-language compliance."""
    from backend import prompts
    from backend.assessment import ANSWER_PROMPT
    from backend.session import Session
    session=Session(None,None)
    session.lesson=Lesson(topic='蓝盒规则')
    session.preparation=Preparation(scope=['蓝盒规则'],boundary_note='只按课堂知识')
    session.state=StudentState(knowledge=[
        Knowledge(id='rule',text='偶数先乘二再加三，奇数只乘二。',status='tentative',sources=['t1']),
        Knowledge(id='boundary',text='输入大于10时规则变化，特殊规则尚未教授。',status='tentative',sources=['t2']),
        Knowledge(id='unknown',text='特殊规则具体内容未知。',status='unclear',sources=['t2'])],
        open_questions=[Question(id='q1',topic='特殊规则',text='特殊规则是什么？',status='pending',sources=['t2'])])
    session.sources={'t1':{'id':'t1','text':session.state.knowledge[0].text},
                     't2':{'id':'t2','text':session.state.knowledge[1].text}}
    session.processed_ids={'t1','t2'}
    batch=[{'id':'t3','text':f'蓝盒({number})是多少？'}]
    try:
        payload=session.payload(batch)
        for context in (payload,classroom_payload(session.state,list(session.sources.values()))):
            assert context['current_state']['knowledge'][1]['id']=='boundary'
            assert context['current_state']['inactive_knowledge'][0]['id']=='unknown'
            assert context['current_state']['open_questions'][0]['status']=='pending'
            assert any(s['id']=='t2' for s in context['teacher_sources'])
        assert payload['new_teacher_segments']==batch
        for prompt in (prompts.STUDENT,ANSWER_PROMPT):
            assert prompts.ANSWER_BOUNDARIES in prompt
            assert '输入6、9应正常' in prompt and '输入12不能外推' in prompt
            assert '只有与本题必要推理直接相关的缺口' in prompt
            assert '阈值、否定或必要条件被转写遗漏' in prompt
            assert '不能用示例中的阈值10补齐课堂未知的阈值' in prompt
            assert '不能断言本题已经超出某个未确定的阈值' in prompt
            assert '不得先算出旧规则的数值答案' in prompt
    finally:asyncio.run(session.llm.close())
