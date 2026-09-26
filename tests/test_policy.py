import pytest
from backend.policy import apply_result, mark_delivered, speech_block
from backend.schemas import Candidate, Knowledge, Question, StudentResult, StudentState

SOURCES = {"p1": {"text": "知道数组"}, "t1": {"text": "节点通过指针连接"}, "t2": {"text": "指针保存下个节点的位置"}}


def test_source_rejection_is_atomic():
    state = StudentState()
    result = StudentResult.model_validate({"knowledge_updates": [
        {"id": "k1", "text": "节点连接", "status": "tentative", "sources": ["t1"]},
        {"id": "k2", "text": "未讲算法", "status": "understood", "sources": ["student_reply"]}]})
    with pytest.raises(ValueError):
        apply_result(state, result, SOURCES, {"t1"})
    assert state.version == 0 and not state.knowledge


def test_resolved_question_cannot_reappear_under_new_id():
    state = StudentState(questions=[Question(id="q1", topic="指针的含义", text="指针是什么？", status="resolved", sources=["t1"], resolution_sources=["t2"], attempts=1)])
    result = StudentResult.model_validate({"question_updates": [{"id": "q2", "topic": "指针的含义", "text": "能再解释指针吗？", "status": "pending", "sources": ["t1"]}],
        "candidate": {"kind": "question", "text": "能再解释指针吗？", "question_id": "q2", "sources": ["t1"]}})
    new, reply = apply_result(state, result, SOURCES, {"t2"})
    assert len(new.questions) == 1 and new.questions[0].status == "resolved"
    assert reply.kind == "silence"


def test_close_requires_teachers_explanation():
    result = StudentResult.model_validate({"question_updates": [{"id": "q1", "topic": "指针", "text": "是什么", "status": "resolved", "sources": ["t1"], "resolution_sources": ["p1"]}]})
    with pytest.raises(ValueError):
        apply_result(StudentState(), result, SOURCES, {"t1"})


def test_attempts_count_delivery_not_generation():
    state = StudentState(questions=[Question(id="q1", topic="指针", text="是什么？", status="pending", sources=["t1"])])
    candidate = Candidate(kind="question", text="是什么？", question_id="q1", sources=["t1"])
    assert state.questions[0].attempts == 0
    mark_delivered(state, candidate); mark_delivered(state, candidate)
    assert state.questions[0].attempts == 2 and state.questions[0].status == "deferred"
    _, reply = apply_result(state, StudentResult(candidate=candidate), SOURCES, {"t2"})
    assert reply.kind == "silence"


@pytest.mark.parametrize('changes', [{"muted":True},{"stale":True},{"busy":True},{"speaking":True},{"silence":.5},{"addressed":False}])
def test_answer_gate(changes):
    params = dict(muted=False,stale=False,busy=False,speaking=False,silence=5,required_pause=1,
                  candidate=Candidate(kind="answer",text="还没学到",sources=["t1"]),addressed=True)
    assert speech_block(**params) is None
    params.update(changes)
    assert speech_block(**params)

