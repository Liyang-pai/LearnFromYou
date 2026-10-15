"""验证五状态的强制规则、版本依赖、画像修订与原子性，不用模拟结果代替语义验收。"""
import pytest
from backend.cognition import seed_prerequisites
from backend.policy import apply_result
from backend.schemas import Knowledge, StudentState, StudentResult, KnowledgeConflict, KnowledgeDependency

SOURCES = {k: {'id': k, 'text': k} for k in ['p1', 't1', 't2', 't3', 't4']}


def k(id='a', status='understood', **extra):
    return Knowledge(**{'id': id, 'text': id + '规则', 'status': status, 'sources': ['t1'],
                        'reason': '教师完整解释，已有基础足够', **extra})


def apply(state, updates=(), conflicts=(), current=('t2',), **extra):
    return apply_result(state, StudentResult(knowledge_updates=list(updates), conflict_updates=list(conflicts), **extra),
                        SOURCES, set(current))[0]


def pair():
    return KnowledgeConflict(id='c1', left_id='p1', right_id='a', conditions='同样的红盒',
                             reason='相同输入却有两种互斥规则', sources=['p1', 't1'])


def seeded():
    state = StudentState()
    seed_prerequisites(state, [{'id': 'p1', 'text': '红盒输入加1'}])
    return state


def test_conflict_restricts_both_and_keeps_unrelated_rule():
    state = apply(seeded(), [k(), k('unrelated')], [pair()])
    assert {x.id for x in state.knowledge if x.status == 'conflict'} == {'p1', 'a'}
    assert [x['id'] for x in state.learning_context()['knowledge']] == ['unrelated']
    assert len(state.conflicts) == 1


def test_conflict_requires_record_and_sources_from_both_sides():
    with pytest.raises(ValueError, match='conflict_updates'):
        apply(StudentState(), [k(status='conflict')])
    with pytest.raises(ValueError, match='覆盖双方'):
        apply(seeded(), [k()], [pair().model_copy(update={'sources': ['t1']})])


def test_conflict_resolution_can_preserve_both_under_different_conditions():
    state = apply(seeded(), [k()], [pair()])
    resolution = state.conflicts[0].model_copy(update={'status': 'resolved', 'resolution': '分别讨论红盒和蓝盒',
                                                       'resolution_sources': ['t2']})
    updates = [x.model_copy(update={'status': 'understood', 'conditions': '红盒' if x.id == 'p1' else '蓝盒',
                                   'reason': '教师区分了适用条件', 'sources': [*x.sources, 't2']}) for x in state.knowledge]
    state = apply(state, updates, [resolution])
    assert len(state.learning_context()['knowledge']) == 2
    assert not state.pending_reviews
    assert not any(h.invalidated for h in state.knowledge_history)


def test_direct_invalidation_requires_teacher_and_keeps_original_profile():
    state = seeded(); before = state.model_dump()
    with pytest.raises(ValueError, match='correction_sources'):
        apply(state, [state.knowledge[0].model_copy(update={'status': 'invalid'})])
    assert state.model_dump() == before
    state = apply(state, [state.knowledge[0].model_copy(update={'status': 'invalid', 'correction_sources': ['t2'],
                                                             'reason': '教师撤回旧规则'})])
    assert state.knowledge[0].status == 'invalid'
    assert state.knowledge_history[0].previous.status == 'understood'
    assert state.knowledge_history[0].invalidated
    assert state.learning_context()['knowledge'] == []
    with pytest.raises(ValueError, match='不能恢复'):
        apply(state, [state.knowledge[0].model_copy(update={'status': 'understood'})])


def test_same_id_correction_archives_invalidated_version_and_requires_current_revision():
    state = StudentState(knowledge=[k()])
    corrected = k(text='纠正后的规则', correction_sources=['t2'], sources=['t2'], reason='教师明确纠正')
    state = apply(state, [corrected])
    assert state.knowledge[0].revision == 2
    assert state.knowledge_history[0].invalidated
    assert state.knowledge_history[0].previous.text == 'a规则'
    with pytest.raises(ValueError, match='修订号'):
        apply(state, [corrected])


def test_unconfirmed_dependency_propagates_transitively_regardless_of_update_order():
    a = k(status='tentative', reason='省略步骤可补出')
    b = k('b', dependencies=[{'id': 'a', 'revision': 1}])
    c = k('c', dependencies=[{'id': 'b', 'revision': 1}])
    state = apply(StudentState(), [c, b, a])
    assert all(x.status == 'tentative' for x in state.knowledge)
    assert state.learning_context()['knowledge'] == []
    assert len(state.learning_context()['tentative_knowledge']) == 3
    b = next(x for x in state.knowledge if x.id == 'b')
    state = apply(state, [b.model_copy(update={'status': 'understood', 'dependencies': [],
        'sources': ['t2'], 'independent_sources': ['t2'], 'reason': '教师给出了无需原推断的完整独立解释'})])
    assert next(x for x in state.knowledge if x.id == 'b').status == 'understood'
    assert next(x for x in state.knowledge if x.id == 'a').status == 'tentative'
    assert {r.knowledge_id for r in state.pending_reviews} == {'c'}


def test_invalid_parent_queues_transitive_dependants_and_review_can_retain_unclear():
    state = apply(StudentState(), [k(), k('b', dependencies=[{'id': 'a', 'revision': 1}]),
                                   k('c', dependencies=[{'id': 'b', 'revision': 1}])])
    state = apply(state, [state.knowledge[0].model_copy(update={'status': 'invalid', 'correction_sources': ['t2']})])
    assert {r.knowledge_id for r in state.pending_reviews} == {'b', 'c'}
    assert state.learning_context()['knowledge'] == []
    b = next(x for x in state.knowledge if x.id == 'b')
    state = apply(state, [b.model_copy(update={'status': 'unclear', 'dependencies': [KnowledgeDependency(id='a', revision=2)],
                                              'reason': '原来的唯一前提撤回，缺少新依据'})])
    assert {r.knowledge_id for r in state.pending_reviews} == {'c'}
    assert next(x for x in state.knowledge if x.id == 'b').status == 'unclear'


