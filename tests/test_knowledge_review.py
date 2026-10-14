"""验证内部依赖复核的自动调度、并发失效、失败重试及结束记录。"""
import asyncio
import json
import httpx
import pytest
from backend import config
from backend.llm import ModelClient, ModelError
from backend.schemas import Lesson, Preparation, KnowledgeReviewResult, StudentResult, StudentState
from backend.session import Session
from test_session import until
from test_knowledge_machine import k, apply, SOURCES


class ReviewModel:
    def __init__(self):
        self.calls = []
        self.gate = None
        self.fail = False

    async def generate(self, phase, system, payload, output_type, *, history=(), result_validator=None):
        if phase == 'preparation': return Preparation(scope=[], boundary_note='')
        if phase == 'student':
            sid = payload['new_teacher_segments'][-1]['id']
            return StudentResult(knowledge_updates=[k('fresh', sources=[sid], reason='新的独立讲授')])
        assert phase == 'knowledge_review'
        self.calls.append(payload['review_targets'])
        if self.gate: await self.gate.wait()
        if self.fail: raise ModelError('模拟内部复核失败')
        context = payload['current_state']
        known = {x['id']: x for group in ('knowledge','tentative_knowledge','inactive_knowledge') for x in context[group]}
        updates = []
        for target in payload['review_targets']:
            item = dict(known[target])
            item.update(status='unclear', reason='原前提已撤回，尚无独立解释', correction_sources=[], supersedes=[])
            item['dependencies'] = [{'id': d['id'], 'revision': known[d['id']]['revision']} for d in item['dependencies']]
            updates.append(item)
        result = KnowledgeReviewResult(knowledge_updates=updates)
        if result_validator: result_validator(result, False)
        return result

    async def close(self): pass


async def setup(tmp_path, monkeypatch, count=1):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    events = []
    async def send(event): events.append(event)
    session = Session(send, None); await session.llm.close()
    model = ReviewModel(); session.llm = model
    await session.start(Lesson(topic='规则', student_profile='普通初学者，尚未学习本课。'),
                        muted=True, tts=False, asr_review=False)
    state = StudentState(knowledge=[k(), *[k(f'b{i}', dependencies=[{'id':'a','revision':1}]) for i in range(count)]])
    session.state = apply(state, [state.knowledge[0].model_copy(update={'status':'invalid','correction_sources':['t2']})])
    for i in (1,2):
        session.sources[f't{i}'] = {'id':f't{i}','text':SOURCES[f't{i}']['text'],'mode':'text','revision':i,'at':i}
    session.processed_ids = {'t1','t2'}
    session.revision = 2
    return session, model, events


def test_automatic_review_updates_state_without_speech_or_classroom_events(tmp_path, monkeypatch):
    async def run():
        session, model, events = await setup(tmp_path, monkeypatch)
        try:
            session.knowledge_review_wake.set()
            await until(lambda: not session.state.pending_reviews)
            assert model.calls == [['b0']]
            assert next(k for k in session.state.knowledge if k.id == 'b0').status == 'unclear'
            assert not session.conversation and not session.state.recent_events
            assert not [e for e in events if e['type'] in ('reply','state')]
            assert any(e['type']=='knowledge_review' and e['data']['status']=='completed' for e in events)
        finally: await session.close()
    asyncio.run(run())


def test_review_chunks_are_limited_to_sixteen(tmp_path, monkeypatch):
    async def run():
        session, model, events = await setup(tmp_path, monkeypatch, 20)
        try:
            session.knowledge_review_wake.set()
            await until(lambda: not session.state.pending_reviews)
            assert [len(call) for call in model.calls] == [16,4]
        finally: await session.close()
    asyncio.run(run())


def test_review_failure_keeps_restriction_and_new_teaching_continues(tmp_path, monkeypatch):
    async def run():
        session, model, events = await setup(tmp_path, monkeypatch)
        try:
            model.fail = True; session.knowledge_review_wake.set()
            await until(lambda: session.state.pending_reviews[0].status == 'failed')
            assert session.state.learning_context()['knowledge'] == []
            assert not session.failed_batch
            await session.add_transcript('新的独立规则'); await session.flush()
            await until(lambda: 't3' in session.processed_ids)
            assert model.calls == [['b0']]
            assert session.state.pending_reviews[0].status == 'failed'
            model.fail = False; session.retry_knowledge_reviews()
            await until(lambda: not session.state.pending_reviews)
            assert model.calls == [['b0'],['b0']]
        finally: await session.close()
    asyncio.run(run())


