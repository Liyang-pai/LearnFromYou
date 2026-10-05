import asyncio
from copy import deepcopy

import pytest

from backend.assessment import assess, generate_question, AssessmentQuestion, same_question
from backend.schemas import (AssessmentAnswer, AssessmentEvaluation, Lesson, StudentState,
                             GeneratedAssessment, AssessmentAudit, AssessmentCriterion)
from backend.session import Session
from backend.llm import ModelError
from test_models import manager, install, FakeEngine


SOURCES = [{"id": "t1", "text": "从头节点开始沿引用访问，引用为空时停止。"}]


class AssessmentModel:
    def __init__(self, coverage="sufficient", evidence=None, answer_sources=None):
        self.calls = []
        self.generated = 0
        self.coverage = coverage
        self.evidence = evidence if evidence is not None else [{"source_id": "t1", "quote": "从头节点开始沿引用访问"}]
        self.answer_sources = answer_sources if answer_sources is not None else ["t1"]

    async def generate(self, phase, system, payload, output_type):
        self.calls.append(deepcopy(payload))
        if phase == "assessment_question":
            self.generated += 1
            scenarios = ['A 引用 C，C 引用 B，B 引用为空。从头节点 A 访问哪些数据？',
                         '书架按引用连接，起点在中间一格。请说明怎样依次读取书名，以及停止条件。',
                         '配送站各有一个下一站地址，如何按给定起点访问包裹标签？']
            return GeneratedAssessment(status='ready', question=scenarios[(self.generated - 1) % 3],
                learning_target='沿引用访问', reference_answer='沿引用访问直到空引用。',
                criteria=[AssessmentCriterion(text='解释访问顺序与停止条件', evidence=[{'source_id':'t1','quote':SOURCES[0]['text']}])])
        if phase == "assessment_audit":
            return AssessmentAudit(verdict='supported', reason='规则均有明确课堂依据。')
        if phase == "assessment_answer":
            return AssessmentAnswer(text="8、5、3，在 B 处停止。", sources=self.answer_sources)
        return AssessmentEvaluation(verdict="passed", coverage=self.coverage,
                                    explanation="沿引用访问。", evidence=self.evidence)

    async def close(self):
        pass


def test_answer_key_is_isolated_and_classroom_unchanged():
    lesson, state = Lesson(topic="链表"), StudentState()
    question = fixture_question()
    model = AssessmentModel()
    before = deepcopy(SOURCES)
    answer, evaluation = asyncio.run(assess(model, question, lesson, state, SOURCES))
    assert evaluation.verdict == "passed"
    assert "reference_answer" not in model.calls[0] and "rubric" not in model.calls[0]
    assert model.calls[0]["question"] == question.public()
    assert model.calls[1]["reference_answer"] == question.reference_answer
    assert state.version == 0 and not state.knowledge
    assert SOURCES == before


@pytest.mark.parametrize("coverage", ["missing", "contradictory", "out_of_scope", "uncertain"])
def test_missing_rules_cannot_be_marked_passed(coverage):
    lesson = Lesson(topic="链表")
    _, evaluation = asyncio.run(assess(AssessmentModel(coverage), fixture_question(), lesson, StudentState(), SOURCES))
    assert evaluation.verdict == "review"


@pytest.mark.parametrize("evidence", [[{"source_id": "t9", "quote": "不存在"}], [{"source_id": "t1", "quote": "编造的原文"}]])
def test_invalid_evaluation_evidence_is_rejected(evidence):
    lesson = Lesson(topic="链表")
    with pytest.raises(ValueError, match="课堂原文"):
        asyncio.run(assess(AssessmentModel(evidence=evidence), fixture_question(), lesson, StudentState(), SOURCES))


def test_invalid_answer_source_is_rejected_before_evaluation():
    lesson, model = Lesson(topic="链表"), AssessmentModel(answer_sources=["t9"])
    with pytest.raises(ValueError, match="不存在"):
        asyncio.run(assess(model, fixture_question(), lesson, StudentState(), SOURCES))
    assert len(model.calls) == 1


def test_ungrounded_correct_answer_requires_review():
    lesson = Lesson(topic="链表")
    _, evaluation = asyncio.run(assess(AssessmentModel(evidence=[], answer_sources=[]), fixture_question(), lesson, StudentState(), SOURCES))
    assert evaluation.verdict == "review"