def test_invalidating_conflict_side_resolves_relation_and_reviews_survivor():
    state = apply(seeded(), [k()], [pair()])
    old = next(x for x in state.knowledge if x.id == 'a')
    state = apply(state, [old.model_copy(update={'status': 'invalid', 'correction_sources': ['t2']})])
    assert state.conflicts[0].status == 'resolved'
    assert {r.knowledge_id for r in state.pending_reviews} == {'p1'}


@pytest.mark.parametrize('dep', ['self', 'cycle', 'missing', 'version'])
def test_bad_dependency_rejects_atomically(dep):
    a, b = k(), k('b')
    if dep == 'self': a = k(dependencies=[{'id': 'a', 'revision': 1}])
    if dep == 'cycle':
        a = k(dependencies=[{'id': 'b', 'revision': 1}]); b = k('b', dependencies=[{'id': 'a', 'revision': 1}])
    if dep == 'missing': a = k(dependencies=[{'id': 'absent', 'revision': 1}])
    if dep == 'version': b = k('b', dependencies=[{'id': 'a', 'revision': 99}])
    state = StudentState(); before = state.model_dump()
    with pytest.raises(ValueError): apply(state, [a, b])
    assert state.model_dump() == before


def test_reason_only_does_not_bump_revision_or_trigger_review():
    state = apply(StudentState(), [k(), k('b', dependencies=[{'id': 'a', 'revision': 1}])])
    state = apply(state, [state.knowledge[0].model_copy(update={'reason': '补充记录依据'})])
    assert state.knowledge[0].revision == 1 and not state.pending_reviews
    assert state.knowledge_history[-1].change_reason == '补充记录依据'


def test_missing_reason_rejected_but_old_record_can_still_be_read():
    old = Knowledge(id='a', text='旧记录', status='conflict', sources=['t1'])
    assert StudentState(knowledge=[old]).knowledge[0].status == 'conflict'
    with pytest.raises(ValueError, match='reason'): apply(StudentState(), [old])


def test_failed_review_not_retried_for_unrelated_teacher_content():
    state = apply(StudentState(), [k(), k('b', dependencies=[{'id': 'a', 'revision': 1}])])
    state = apply(state, [state.knowledge[0].model_copy(update={'status': 'invalid', 'correction_sources': ['t2']})])
    state.pending_reviews[0].status = 'failed'
    state = apply(state, current=('t3',))
    assert state.pending_reviews[0].status == 'failed'
    assert state.pending_reviews[0].trigger_sources == ['t2']


def test_multiroute_understanding_does_not_require_clearing_unrelated_gap():
    state = apply(StudentState(), [k(status='unclear', reason='不懂坐标'), k('graph', 'unclear', reason='不会读图')])
    state = apply(state, [state.knowledge[0].model_copy(update={'status': 'understood', 'text': '表格中输入增加1输出增加2',
        'sources': ['t2'], 'reason': '通过数量表理解变化关系，无需先学坐标'})])
    assert state.knowledge[0].status == 'understood'
    assert state.knowledge[1].status == 'unclear'


def test_unrelated_split_fact_survives_explicit_replacement():
    state = apply(StudentState(), [k('sides'), k('area')])
    state = apply(state, [k('correct_area', supersedes=['area'], correction_sources=['t2'], sources=['t2'])])
    assert {x.id: x.status for x in state.knowledge} == {'sides': 'understood', 'area': 'invalid', 'correct_area': 'understood'}


def test_candidate_cannot_use_blocked_knowledge_as_a_fact():
    state = apply(StudentState(), [k(status='unclear', reason='关键定义缺失')])
    with pytest.raises(ValueError, match='candidate.knowledge_ids'):
        apply_result(state, StudentResult(candidate={'kind': 'answer', 'text': '确定答案',
            'sources': ['t1'], 'knowledge_ids': ['a']}), SOURCES, {'t2'})


def test_cannot_erase_dependency_by_reusing_historical_correction_source():
    state = apply(StudentState(), [k(status='tentative'), k('b', dependencies=[{'id':'a','revision':1}])])
    b = next(x for x in state.knowledge if x.id == 'b')
    with pytest.raises(ValueError, match='independent_sources'):
        apply(state, [b.model_copy(update={'status':'understood', 'dependencies':[], 'correction_sources':['t1']})])


def test_later_replacement_of_already_invalid_rule_rechecks_dependants():
    state = apply(StudentState(), [k(), k('b', dependencies=[{'id':'a','revision':1}])])
    state = apply(state, [state.knowledge[0].model_copy(update={'status':'invalid', 'correction_sources':['t2']})])
    b=next(x for x in state.knowledge if x.id=='b')
    state=apply(state,[b.model_copy(update={'status':'unclear','dependencies':[KnowledgeDependency(id='a',revision=2)]})])
    assert not state.pending_reviews
    state=apply(state,[k('new_a',supersedes=['a'],sources=['t3'],correction_sources=['t3'])],current=('t3',))
    assert {r.knowledge_id for r in state.pending_reviews} == {'b'}


def test_explicit_old_invalidation_and_new_replacement_link_the_history():
    state=StudentState(knowledge=[k()])
    state=apply(state,[k(status='invalid',correction_sources=['t2']),
                       k('new',supersedes=['a'],correction_sources=['t2'])])
    assert state.knowledge_history[0].invalidated
    assert state.knowledge_history[0].replacement_id=='new'
