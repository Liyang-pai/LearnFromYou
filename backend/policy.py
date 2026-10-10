"""Deterministic rules: the model proposes; this module controls state and speech."""
import re
from difflib import SequenceMatcher
from .schemas import StudentState, StudentResult, Question, KnowledgeRevision


def invited(text: str) -> bool:
    return bool(re.search(r"(你.{0,4}(懂|理解|明白|觉得|知道|回答|说说|复述)|听(懂|明白)了吗|有什么(问题|疑问)|请(回答|复述)|学生.{0,3}(回答|说)|能.{0,3}(解释|举例).{0,3}吗)", text))


def normalize(text: str) -> str:
    return re.sub(r"[\W_]", "", text).lower()


def question_key(text: str) -> str:
    """仅去掉请求解释的常见外壳，不能把同主题的不同内容视为同一疑问。"""
    return re.sub(r'能否|能再|请再|请|再|解释|一下|是什么|吗|呢|什么是', '', normalize(text))


def apply_result(state: StudentState, result: StudentResult, sources: dict, current_ids: set[str]):
    """Validate everything before committing anything; source validity isn't semantic proof."""
    next_state = state.model_copy(deep=True)
    known = {k.id: k for k in next_state.knowledge}
    questions = {q.id: q for q in next_state.questions}
    aliases = {}

    def check(ids):
        if any(s not in sources for s in ids):
            raise ValueError("输出引用了不存在的课堂来源")

    for item in result.knowledge_updates:
        check(item.sources)
        old = known.get(item.id)
        if item.id in item.supersedes:
            if old is None:
                raise ValueError("纠正必须关联到已有知识条目")
            # A same-ID correction already archives the previous version below.
            item = item.model_copy(update={"supersedes": [id for id in item.supersedes if id != item.id]})
        if old and old != item:
            next_state.knowledge_history.append(KnowledgeRevision(
                version=state.version + 1, previous=old.model_copy(deep=True), replacement_id=item.id))
        for old_id in item.supersedes:
            if old_id not in known:
                raise ValueError("纠正必须关联到已有的其他知识条目")
            replaced = known[old_id]
            if replaced.status != "conflict":
                next_state.knowledge_history.append(KnowledgeRevision(
                    version=state.version + 1, previous=replaced.model_copy(deep=True), replacement_id=item.id))
                known[old_id] = replaced.model_copy(update={"status": "conflict"})
        known[item.id] = item
    events = {event.id: event for event in next_state.recent_events}
    for event in result.event_updates:
        check(event.sources)
        if not set(event.sources) & current_ids:
            raise ValueError("新课堂事件需要本轮教师来源，不能重复添加历史事件")
        events.pop(event.id, None)
        events[event.id] = event
    next_state.recent_events = list(events.values())[-20:]
    for update in result.question_updates:
        check(update.sources)
        check(update.resolution_sources)
        if update.status == "resolved" and not any(s.startswith("t") for s in update.resolution_sources):
            raise ValueError("问题关闭需要教师解释的来源")
        old = questions.get(update.id)
        if not old:
            for existing in questions.values():
                same_topic = normalize(existing.topic) == normalize(update.topic)
                same_question = SequenceMatcher(None, normalize(existing.text), normalize(update.text)).ratio() > .82
                if same_question or (same_topic and question_key(existing.text) == question_key(update.text)):
                    old = existing
                    aliases[update.id] = existing.id
                    break
        if old and old.status == "resolved":
            continue  # A closed question cannot be resurrected by rewording it.
        data = update.model_dump()
        if old:
            data["id"] = old.id
            if old.attempts >= 2 and update.status != "resolved":
                data["status"] = "deferred"
            elif old.status == "asked" and update.status == "pending":
                data["status"] = "asked"
        elif update.status == "asked":
            data["status"] = "pending"  # Only actual delivery increments/marks an ask.
        questions[data["id"]] = Question(**data, attempts=old.attempts if old else 0)
    candidate = result.candidate.model_copy(deep=True)
    check(candidate.sources)
    if candidate.question_id in aliases:
        candidate.question_id = aliases[candidate.question_id]
    if candidate.kind != "silence" and (not candidate.text.strip() or not candidate.sources):
        raise ValueError("候选发言需要正文及课堂来源")
    if candidate.kind == "question":
        q = questions.get(candidate.question_id)
        if not q:
            raise ValueError("候选问题不在问题库中")
        if q.status in ("resolved", "deferred") or q.attempts >= 2:
            candidate.kind, candidate.text = "silence", ""
    next_state.knowledge = list(known.values())
    next_state.questions = list(questions.values())
    next_state.version += 1
    return next_state, candidate


def mark_delivered(state, candidate):
    if candidate.kind == "question":
        for q in state.questions:
            if q.id == candidate.question_id:
                q.attempts += 1
                q.status = "asked" if q.attempts < 2 else "deferred"


def speech_block(*, muted, stale, busy, speaking, silence, required_pause, candidate, addressed):
    if muted:
        return "静音：继续更新认知，不对外发言"
    if stale:
        return "已有新的教师输入，旧回复作废"
    if busy:
        return "学生正在消化，等待未处理内容"
    if speaking:
        return "教师仍在讲话"
    if silence < required_pause:
        return "等待合适的停顿"
    if candidate.kind == "silence":
        return "本轮无需发言"
    if not addressed and candidate.kind != "question":
        return "未被点名，继续听课"
    return None