def fixture_question():
    return AssessmentQuestion('fixture', 'apply', '从头节点 A 访问哪些数据？', '沿引用访问',
                              '沿引用访问直到空引用。', (AssessmentCriterion(text='沿引用访问', evidence=[]),))


def test_dynamic_generation_works_for_any_topic_and_keeps_key_private():
    async def run():
        model = AssessmentModel()
        lesson, state = Lesson(topic='多媒体教学'), StudentState()
        question = await generate_question(model, 'apply', lesson, state, SOURCES, [])
        await assess(model, question, lesson, state, SOURCES)
        assert len(model.calls) == 4
        student_payload = model.calls[2]
        assert 'reference_answer' not in student_payload and 'criteria' not in student_payload
        assert set(student_payload['question']) == {'id', 'kind', 'text'}
        assert model.calls[3]['reference_answer'] == question.reference_answer
        assert state.version == 0 and not state.knowledge
    asyncio.run(run())


def test_insufficient_teaching_does_not_attempt_student_answer():
    class InsufficientModel(AssessmentModel):
        async def generate(self, *args):
            self.calls.append(args[0])
            return GeneratedAssessment(status='insufficient', reason='只给出了课程名称，没有实质讲解。')

    async def run():
        model = InsufficientModel()
        with pytest.raises(ValueError, match='课堂内容不足'):
            await generate_question(model, 'apply', Lesson(topic='任何主题'), StudentState(), SOURCES, [])
        assert model.calls == ['assessment_question']
    asyncio.run(run())


@pytest.mark.parametrize('verdict', ['unsupported', 'uncertain'])
def test_independent_audit_blocks_unreliable_question(verdict):
    class AuditModel(AssessmentModel):
        async def generate(self, phase, *args):
            if phase == 'assessment_audit':
                return AssessmentAudit(verdict=verdict, reason='答案依赖未讲授的规则。')
            return await super().generate(phase, *args)

    with pytest.raises(ValueError, match='依据检查未通过'):
        asyncio.run(generate_question(AuditModel(), 'apply', Lesson(topic='数学'), StudentState(), SOURCES, []))


def test_generated_criterion_must_have_real_quote():
    class InvalidModel(AssessmentModel):
        async def generate(self, *args):
            result = await super().generate(*args)
            if isinstance(result, GeneratedAssessment):
                result.criteria[0].evidence[0].quote = '编造规则'
            return result

    with pytest.raises(ValueError, match='课堂原文'):
        asyncio.run(generate_question(InvalidModel(), 'apply', Lesson(topic='语文'), StudentState(), SOURCES, []))


def test_generated_question_cannot_repeat_completed_question():
    async def run():
        lesson, state = Lesson(topic='多媒体教学'), StudentState()
        first = await generate_question(AssessmentModel(), 'apply', lesson, state, SOURCES, [])
        completed = [{'question':first.public(), 'learning_target':first.learning_target, 'criteria':['沿引用访问']}]
        class RepeatingModel(AssessmentModel):
            async def generate(self, *args):
                self.generated = 0
                return await super().generate(*args)
        with pytest.raises(ValueError, match='过于相似'):
            await generate_question(RepeatingModel(), 'apply', lesson, state, SOURCES, completed)
        model = AssessmentModel()
        model.generated = 1
        second = await generate_question(model, 'apply', lesson, state, SOURCES, completed)
        assert first.text != second.text
        assert model.calls[0]['previous_target'] == first.learning_target
        assert model.calls[0]['previous_criteria'] == ['沿引用访问']
    asyncio.run(run())


def test_more_than_two_rounds_are_available():
    async def run():
        session, _ = await make_session(AssessmentModel())
        try:
            session.lesson = Lesson(topic='多媒体教学')
            for index in range(3):
                source_id = f't{index + 2}'
                session.sources[source_id] = {'id':source_id, 'text':'新增讲授例子。'}
                session.processed_ids.add(source_id)
                session.revision += 1
                await session.start_assessment('apply')
                await session.assessment_task
            assert [r['round'] for r in session.assessments] == [1, 2, 3]
        finally:
            await session.close()
    asyncio.run(run())


