import asyncio
from copy import deepcopy
from backend import config
from backend.llm import ModelError
from backend.schemas import Lesson, Preparation, StudentResult
from backend.session import Session


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


class FakeModel:
    def __init__(self, fail_once=False):
        self.fail_once=fail_once
        self.batches=[]

    async def generate(self,phase,system,payload,output_type):
        if phase=='preparation':
            return Preparation(scope=['课堂主题'],boundary_note='待学范围不是已知知识')
        ids=[s['id'] for s in payload['new_teacher_segments']]
        self.batches.append(ids)
        if self.fail_once:
            self.fail_once=False
            raise ModelError('模拟网络失败')
        return StudentResult.model_validate({'knowledge_updates':[{'id':'k'+ids[-1], 'text':'暂定理解','status':'tentative','sources':ids}],
            'addressed':True,'candidate':{'kind':'answer','text':'目前只是大概理解。','sources':ids}})

    async def close(self): pass


def test_failed_batch_retries_before_later_content(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    async def run():
        events=[]
        async def emit(e):events.append(e)
        session=Session(emit,None)
        await session.llm.close()
        fake=FakeModel(True);session.llm=fake
        try:
            await session.start(Lesson(topic='任意主题'),tts=False,asr_review=False)
            await session.add_transcript('第一段');await session.flush()
            await until(lambda:session.failed_batch is not None)
            assert session.state.version==0
            await session.add_transcript('第二段');await session.flush()
            session.retry_event.set()
            await until(lambda:session.state.version==2)
            assert fake.batches==[['t1'],['t1'],['t2']]
            assert session.processed_ids=={'t1','t2'}
            assert not [e for e in events if e['type']=='reply']
        finally:await session.close()
    asyncio.run(run())


def test_mute_updates_state_without_backlog_speech(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    async def run():
        events=[]
        async def emit(e):events.append(e)
        session=Session(emit,None);await session.llm.close();session.llm=FakeModel()
        try:
            await session.start(Lesson(topic='任意主题'),muted=True,tts=False,asr_review=False)
            await session.add_transcript('你理解了吗');await session.flush()
            await until(lambda:session.state.version==1)
            await session.try_speak(10)
            assert session.candidate is None
            await session.set_muted(False);await session.try_speak(10)
            assert not [e for e in events if e['type']=='reply']
        finally:await session.close()
    asyncio.run(run())


def test_new_input_invalidates_previous_candidate(tmp_path, monkeypatch):
    monkeypatch.setattr(config,'ROOT',tmp_path)
    async def run():
        events=[]
        async def emit(e):events.append(e)
        session=Session(emit,None);await session.llm.close();session.llm=FakeModel()
        try:
            await session.start(Lesson(topic='任意主题'),tts=False,asr_review=False)
            await session.add_transcript('你理解了吗');await session.flush()
            await until(lambda:session.state.version==1)
            await session.add_transcript('我再补充一下。')
            await session.try_speak(10)
            assert not [e for e in events if e['type']=='reply']
        finally:await session.close()
    asyncio.run(run())


def test_invalid_source_retry_receives_feedback_without_committing_bad_state(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)

    class InvalidSourceModel(FakeModel):
        def __init__(self):
            super().__init__()
            self.payloads = []

        async def generate(self, phase, system, payload, output_type):
            if phase == 'preparation':
                return await super().generate(phase, system, payload, output_type)
            self.payloads.append(deepcopy(payload))
            if len(self.payloads) == 1:
                return StudentResult.model_validate({
                    'knowledge_updates': [{'id': 'k1', 'text': '列表可修改',
                                           'status': 'tentative', 'sources': ['t1']}],
                    'addressed': True,
                    'candidate': {'kind': 'answer', 'text': '我会选择列表。', 'sources': ['k1']}})
            feedback = payload.get('retry_feedback', {})
            assert feedback.get('allowed_source_ids') == ['t1']
            assert '不存在的课堂来源' in feedback.get('error', '')
            return await super().generate(phase, system, payload, output_type)

    async def run():
        events = []
        async def emit(event):
            events.append(event)
        session = Session(emit, None)
        await session.llm.close()
        model = InvalidSourceModel()
        session.llm = model
        try:
            await session.start(Lesson(topic='列表与元组'), tts=False, asr_review=False)
            await session.add_transcript('列表可修改，你会选择哪个？')
            await session.flush()
            await until(lambda: session.failed_batch is not None)
            assert session.state.version == 0
            assert session.processed_ids == set()
            assert not any(e['type'] == 'reply' for e in events)
            session.retry_event.set()
            await until(lambda: session.state.version == 1)
            assert session.processed_ids == {'t1'}
            assert len(model.payloads) == 2
        finally:
            await session.close()
    asyncio.run(run())
