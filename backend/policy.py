"""Deterministic rules: the model proposes; this module controls state and speech."""
import re
from difflib import SequenceMatcher
from .schemas import StudentState, StudentResult, Question, Candidate
from .cognition import apply_knowledge


class MissingQuestionError(ValueError):
    """候选追问缺少关联记录；与知识或来源不可靠的错误明确区分。"""
    def __init__(self, question_id):
        self.question_id = question_id
        super().__init__(f'candidate.question_id={question_id!r} 未关联到已有或本轮登记的疑问。'
                         '请在 question_updates 中补齐对应疑问记录，或将 candidate 改为 silence。')


class RestrictedKnowledgeError(ValueError):
    """候选将不可用知识声明为推理前提，需要模型修正发言或关联。"""
    def __init__(self, knowledge_ids):
        self.knowledge_ids = knowledge_ids
        super().__init__(f'candidate.knowledge_ids 引用了不可作为确定前提的知识：{knowledge_ids!r}。'
                         '若只是复述旧说法、描述冲突或说明不懂，请保留 sources，'
                         '从 knowledge_ids 移除仅被提及的条目；若确实用它们推导答案，'
                         '请改为说明障碍，不要给出确定结论。新追问仍须在 question_updates 登记。')


class DeferredQuestionReplyError(ValueError):
    """教师正在询问学生，但候选再次追问已暂缓的旧问题。"""
    def __init__(self, question_id):
        self.question_id = question_id
        super().__init__(f'疑问 {question_id!r} 已暂缓或达到两次提问上限，不能再次输出 question。'
                         '但本轮教师明确要求回应，不能因此沉默。请改为 answer 或 ack，'
                         '简短说明目前的理解或尚未想明白的地方，不再重复请求解释；'
                         'question_id=null，保留有效 sources，仅说明障碍时 knowledge_ids=[]。'
                         '保持原疑问 deferred，不重建同义疑问，不增加提问次数。')


def student_result_validator(state, sources, current_ids, recovery):
    """在状态副本上预校验，最后一次可跳过关联错误的发言。"""
    snapshot = state.model_copy(deep=True)

    def validate(result, final_attempt):
        try:
            apply_result(snapshot, result, sources, current_ids)
        except (MissingQuestionError, RestrictedKnowledgeError, DeferredQuestionReplyError) as exc:
            if not final_attempt:
                raise
            replacement = Candidate()
            if isinstance(exc, DeferredQuestionReplyError):
                # 只确认仍在听、不作领域断言；不会将第三次追问改名后放行。
                replacement = Candidate(kind='ack', text='我在听，刚才的问题还没想明白，我先继续听您讲。',
                                        sources=sorted(current_ids))
            cleaned = result.model_copy(update={'candidate': replacement}, deep=True)
            # 替换候选后仍需完整校验；不能把其他错误一起忽略或提前写入状态。
            apply_result(snapshot, cleaned, sources, current_ids)
            detail = {'sources': list(result.candidate.sources), 'reason': str(exc), 'attempt': 2}
            if isinstance(exc, MissingQuestionError):
                detail.update(code='missing_question_reference', question_id=exc.question_id,
                              message='异常追问已跳过，课堂继续')
            elif isinstance(exc, RestrictedKnowledgeError):
                detail.update(code='restricted_knowledge_reference', knowledge_ids=exc.knowledge_ids,
                              message='回复仍使用不可用的知识前提，已跳过；课堂继续')
            else:
                detail.update(code='deferred_question_reply', question_id=exc.question_id,
                              message='重复追问已改为简短回应，课堂继续')
            recovery.append(detail)
            return cleaned
        return result
    return validate


def invited(text: str) -> bool:
    return bool(re.search(r"(你.{0,4}(懂|理解|明白|觉得|知道|回答|说说|复述)|听(懂|明白)了吗|有什么(问题|疑问)|还有(?:什么)?(?:问题|疑问)吗|请(回答|复述)|学生.{0,3}(回答|说)|能.{0,3}(解释|举例).{0,3}吗)", text))


def normalize(text: str) -> str:
    return re.sub(r"[\W_]", "", text).lower()


def apply_result(state: StudentState, result: StudentResult, sources: dict, current_ids: set[str]):
    """Validate everything before committing anything; source validity isn't semantic proof."""
    next_state = state.model_copy(deep=True)
    questions = {q.id: q for q in next_state.questions}
    aliases = {}

    def check(ids):
        if any(s not in sources for s in ids):
            raise ValueError("输出引用了不存在的课堂来源")

    apply_knowledge(next_state, result, sources, current_ids)
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
                if same_topic or same_question:
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
    current = {k.id: k for k in next_state.knowledge}
    if any(kid not in current for kid in candidate.knowledge_ids):
        raise ValueError('候选回复引用了不存在的知识前提')
    if candidate.kind == "question":
        q = questions.get(candidate.question_id)
        if not q:
            raise MissingQuestionError(candidate.question_id)
        addressed = result.addressed or invited('\n'.join(sources[sid].get('text', '') for sid in sorted(current_ids)))
        if q.status != 'resolved' and (q.status == 'deferred' or q.attempts >= 2) and addressed:
            raise DeferredQuestionReplyError(q.id)
        if q.status in ("resolved", "deferred") or q.attempts >= 2:
            candidate.kind, candidate.text = "silence", ""
    blocked = {r.knowledge_id for r in next_state.pending_reviews}
    restricted = [kid for kid in candidate.knowledge_ids
                  if kid in blocked or current[kid].status in ('unclear', 'conflict', 'invalid')]
    if candidate.kind != 'silence' and restricted:
        raise RestrictedKnowledgeError(restricted)
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