def test_rejected_question_is_revised_once_before_answering():
    class RetryModel(AssessmentModel):
        def __init__(self):
            super().__init__()
            self.audits = 0

        async def generate(self, phase, *args):
            if phase == 'assessment_audit':
                self.audits += 1
                if self.audits == 1:
                    return AssessmentAudit(verdict='unsupported', reason='照搬课堂示例，需要不同情境。')
            return await super().generate(phase, *args)

    async def run():
        model = RetryModel()
        question = await generate_question(model, 'apply', Lesson(topic='多媒体教学'), StudentState(), SOURCES, [])
        assert model.generated == 2 and model.audits == 2
        assert '书架' in question.text
        revised_payload = next(p for p in model.calls if 'revision_feedback' in p)
        assert '不同情境' in revised_payload['revision_feedback']
        assert len(revised_payload['rejected_questions']) == 1
    asyncio.run(run())


async def make_session(model):
    events = []

    async def send(event):
        events.append(event)

    session = Session(send, None)
    await session.llm.close()
    session.llm = model
    session.ready = True
    session.lesson = Lesson(topic="链表基础")
    session.sources = {s['id']: dict(s) for s in SOURCES}
    session.processed_ids = {'t1'}
    return session, events


def test_session_retest_needs_new_teaching_and_does_not_write_memory():
    async def run():
        session, events = await make_session(AssessmentModel())
        before = (session.state.model_dump(), deepcopy(session.sources), deepcopy(session.history))
        try:
            await session.start_assessment('apply')
            await session.assessment_task
            assert len(session.assessments) == 1
            with pytest.raises(ValueError, match='补充讲解'):
                await session.start_assessment('apply')
            session.sources['t2'] = {'id':'t2', 'text':'从头节点沿引用访问，而不是按节点名字排序。'}
            session.processed_ids.add('t2')
            session.revision += 1
            await session.start_assessment('apply')
            await session.assessment_task
            assert session.assessments[0]['question']['id'] != session.assessments[1]['question']['id']
            assert [r['round'] for r in session.assessments] == [1, 2]
            assert session.state.model_dump() == before[0] and session.history == before[2]
            assert session.sources['t1'] == before[1]['t1']
            assert all(not key.startswith('a') for key in session.sources)
            assert any(e['type'] == 'assessment_result' for e in events)
        finally:
            await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('condition', ['audio', 'pending', 'processing', 'asr', 'empty'])
def test_session_rejects_assessment_before_classroom_is_ready(condition):
    async def run():
        model = AssessmentModel()
        session, _ = await make_session(model)
        try:
            if condition == 'audio': session.audio_enabled = True
            if condition == 'pending': session.pending = [{'id':'t2', 'text':'还没消化'}]
            if condition == 'processing': session.processing = True
            if condition == 'asr': session.asr_pending = 1
            if condition == 'empty': session.processed_ids.clear()
            with pytest.raises(ValueError):
                await session.start_assessment('apply')
            assert not model.calls
        finally:
            await session.close()
    asyncio.run(run())


class PausedModel(AssessmentModel):
    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def generate(self, *args):
        self.started.set()
        await self.release.wait()
        return await super().generate(*args)


def test_new_input_discards_old_result_and_duplicate_request_is_rejected():
    async def run():
        model = PausedModel()
        session, events = await make_session(model)
        try:
            await session.start_assessment('apply')
            await model.started.wait()
            with pytest.raises(ValueError, match='正在进行'):
                await session.start_assessment('apply')
            await session.add_transcript('补充讲解')
            model.release.set()
            await session.assessment_task
            assert not session.assessments
            assert any(e['type'] == 'assessment_discarded' for e in events)
            assert events[-1]['data']['busy'] is False
        finally:
            await session.close()
    asyncio.run(run())


def test_finish_cancels_inflight_assessment():
    async def run():
        model = PausedModel()
        session, events = await make_session(model)
        await session.start_assessment('apply')
        await model.started.wait()
        await asyncio.wait_for(session.finish(), 2)
        assert session.closed and session.assessment_task.cancelled()
        assert not session.assessments
        assert not any(e['type'] == 'assessment_result' for e in events)
    asyncio.run(run())


