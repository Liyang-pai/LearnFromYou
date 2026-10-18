"""Deterministic rules: the model proposes; this module controls state and speech."""
import re
from difflib import SequenceMatcher
from .schemas import StudentState, StudentResult, Question, Candidate, ResponseRequest
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


class QuestionReleaseError(ValueError):
    """An exception requires an explicit, current teacher invitation and an application."""
    def __init__(self, question_id, request_id=None, reason='缺少有效的例外放行申请'):
        self.question_id, self.request_id = question_id, request_id
        super().__init__(f'疑问 {question_id!r} 例外申请未通过：{reason}。'
                         '同题通常最多问两次；仅教师明确邀请说明疑问时，使用 question 并附'
                         ' question_release_request，关联原问题和有效邀请。不得改成 answer/ack 绕过；'
                         '无有效邀请时 silence。')


class ResponseIntentError(ValueError):
    def __init__(self):
        super().__init__('教师有本轮待回应请求，不能因没有新知识而 silence。按意图回应：'
                         '说明疑问用 question（暂缓疑问附例外申请），回答用 answer，检查声音用 ack；'
                         '没有疑问可说明没有疑问，不强造问题。')


def expire_invitations(state, revision):
    changed = []
    for request in state.response_requests:
        if (request.kind == 'invite_questions' and request.status == 'pending'
                and request.input_revision != revision):
            request.status, request.reason = 'dismissed', '新的教师输入使旧邀请失效'
            changed.append(request.id)
    if changed:
        state.version += 1
    return changed


def release_error(state, candidate, revision):
    """Recheck immediately before delivery; no counters or permissions are consumed here."""
    release = candidate.question_release_request
    if not release:
        return None
    questions = {q.id: q for q in state.questions}
    requests = {r.id: r for r in state.response_requests}
    q, request = questions.get(candidate.question_id), requests.get(release.response_request_id)
    if (candidate.kind != 'question' or release.question_id != candidate.question_id
            or not q or q.status == 'resolved' or not (q.status == 'deferred' or q.attempts >= 2)):
        return '候选与原暂缓疑问不匹配，或疑问已经解决'
    if (not request or request.kind != 'invite_questions' or request.status != 'pending'
            or request.input_revision != revision
            or candidate.response_request_ids != [request.id]
            or request.invitation_source_id not in candidate.sources):
        return '教师邀请已失效、已使用，或未正确关联'
    return None


def student_result_validator(state, sources, current_ids, recovery, *, input_revision=None, resume=False, require_response=False):
    """Validate on a snapshot; a final candidate-only failure cannot discard valid learning."""
    snapshot = state.model_copy(deep=True)

    def validate(result, final_attempt):
        proposed = StudentResult(**result.model_dump()) if resume else result
        normalizations = []
        try:
            updated, _ = apply_result(snapshot, proposed, sources, current_ids, resume=resume,
                                      input_revision=input_revision, normalizations=normalizations)
            if require_response and result.candidate.kind == 'silence' and any(
                    r.status == 'pending' and set(r.sources) & current_ids for r in updated.response_requests):
                raise ResponseIntentError()
        except (MissingQuestionError, RestrictedKnowledgeError, QuestionReleaseError, ResponseIntentError) as exc:
            if not final_attempt:
                raise
            updates = result.response_request_updates
            if isinstance(exc, QuestionReleaseError) and exc.request_id:
                # Invalid invitation records are not committed, either. Other records are revalidated.
                updates = [r for r in updates if r.id != exc.request_id]
            cleaned = result.model_copy(update={'candidate': Candidate(), 'response_request_updates': updates}, deep=True)
            proposed = StudentResult(**cleaned.model_dump()) if resume else cleaned
            apply_result(snapshot, proposed, sources, current_ids, resume=resume,
                         input_revision=input_revision, allow_silence=True)
            detail = {'sources': list(result.candidate.sources), 'reason': str(exc), 'attempt': 2}
            if isinstance(exc, MissingQuestionError):
                detail.update(code='missing_question_reference', question_id=exc.question_id,
                              message='异常追问已跳过，课堂继续')
            elif isinstance(exc, RestrictedKnowledgeError):
                detail.update(code='restricted_knowledge_reference', knowledge_ids=exc.knowledge_ids,
                              message='回复仍使用不可用的知识前提，已跳过；课堂继续')
            elif isinstance(exc, QuestionReleaseError):
                detail.update(code='question_release_denied', question_id=exc.question_id,
                              response_request_id=exc.request_id, message='提问例外申请未通过，已跳过候选；课堂继续')
            else:
                detail.update(code='response_intent_unrepaired', message='教师回应请求未完成，已保留待回应记录；课堂继续')
            recovery.append(detail)
            return cleaned
        if normalizations:
            candidate = result.candidate.model_dump()
            for detail in normalizations:
                candidate.update(detail['after'])
            result = result.model_copy(update={'candidate': Candidate.model_validate(candidate)}, deep=True)
            recovery.extend({**detail, 'attempt': 2 if final_attempt else 1} for detail in normalizations)
        return result
    return validate


