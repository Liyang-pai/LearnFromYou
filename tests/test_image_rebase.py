"""Cross-branch invariants: image evidence, mute recovery and shared transport."""
import asyncio
from copy import deepcopy
import io
import pytest
from PIL import Image
from backend.schemas import ResponseRequest, ResumeResult
from test_classroom_images import PNG, make_session
from test_llm_recovery import client, envelope


def test_resume_transport_keeps_images_history_runtime_and_bounded_recovery(monkeypatch, tmp_path):
    from backend.classroom_images import save_image
    async def run():
        image = save_image(tmp_path, 'a' * 12, PNG)
        model, calls, events = await client(monkeypatch, [envelope('{}', 'length'), envelope('{}')])
        try:
            await model.generate('student_resume', '规则', {}, ResumeResult,
                                 history=[{'role':'assistant','content':'在听'}], images=[image])
            assert len(calls) == 2
            assert [c['max_tokens'] for c in calls] == [6000, 8000]
            assert calls[0]['messages'][1]['content'] == '在听'
            assert calls[0]['messages'][-1]['content'][1]['type'] == 'image_url'
            logged = [d for k,d in events if k == 'llm_request']
            assert all('runtime' in d and 'prompt_fingerprint' in d for d in logged)
            assert all('base64,' not in str(d) for d in logged)
        finally:
            await model.close()
    asyncio.run(run())


@pytest.mark.parametrize('material', ['same', 'different', 'missing', 'inflight_change'])
def test_resume_binds_task_image_and_discards_changed_snapshot(tmp_path, monkeypatch, material):
    async def run():
        session, events = await make_session(tmp_path, monkeypatch)
        for task in session.tasks:
            task.cancel()
        await asyncio.gather(*session.tasks, return_exceptions=True)
        first = await session.set_image(PNG)
        raw = io.BytesIO(); Image.new('RGB', (2,2), 'blue').save(raw, format='PNG')
        if material in ('different', 'missing'):
            await session.set_image(raw.getvalue())
        if material == 'missing':
            session.images.pop(first['id'])
        session.sources.update({
            't1': {'id':'t1','text':'回答旧题','mode':'text','revision':session.revision,'at':0,'image_id':first['id']},
            't2': {'id':'t2','text':'回答最新题','mode':'text','revision':session.revision,'at':0,'image_id':first['id']},
        })
        session.processed_ids.update(('t1','t2'))
        session.state.response_requests = [
            ResponseRequest(id='r1',text='旧题',sources=['t1']),
            ResponseRequest(id='r2',text='最新题',sources=['t2'])]
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        class Model:
            async def generate(self, phase, system, payload, output_type, **kwargs):
                calls.append((phase, deepcopy(payload), kwargs.get('images', [])))
                entered.set()
                if material == 'inflight_change':
                    await release.wait()
                obj = output_type.model_validate({'candidate': {'kind':'answer', 'text':'我在听，请继续。',
                    'sources':['t2'],'response_request_ids':['r2']}})
                return kwargs['result_validator'](obj, True)
            async def close(self): pass
        session.llm = Model()
        try:
            job = asyncio.create_task(session.resume_response())
            await entered.wait()
            if material == 'inflight_change':
                await session.set_image(raw.getvalue())
                release.set()
            await job
            phase, payload, images = calls[0]
            assert phase == 'student_resume'
            assert payload['new_teacher_segments'] == []
            if material == 'missing':
                assert not images and 'teaching_images' not in payload
            else:
                assert [im.id for im in images] == [first['id']]
                assert payload['teaching_images'] == [{'id': first['id'], 'teacher_source_ids':['t2']}]
            if material == 'inflight_change':
                assert session.candidate is None
                assert any(e['type']=='resume_response' and e['data']['status']=='stale' for e in events)
            else:
                assert session.candidate['image_id'] == session.current_image
                await session.try_speak(10)
                assert any(e['type']=='reply' for e in events)
        finally:
            await session.close()
    asyncio.run(run())