def test_microphone_start_is_rejected_during_assessment(monkeypatch):
    async def run():
        model = PausedModel()
        session, _ = await make_session(model)

        def forbidden(*args):
            pytest.fail('测验期间不得创建音频流')

        monkeypatch.setattr('backend.session.AudioStream', forbidden)
        try:
            await session.start_assessment('apply')
            await model.started.wait()
            with pytest.raises(ValueError, match='测验正在进行'):
                await session.start_audio(16000)
            assert not session.audio_enabled
        finally:
            await session.close()
    asyncio.run(run())


def test_result_is_not_committed_if_audio_becomes_active():
    async def run():
        model = PausedModel()
        session, events = await make_session(model)
        try:
            await session.start_assessment('apply')
            await model.started.wait()
            session.audio_enabled = True
            model.release.set()
            await session.assessment_task
            assert not session.assessments
            assert any(e['type'] == 'assessment_discarded' for e in events)
        finally:
            await session.close()
    asyncio.run(run())


def test_failed_assessment_does_not_consume_question_and_can_retry():
    class FailingModel(AssessmentModel):
        async def generate(self, *args):
            if not hasattr(self, 'failed'):
                self.failed = True
                raise ModelError('模拟请求失败')
            return await super().generate(*args)

    async def run():
        session, events = await make_session(FailingModel())
        try:
            await session.start_assessment('apply')
            await session.assessment_task
            assert not session.assessments
            assert any(e['type'] == 'error' and e['data']['code'] == 'assessment_failure' for e in events)
            await session.start_assessment('apply')
            await session.assessment_task
            assert session.assessments[0]['question']['id'] == 'q1'
        finally:
            await session.close()
    asyncio.run(run())


def test_websocket_assessment_roundtrip(manager, monkeypatch, tmp_path):
    from fastapi.testclient import TestClient
    from backend import app as service
    from backend.schemas import Preparation, StudentResult

    class ProtocolModel(AssessmentModel):
        def __init__(self, emit):
            super().__init__()

        async def generate(self, phase, system, payload, output_type):
            if phase == 'preparation':
                return Preparation(scope=['链表基础'], boundary_note='仅从课堂学习')
            if phase == 'student':
                return StudentResult()
            return await super().generate(phase, system, payload, output_type)

    def receive(ws, kind):
        for _ in range(40):
            event = ws.receive_json()
            if event['type'] == kind:
                return event['data']
        pytest.fail('没有收到事件：' + kind)

    asyncio.run(install(manager))
    manager.select('first')
    monkeypatch.setattr(manager, '_create_engine', lambda model: FakeEngine(model.id))
    monkeypatch.setattr(service, 'models', manager)
    monkeypatch.setattr(service, 'active_session', None)
    monkeypatch.setattr('backend.session.ModelClient', ProtocolModel)
    monkeypatch.setattr('backend.config.ROOT', tmp_path)
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            ws.send_json({'type':'start', 'lesson':{'topic':'多媒体教学'}, 'tts':False, 'asr_review':False})
            assert receive(ws, 'ready')['assessment_available']
            ws.send_json({'type':'text', 'text':SOURCES[0]['text']})
            receive(ws, 'state')
            ws.send_json({'type':'assessment', 'kind':'apply'})
            first = receive(ws, 'assessment_result')
            assert first['question']['id'] == 'q1'
            assert first['evaluation']['verdict'] == 'passed'
            ws.send_json({'type':'assessment', 'kind':'apply'})
            assert receive(ws, 'error')['code'] == 'assessment_failure'
            ws.send_json({'type':'text', 'text':'节点的数据和引用作用不同。'})
            receive(ws, 'state')
            ws.send_json({'type':'assessment', 'kind':'apply'})
            second = receive(ws, 'assessment_result')
            assert second['question']['id'] == 'q2' and second['round'] == 2
            ws.send_json({'type':'end'})
            finished = receive(ws, 'finished')
            assert len(finished['assessments']) == 2 and not finished['unprocessed_sources']
    assert not manager.busy
    # Completion must persist the results, not merely display them over WebSocket.
    import json
    records = [json.loads(line) for line in (tmp_path / finished['log_file']).read_text(encoding='utf-8').splitlines()]
    stored_results = [event['data'] for event in records if event['type'] == 'assessment_result']
    assert stored_results == finished['assessments']
    assert next(event['data'] for event in records if event['type'] == 'ready')['assessment_available'] is True
    assert next(event['data'] for event in records if event['type'] == 'finished')['assessments'] == stored_results