def test_stale_review_cannot_overwrite_new_teacher_state(tmp_path, monkeypatch):
    async def run():
        session, model, events = await setup(tmp_path, monkeypatch)
        try:
            gate=asyncio.Event(); model.gate=gate
            session.knowledge_review_wake.set()
            await until(lambda: bool(model.calls))
            await session.add_transcript('新的独立规则'); await session.flush()
            await until(lambda: 't3' in session.processed_ids)
            gate.set()
            await until(lambda: not session.state.pending_reviews)
            assert any(k.id=='fresh' for k in session.state.knowledge)
            assert len(model.calls)==2
            assert any(e['type']=='knowledge_review' and e['data']['status']=='stale' for e in events)
        finally: await session.close()
    asyncio.run(run())


def test_queued_teacher_input_precedes_review(tmp_path, monkeypatch):
    async def run():
        session, model, events = await setup(tmp_path, monkeypatch)
        try:
            await session.add_transcript('新的独立规则')
            session.knowledge_review_wake.set()
            await asyncio.sleep(.02)
            assert not model.calls
            await session.flush()
            await until(lambda: not session.state.pending_reviews)
            completed = next(e for e in events if e['type']=='knowledge_review' and e['data']['status']=='completed')
            assert any(k['id']=='fresh' for k in completed['data']['state']['knowledge'])
        finally: await session.close()
    asyncio.run(run())


def test_finish_retains_failed_review_record(tmp_path, monkeypatch):
    async def run():
        session, model, events = await setup(tmp_path, monkeypatch)
        model.fail=True; session.knowledge_review_wake.set()
        await until(lambda: session.state.pending_reviews[0].status=='failed')
        await session.finish()
        finished=next(e['data'] for e in events if e['type']=='finished')
        assert finished['unreviewed_knowledge'][0]['knowledge_id']=='b0'
        assert finished['state']['pending_reviews'][0]['status']=='failed'
    asyncio.run(run())


def test_same_round_recheck_prevents_extra_request():
    state=apply(StudentState(),[k(),k('b',dependencies=[{'id':'a','revision':1}])])
    a=state.knowledge[0].model_copy(update={'status':'invalid','correction_sources':['t2']})
    b=state.knowledge[1].model_copy(update={'status':'unclear','reason':'教师撤回唯一依据'})
    state=apply(state,[b,a])
    assert not state.pending_reviews
    assert next(k for k in state.knowledge if k.id=='b').dependencies[0].revision==2


@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_review_model_repairs_references_atomically_with_bounded_requests(tmp_path, monkeypatch, repair_succeeds):
    async def run():
        session, _, events = await setup(tmp_path, monkeypatch)
        monkeypatch.setattr(config, 'API_KEY', 'offline-test-key')
        before = session.state.model_dump()
        requests = []

        async def respond(request):
            # 第一次无效输出不能提前写入状态；第二次请求仍看到原快照。
            assert session.state.model_dump() == before
            requests.append(json.loads(request.content))
            item = next(k for k in session.state.knowledge if k.id == 'b0').model_dump()
            item.update(status='unclear', reason='原前提已撤回，尚无独立依据',
                        dependencies=[{'id': 'a', 'revision': 2}])
            if len(requests) == 1 or not repair_succeeds:
                item['sources'] = ['t_missing']
            return httpx.Response(200, json={'choices': [{'message': {
                'content': json.dumps({'knowledge_updates': [item]})}}]})

        session.llm = ModelClient(session.emit)
        await session.llm.http.aclose()
        session.llm.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            session.knowledge_review_wake.set()
            await until(lambda: any(e['type'] == 'knowledge_review' and
                e['data']['status'] in ('completed', 'failed') for e in events))
            assert len(requests) == 2
            assert '不存在的课堂来源' in requests[1]['messages'][-1]['content']
            if repair_succeeds:
                assert not session.state.pending_reviews
                assert session.state.version == before['version'] + 1
            else:
                assert session.state.version == before['version']
                assert [k.model_dump() for k in session.state.knowledge] == before['knowledge']
                assert session.state.pending_reviews[0].status == 'failed'
            assert not session.failed_batch
            assert not session.conversation
        finally:
            await session.close()
    asyncio.run(run())
