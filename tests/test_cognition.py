"""验证认知状态、修订关系及普通课堂协议；语义分类效果另行实测。"""
import asyncio
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from backend.policy import apply_result
from backend.schemas import ClassroomEvent, Knowledge, StudentResult, StudentState
from backend.session import Session
from test_models import manager, install, FakeEngine


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


def test_classroom_payload_keeps_revision_sources_and_filters_current_knowledge():
    old = rule(text=SOURCES['t1']['text'], sources=['t1'])
    state, _ = apply_result(StudentState(knowledge=[old]), StudentResult(
        knowledge_updates=[rule(), rule(id='k_missing', status='unclear')],
        question_updates=[{'id': 'q1', 'topic': '旧规则', 'text': '规则是什么？',
                           'status': 'resolved', 'sources': ['t3'], 'resolution_sources': ['t2']}]),
        SOURCES, {'t2'})
    # t1 已不在最近八条中，也没有被当前知识直接引用，仍应通过修订历史保留。
    sources = {f't{i}': {'id': f't{i}', 'text': f'已采用的课堂内容{i}', 'mode': 'text',
                         'revision': i, 'at': i, 'raw_text': '不应发给学生的审核原文'}
               for i in range(1, 13)}
    session = SimpleNamespace(state=state, sources=sources, processed_ids=set(sources) - {'t12'},
                              preparation=SimpleNamespace(scope=['星星运算']),
                              lesson=SimpleNamespace(level='初学者'), prerequisites=[], history=[])
    before = state.model_dump()
    payload = Session.payload(session, [sources['t12']])
    assert payload['current_state']['knowledge'] == [rule().model_dump()]
    assert payload['current_state']['inactive_knowledge'][0]['id'] == 'k_missing'
    assert payload['current_state']['knowledge_history'][0]['previous'] == old.model_dump()
    assert payload['current_state']['open_questions'][0]['status'] == 'resolved'
    assert 't1' in {s['id'] for s in payload['teacher_sources']}
    assert payload['new_teacher_segments'][0]['id'] == 't12'
    assert all('raw_text' not in s for s in payload['teacher_sources'] + payload['new_teacher_segments'])
    assert state.model_dump() == before


@pytest.mark.parametrize('asr_review', [False, True])
def test_classroom_protocol_preserves_corrections_answers_and_finish(manager, monkeypatch, tmp_path, asr_review):
    from fastapi.testclient import TestClient
    from backend import app as service
    from backend.schemas import Preparation, ReviewGlossary

    phases = []

    class ClassroomModel:
        def __init__(self, emit):
            pass

        async def generate(self, phase, system, payload, output_type):
            phases.append(phase)
            if phase == 'preparation':
                return Preparation(scope=['星星运算'], boundary_note='仅从课堂学习')
            if phase == 'review_glossary':
                return ReviewGlossary(terms=['星星运算'])
            assert phase == 'student'
            source_id = payload['new_teacher_segments'][-1]['id']
            if source_id == 't1':
                return StudentResult(knowledge_updates=[rule(text=SOURCES['t1']['text'], sources=['t1'])])
            if source_id == 't2':
                return StudentResult(knowledge_updates=[rule()], addressed=True,
                    candidate={'kind': 'answer', 'text': '3加3再减1，得到5。', 'sources': ['t2']})
            assert payload['current_state']['knowledge'] == [rule().model_dump()]
            assert payload['current_state']['knowledge_history'][0]['previous']['sources'] == ['t1']
            return StudentResult(event_updates=[ClassroomEvent(
                id='e_end', kind='lesson_end', text='今天学习结束。', sources=[source_id])])

        async def close(self):
            pass

    def receive(ws, kind):
        for _ in range(40):
            event = ws.receive_json()
            assert event['type'] != 'error', event
            if event['type'] == kind:
                return event['data']
        pytest.fail('没有收到事件：' + kind)

    asyncio.run(install(manager))
    manager.select('first')
    monkeypatch.setattr(manager, '_create_engine', lambda model: FakeEngine(model.id))
    monkeypatch.setattr(service, 'models', manager)
    monkeypatch.setattr(service, 'active_session', None)
    monkeypatch.setattr('backend.session.ModelClient', ClassroomModel)
    monkeypatch.setattr('backend.config.DIRECT_PAUSE', 0)
    with TestClient(service.app) as client:
        page = client.get('/')
        assert page.status_code == 200
        assert 'assessment' not in page.text
        assert 'lessonUploadFile' in page.text and 'asrReview' in page.text
        monkeypatch.setattr('backend.config.ROOT', tmp_path)
        with client.websocket_connect('/ws/session') as ws:
            ws.send_json({'type': 'start', 'lesson': {'topic': '星星运算'},
                          'tts': False, 'asr_review': asr_review})
            ready = receive(ws, 'ready')
            assert 'assessment_available' not in ready
            assert ready['asr_review'] == asr_review
            assert ready['state']['open_questions'] == []
            ws.send_json({'type': 'text', 'text': SOURCES['t1']['text']})
            assert receive(ws, 'state')['state']['version'] == 1
            ws.send_json({'type': 'text', 'text': SOURCES['t2']['text'] + '请回答3星星3。'})
            corrected = receive(ws, 'state')['state']
            assert corrected['knowledge'] == [rule().model_dump()]
            assert corrected['knowledge_history'][0]['previous']['sources'] == ['t1']
            assert receive(ws, 'reply')['text'] == '3加3再减1，得到5。'
            ws.send_json({'type': 'text', 'text': SOURCES['t4']['text']})
            final = receive(ws, 'state')['state']
            assert final['recent_events'][0]['kind'] == 'lesson_end'
            assert len(final['knowledge']) == 1
            ws.send_json({'type': 'end'})
            finished = receive(ws, 'finished')
            assert finished['state'] == final and not finished['unprocessed_sources']
            assert 'assessments' not in finished
    assert phases == ['preparation'] + (['review_glossary'] if asr_review else []) + ['student'] * 3
    assert not manager.busy