def invited(text: str) -> bool:
    return bool(re.search(r"(你.{0,4}(懂|理解|明白|觉得|知道|回答|说说|复述)|听(懂|明白)了吗|有什么(问题|疑问)|还有(?:什么)?(?:问题|疑问)吗|请(回答|复述)|学生.{0,3}(回答|说)|能.{0,3}(解释|举例).{0,3}吗)", text))


def normalize(text: str) -> str:
    return re.sub(r"[\W_]", "", text).lower()


def normalize_candidate(candidate, knowledge, sources):
    """Repair only unambiguous candidate bookkeeping, never infer adopted knowledge."""
    before = candidate.model_dump()
    refs = []
    for sid in candidate.sources:
        if sid not in sources and sid in candidate.knowledge_ids and sid in knowledge:
            original_sources = knowledge[sid].sources
            if not original_sources or any(ref not in sources for ref in original_sources):
                raise ValueError('候选知识引用缺少有效的原始课堂来源')
            refs.extend(original_sources)
        else:
            refs.append(sid)
    after = {**before, 'sources': list(dict.fromkeys(refs))}
    if candidate.kind in ('answer', 'ack') and candidate.question_release_request is None:
        after['question_id'] = None
    changed = [key for key in ('sources', 'question_id') if before[key] != after[key]]
    if not changed:
        return candidate.model_copy(deep=True), []
    # Revalidate limits after expansion; never truncate evidence to make it fit.
    normalized = Candidate.model_validate(after)
    return normalized, [{'code': 'candidate_normalized',
        'message': '回复关联字段已本地整理，未追加模型请求',
        'sources': list(normalized.sources),
        'before': {key: before[key] for key in changed},
        'after': {key: after[key] for key in changed}}]


