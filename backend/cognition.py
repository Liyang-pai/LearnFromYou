"""管理五状态知识、冲突关系、版本历史及推导复核；不改变问题或发言生命周期。"""
from .schemas import Knowledge, KnowledgeRevision, KnowledgeReview


SEMANTIC_FIELDS = ('text', 'status', 'conditions', 'dependencies')


def semantic(item):
    return {key: item.model_dump()[key] for key in SEMANTIC_FIELDS}


def dependency_order(known):
    """验证引用并拓扑排序，防止循环推导使状态传播无法收敛。"""
    visited, active, order = set(), set(), []

    def visit(kid):
        if kid in active:
            raise ValueError('知识依赖不能形成自引用或循环')
        if kid in visited:
            return
        if kid not in known:
            raise ValueError('依赖必须引用已有或本轮新增知识')
        active.add(kid)
        for dep in known[kid].dependencies:
            visit(dep.id)
        active.remove(kid)
        visited.add(kid)
        order.append(kid)

    for kid in known:
        visit(kid)
    return order


def seed_prerequisites(state, prerequisites):
    state.knowledge = [Knowledge(id=p['id'], text=p['text'], status='understood',
                                sources=[p['id']], reason='画像确认基础') for p in prerequisites]


def apply_knowledge(state, result, sources, current_ids):
    """仅操作整轮副本；所有引用及来源通过后，由调用者提交。"""
    original = {k.id: k.model_copy(deep=True) for k in state.knowledge}
    known = {k: v.model_copy(deep=True) for k, v in original.items()}
    updates = {k.id: k.model_copy(deep=True) for k in result.knowledge_updates}
    if len(updates) != len(result.knowledge_updates):
        raise ValueError('同轮知识 ID 不得重复')
    conflicts = {c.id: c.model_copy(deep=True) for c in state.conflicts}
    prior_reviews = {r.knowledge_id: r for r in state.pending_reviews}
    invalidated, required, replaced_ids = {}, set(), set()

    def check(ids):
        if any(s not in sources for s in ids):
            raise ValueError('输出引用了不存在的课堂来源')

    def correction(ids):
        check(ids)
        if not ids or not all(s.startswith('t') for s in ids) or not set(ids) & current_ids:
            raise ValueError('明确撤回、纠正或替代需要本轮教师来源 correction_sources')

    for kid, item in updates.items():
        check(item.sources)
        check(item.correction_sources)
        check(item.independent_sources)
        if not item.reason.strip():
            raise ValueError('每条知识更新必须提供非空 reason 状态依据')
        old = original.get(kid)
        if old is None and kid.startswith('p'):
            raise ValueError('不能凭空新增画像前置知识 p')
        if item.revision != (old.revision if old else 1):
            raise ValueError('知识 revision 必须使用上下文当前修订号；新条目使用 1')
        if old and old.status == 'invalid' and semantic(old) != semantic(item):
            raise ValueError('已失效旧条目不能恢复或改写；重新采用需创建新 ID')
        if item.status == 'invalid' and old is None:
            raise ValueError('失效必须定位已有知识条目')
        if len({d.id for d in item.dependencies}) != len(item.dependencies):
            raise ValueError('同一知识不能重复依赖同一前提')
        is_correction = (item.status == 'invalid' and old and old.status != 'invalid') or item.supersedes
        if item.correction_sources and old and (old.text != item.text or old.conditions != item.conditions):
            is_correction = True
        if is_correction:
            correction(item.correction_sources)
        if old and is_correction:
            invalidated[kid] = (item.correction_sources, kid)
        if old and {d.id for d in old.dependencies} - {d.id for d in item.dependencies}:
            if item.status == 'invalid':
                item.dependencies = [d.model_copy() for d in old.dependencies]
            elif not (is_correction or (set(item.independent_sources) <= set(item.sources)
                    and any(s.startswith('t') and s in current_ids for s in item.independent_sources))):
                raise ValueError('移除旧推导依赖需要 independent_sources 引用教师独立解释；前提失效时保留依赖并标 unclear')
        if item.id in item.supersedes:
            if old is None:
                raise ValueError('纠正必须关联到已有知识条目')
            item.supersedes = [i for i in item.supersedes if i != kid]
        known[kid] = item

    # 先建立整轮集合，替代关系不依赖模型条目顺序。
    for kid, item in updates.items():
        for old_id in item.supersedes:
            if old_id not in original:
                raise ValueError('纠正必须关联到已有的其他知识条目')
            if old_id in updates and updates[old_id].status != 'invalid':
                raise ValueError('被替代旧条目不能在同轮继续作为有效知识更新')
            old = known[old_id]
            replaced_ids.add(old_id)
            if old_id in invalidated:
                invalidated[old_id] = (item.correction_sources, kid)
            if old.status != 'invalid':
                known[old_id] = old.model_copy(update={
                    'status': 'invalid', 'reason': item.reason,
                    'correction_sources': item.correction_sources})
                invalidated[old_id] = (item.correction_sources, kid)

    if len({c.id for c in result.conflict_updates}) != len(result.conflict_updates):
        raise ValueError('同轮冲突 ID 不得重复')
    for c in result.conflict_updates:
        check(c.sources)
        check(c.resolution_sources)
        if c.left_id == c.right_id or c.left_id not in known or c.right_id not in known:
            raise ValueError('冲突必须关联两个不同的已有或本轮新增知识')
        previous = conflicts.get(c.id)
        if previous and (previous.left_id, previous.right_id) != (c.left_id, c.right_id):
            raise ValueError('已有冲突不能更换双方；新矛盾需新 ID')
        if c.status == 'open':
            if previous and previous.status == 'resolved':
                raise ValueError('已解决冲突不静默重开；新矛盾需新 ID')
            if any(known[k].status == 'invalid' for k in (c.left_id, c.right_id)):
                raise ValueError('已失效条目不能新建未解决冲突')
            if not all(set(c.sources) & set(known[k].sources) for k in (c.left_id, c.right_id)):
                raise ValueError('冲突 sources 必须覆盖双方知识的来源')
        else:
            if not previous:
                raise ValueError('不能解决不存在的冲突')
            correction(c.resolution_sources)
            if not c.resolution.strip():
                raise ValueError('解决冲突需要 resolution 说明')
            required.update((c.left_id, c.right_id))
        conflicts[c.id] = c.model_copy(deep=True)

    for cid, c in conflicts.items():
        if c.status != 'open':
            continue
        withdrawn = [k for k in (c.left_id, c.right_id) if known[k].status == 'invalid']
        if withdrawn:
            evidence = list(dict.fromkeys(s for k in withdrawn for s in known[k].correction_sources))
            c.status, c.resolution = 'resolved', '冲突一方被教师明确撤回或纠正，保留方需要重新判断'
            c.resolution_sources = evidence
            required.update((c.left_id, c.right_id))
            continue
        for kid in (c.left_id, c.right_id):
            item = known[kid]
            item.status = 'conflict'
            item.reason = c.reason
            item.sources = list(dict.fromkeys(item.sources + c.sources))[:12]

    open_ids = {kid for c in conflicts.values() if c.status == 'open' for kid in (c.left_id, c.right_id)}
    for kid, item in updates.items():
        if item.status == 'conflict' and kid not in open_ids:
            raise ValueError('conflict 必须关联未解决冲突记录 conflict_updates')

    order = dependency_order(known)
    pending = {}
    for kid in order:
        item, old = known[kid], original.get(kid)
        explicit = kid in updates
        for dep in item.dependencies:
            parent = known[dep.id]
            if explicit:
                before = original.get(dep.id)
                accepted = {1} if before is None else {before.revision, parent.revision}
                if dep.revision not in accepted:
                    raise ValueError('依赖 revision 必须匹配当前前提版本')
                dep.revision = parent.revision
        parents = [known[d.id] for d in item.dependencies]
        restricted = any(p.status in ('unclear', 'conflict', 'invalid') or p.id in pending for p in parents)
        if explicit and item.status in ('understood', 'tentative') and restricted:
            raise ValueError('不能用缺失、冲突、失效或待复核前提生成可用结论；请提供独立依据或标 unclear')
        if item.status == 'understood' and any(p.status == 'tentative' for p in parents):
            item.status = 'tentative'
            item.reason = ('依赖未确认前提；' + item.reason)[:400]
        if old:
            item.revision = old.revision + int(semantic(old) != semantic(item))
        else:
            item.revision = 1
        stale = any(dep.revision != known[dep.id].revision or dep.id in pending
                    or (dep.id in replaced_ids and not explicit) for dep in item.dependencies)
        unresolved = kid in required and not explicit
        if item.status != 'invalid' and (stale or unresolved or (kid in prior_reviews and not explicit)):
            prior = prior_reviews.get(kid)
            dependency_revisions = {d.id: known[d.id].revision for d in item.dependencies}
            same = prior and prior.revision == item.revision and prior.dependency_revisions == dependency_revisions
            triggers = prior.trigger_sources if same else list(dict.fromkeys(
                (prior.trigger_sources if prior else []) + sorted(current_ids)))
            pending[kid] = KnowledgeReview(knowledge_id=kid, revision=item.revision,
                reason='关联条件或前提变化，需要重新判断', trigger_sources=triggers, dependency_revisions=dependency_revisions,
                status=prior.status if same else 'pending', error=prior.error if same else '')
        if old and item != old:
            proof, replacement = invalidated.get(kid, ([], kid))
            state.knowledge_history.append(KnowledgeRevision(version=state.version + 1,
                previous=old, replacement_id=replacement, invalidated=bool(proof),
                change_reason=item.reason, change_sources=list(dict.fromkeys(proof or item.sources))))
    state.knowledge = [known[k] for k in known]
    state.conflicts = list(conflicts.values())
    state.pending_reviews = list(pending.values())