def apply_result(state: StudentState, result: StudentResult, sources: dict, current_ids: set[str], *, resume=False, input_revision=None, allow_silence=False, normalizations=None):
    """Validate everything before committing anything; source validity isn't semantic proof."""
    next_state = state.model_copy(deep=True)
    questions = {q.id: q for q in next_state.questions}
    aliases = {}
    if input_revision is None:
        input_revision = max((s.get('revision', 0) for s in sources.values()), default=0)
    expire_invitations(next_state, input_revision)

    def check(ids):
        if any(s not in sources for s in ids):
            raise ValueError("输出引用了不存在的课堂来源")

    # 恢复判断不重新应用认知规则，确保知识、历史和复核队列原样保留。
    if not resume:
        apply_knowledge(next_state, result, sources, current_ids)
    requests = {r.id: r for r in next_state.response_requests}
    if len({r.id for r in result.response_request_updates}) != len(result.response_request_updates):
        raise ValueError('同轮教师请求 ID 不得重复')
    for update in result.response_request_updates:
        check(update.sources)
        check(update.resolution_sources)
        if any(not s.startswith('t') for s in update.sources + update.resolution_sources):
            raise ValueError('教师回应请求只能引用教师来源')
        old = requests.get(update.id)
        immutable = ('text', 'sources', 'kind', 'invitation_source_id', 'invitation_quote')
        if old and any(getattr(update, key) != getattr(old, key) for key in immutable):
            raise ValueError('已有教师请求必须保留意图、正文和原始来源；新请求使用新 ID')
        if old and old.status != 'pending':
            if update.status != old.status and not (old.kind == 'invite_questions' and old.status == 'dismissed'):
                raise ValueError('已回应或已取消的请求不能重新激活或改写')
            continue
        if not old:
            if resume or update.status != 'pending' or not set(update.sources) & current_ids:
                if update.kind == 'invite_questions':
                    raise QuestionReleaseError(result.candidate.question_id, update.id, '新邀请必须来自本轮教师输入，恢复时不能新建邀请')
                raise ValueError('新教师请求需要本轮教师来源；恢复判断不能新建请求')
            if update.kind != 'invite_questions' and any(r.text == update.text and set(r.sources) == set(update.sources) for r in requests.values()):
                raise ValueError('相同教师请求应沿用原 ID，不能重复登记')
        if update.kind == 'invite_questions':
            sid, quote = update.invitation_source_id, update.invitation_quote.strip()
            if (sid not in update.sources or sid not in sources or not quote
                    or quote not in sources[sid].get('text', '') or (not old and sid not in current_ids)):
                raise QuestionReleaseError(result.candidate.question_id, update.id, '邀请必须附有效教师来源及逐字原文')
            if any(r.id != update.id and r.kind == 'invite_questions' and r.invitation_source_id == sid
                   for r in requests.values()):
                raise QuestionReleaseError(result.candidate.question_id, update.id, '同一教师来源只能登记一次邀请，不能换 ID 领取')
        if update.status == 'dismissed':
            if not update.reason.strip() or not update.resolution_sources:
                raise ValueError('取消教师请求需要原因和教师依据')
            if not resume and not set(update.resolution_sources) & current_ids:
                raise ValueError('取消教师请求需要本轮教师依据')
        requests[update.id] = ResponseRequest(**update.model_dump(),
            input_revision=old.input_revision if old else sources.get(update.invitation_source_id, {}).get('revision', input_revision))
    next_state.response_requests = list(requests.values())
    expire_invitations(next_state, input_revision)
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
            elif update.status == 'deferred' and old.status != 'deferred':
                data['status'] = old.status  # Do not anticipate an as-yet undelivered second ask.
            elif old.status == "asked" and update.status == "pending":
                data["status"] = "asked"
        elif update.status == "asked":
            data["status"] = "pending"  # Only actual delivery increments/marks an ask.
        elif update.status == 'deferred':
            data['status'] = 'pending'
        questions[data["id"]] = Question(**data, attempts=old.attempts if old else 0)
    current = {k.id: k for k in next_state.knowledge}
    candidate, changes = normalize_candidate(result.candidate, current, sources)
    check(candidate.sources)
    if candidate.question_id in aliases:
        candidate.question_id = aliases[candidate.question_id]
    if candidate.kind != "silence" and (not candidate.text.strip() or not candidate.sources):
        raise ValueError("候选发言需要正文及课堂来源")
    if len(set(candidate.response_request_ids)) != len(candidate.response_request_ids):
        raise ValueError('候选教师请求 ID 不得重复')
    if candidate.response_request_ids:
        if candidate.kind == 'silence':
            raise ValueError('沉默不能标记已回应教师请求')
        if any(rid not in requests or requests[rid].status != 'pending' for rid in candidate.response_request_ids):
            raise QuestionReleaseError(candidate.question_id, reason='候选关联的教师邀请或请求已经失效')
        if candidate.kind == 'question' and any(requests[rid].kind != 'invite_questions' for rid in candidate.response_request_ids):
            raise QuestionReleaseError(candidate.question_id, reason='只有明确邀请说明疑问才能由 question 回应')
    if candidate.kind != 'question' and (candidate.question_release_request or candidate.question_id):
        raise QuestionReleaseError(candidate.question_id, reason='不能把提问关联放进 answer/ack 绕过限制')
    if resume and not allow_silence:
        pending = {r.id for r in requests.values() if r.status == 'pending'}
        if pending and not candidate.response_request_ids:
            raise QuestionReleaseError(candidate.question_id, reason='仍有教师请求，须关联实际回应的请求或提供取消依据')
        if not pending and candidate.kind in ('answer', 'ack'):
            raise ValueError('没有教师待回应请求时只能有效提问或沉默')
    if any(kid not in current for kid in candidate.knowledge_ids):
        raise ValueError('候选回复引用了不存在的知识前提')
    next_state.questions = list(questions.values())
    if candidate.kind == "question":
        q = questions.get(candidate.question_id)
        if not q:
            raise MissingQuestionError(candidate.question_id)
        if q.status == 'resolved':
            if candidate.question_release_request:
                raise QuestionReleaseError(q.id, reason='已解决的疑问不能申请例外')
            candidate = Candidate()
        elif q.status == 'deferred' or q.attempts >= 2:
            reason = release_error(next_state, candidate, input_revision) if candidate.question_release_request else '缺少例外申请'
            if reason:
                raise QuestionReleaseError(q.id, reason=reason)
        elif candidate.question_release_request:
            raise QuestionReleaseError(q.id, reason='未暂缓的疑问不需要例外申请')
    blocked = {r.knowledge_id for r in next_state.pending_reviews}
    restricted = [kid for kid in candidate.knowledge_ids
                  if kid in blocked or current[kid].status in ('unclear', 'conflict', 'invalid')]
    if candidate.kind != 'silence' and restricted:
        raise RestrictedKnowledgeError(restricted)
    next_state.questions = list(questions.values())
    next_state.version += 1
    if normalizations is not None:
        normalizations.extend(changes)
    return next_state, candidate


def mark_delivered(state, candidate):
    if candidate.question_release_request and not any(
            r.id == candidate.question_release_request.response_request_id and r.status == 'pending'
            for r in state.response_requests):
        return  # A delivery retry must not spend the same invitation or count twice.
    changed = False
    if candidate.kind == "question":
        for q in state.questions:
            if q.id == candidate.question_id:
                q.attempts += 1
                q.status = "asked" if q.attempts < 2 else "deferred"
                changed = True
    if candidate.kind in ('answer', 'ack', 'question'):
        for request in state.response_requests:
            if request.id in candidate.response_request_ids and request.status == 'pending':
                request.status = 'answered'
                request.reason = '学生已实际发出回应；不表示知识已经理解'
                changed = True
    if changed:
        state.version += 1


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
