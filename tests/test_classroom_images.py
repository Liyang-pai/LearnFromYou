"""Offline screenshot input tests: no ASR, production logs or paid model calls."""
import asyncio
import base64
import io
import json
from types import SimpleNamespace
import pytest

import httpx
from fastapi.testclient import TestClient

from backend import app as service, config
from backend.llm import ModelClient
from backend.schemas import Lesson, Preparation, StudentResult
from backend.session import Session
from test_session import until


PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=')


async def make_session(tmp_path, monkeypatch, model=None):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    events = []
    async def send(e): events.append(e)
    session = Session(send, None)
    await session.llm.close()
    class QuietModel:
        async def generate(self, phase, system, payload, output_type, **kwargs):
            return Preparation(scope=['课堂'], boundary_note='待学') if phase == 'preparation' else StudentResult()
        async def close(self): pass
    session.llm = model or QuietModel()
    await session.start(Lesson(topic='看图讲解', student_profile='已有知识：会整数加法。'), tts=False, asr_review=False)
    return session, events


def test_upload_route_only_changes_image_after_success(tmp_path, monkeypatch):
    calls = []
    class UploadSession:
        id = 'a' * 12
        ready, closed, finishing = True, False, False
        async def set_image(self, data):
            calls.append(data)
            return {'id': 'b' * 64, 'url': '/preview', 'width': 1, 'height': 1}
        async def close(self): pass
    monkeypatch.setattr(service, 'active_session', UploadSession())
    with TestClient(service.app) as client:
        response = client.post('/api/sessions/' + 'a' * 12 + '/image', content=PNG,
                               headers={'Content-Type': 'image/png'})
        assert response.status_code == 200, response.text
        assert response.json()['width'] == 1
        assert calls == [PNG]
        blocked = client.post('/api/sessions/' + 'a' * 12 + '/image', content=PNG,
                              headers={'Origin': 'https://other.example'})
        assert blocked.status_code == 403
        assert len(calls) == 1
    monkeypatch.setattr(service, 'active_session', None)


def test_same_class_image_invalid_upload_and_duplicates(tmp_path, monkeypatch):
    async def run():
        session, events = await make_session(tmp_path, monkeypatch)
        try:
            first = await session.set_image(PNG)
            assert first['width'] == first['height'] == 1
            old_revision = session.revision
            try:
                await session.set_image(b'not an image')
                assert False, 'invalid image accepted'
            except ValueError:
                pass
            assert session.current_image == first['id']
            assert session.revision == old_revision
            again = await session.set_image(PNG)
            assert first['id'] == again['id']
            assert len(session.images) == 1
            assert session.state.version == 0 and not session.pending
            assert not any(e['type'] == 'reply' for e in events)
        finally: await session.close()
    asyncio.run(run())


def test_question_keeps_its_image_when_upload_changes_during_request(tmp_path, monkeypatch):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()
        calls = []
        class HeldModel:
            async def generate(self, phase, system, payload, output_type, **kwargs):
                if phase == 'preparation': return Preparation(scope=['课堂'], boundary_note='待学')
                calls.append((payload, kwargs['images']))
                started.set(); await release.wait()
                sid = payload['new_teacher_segments'][-1]['id']
                return StudentResult(addressed=True, candidate={'kind': 'answer', 'text': '看到第一张图。', 'sources': [sid]})
            async def close(self): pass
        session, events = await make_session(tmp_path, monkeypatch, HeldModel())
        try:
            from PIL import Image
            second = io.BytesIO(); Image.new('RGB', (2, 2), 'blue').save(second, format='PNG')
            first = await session.set_image(PNG)
            await session.add_transcript('请回答，这张图是什么？'); await session.flush()
            await asyncio.wait_for(started.wait(), 5)
            newer = await session.set_image(second.getvalue())
            release.set(); await until(lambda: session.state.version == 1)
            assert first['id'] != newer['id']
            assert calls[0][0]['new_teacher_segments'][0]['image_id'] == first['id']
            assert len(calls[0][1]) == 1 and calls[0][1][0].id == first['id']
            assert session.current_image == newer['id']
            await session.try_speak(10)
            assert not any(e['type'] == 'reply' for e in events), 'old-image answer must not speak over new material'
        finally: await session.close()
    asyncio.run(run())


def test_image_wire_is_multimodal_and_debug_does_not_log_base64(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'API_KEY', 'offline-key')
    requests, events = [], []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})
    async def run():
        from backend.classroom_images import save_image
        image = save_image(tmp_path, 'a' * 12, PNG)
        async def emit(kind, data): events.append((kind, data))
        model = ModelClient(emit); await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            await model.generate('student', '学生规则', {'new_teacher_segments': []}, StudentResult, images=[image])
            await model.generate('student', '学生规则', {'new_teacher_segments': []}, StudentResult)
        finally: await model.close()
    asyncio.run(run())
    parts = requests[0]['messages'][-1]['content']
    assert [p['type'] for p in parts] == ['text', 'image_url']
    assert parts[1]['image_url']['url'].startswith('data:image/png;base64,')
    assert requests[0]['model'] == config.IMAGE_MODEL
    assert requests[1]['model'] == config.MODEL
    assert isinstance(requests[1]['messages'][-1]['content'], str)
    assert base64.b64encode(PNG).decode() not in json.dumps(events)


def test_current_image_read_clear_and_local_preview(tmp_path, monkeypatch):
    from backend.classroom_images import save_image
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    image = save_image(tmp_path, 'a' * 12, PNG)
    class ImageSession:
        id = 'a' * 12
        ready, closed, finishing = True, False, False
        current_image = image.id
        images = {image.id: image}
        async def clear_image(self): self.current_image = None
        async def close(self): pass
    session = ImageSession()
    monkeypatch.setattr(service, 'active_session', session)
    with TestClient(service.app) as client:
        endpoint = '/api/sessions/' + session.id + '/image'
        assert client.get(endpoint).json()['id'] == image.id
        preview = client.get(image.public(session.id)['url'])
        assert preview.status_code == 200 and preview.content == PNG
        assert preview.headers['cross-origin-resource-policy'] == 'same-origin'
        assert client.get(image.public(session.id)['url'], headers={'Origin': 'https://other.example'}).status_code == 403
        assert client.post(endpoint + '/clear').status_code == 200
        assert client.get(endpoint).json() is None
        assert client.get(image.public('b' * 12)['url']).status_code == 404
    monkeypatch.setattr(service, 'active_session', None)


def test_storage_rejects_invalid_paths_bomb_animation_and_quota(tmp_path, monkeypatch):
    from backend import classroom_images as storage
    from PIL import Image
    with pytest.raises(ValueError): storage.save_image(tmp_path, '../outside', PNG)
    with pytest.raises(ValueError): storage.save_image(tmp_path, 'a' * 12, b'bad')
    big = io.BytesIO(); Image.new('RGB', (11, 11)).save(big, format='PNG')
    monkeypatch.setattr(storage, 'MAX_IMAGE_PIXELS', 100)
    with pytest.raises(ValueError, match='尺寸'): storage.save_image(tmp_path, 'a' * 12, big.getvalue())
    monkeypatch.setattr(storage, 'MAX_CLASSROOM_BYTES', 1)
    with pytest.raises(ValueError, match='64 MiB'): storage.save_image(tmp_path, 'a' * 12, PNG)
    with pytest.raises(ValueError): storage.find_image(tmp_path, 'a' * 12, '../outside')


def test_missing_or_changed_saved_image_has_recoverable_error(tmp_path):
    from backend.classroom_images import save_image
    image = save_image(tmp_path, 'a' * 12, PNG)
    image.path.unlink()
    with pytest.raises(ValueError, match='重新上传'): image.data_url()


def test_oversize_and_other_class_upload_never_reaches_session(monkeypatch):
    class ImageSession:
        id = 'a' * 12
        ready, closed, finishing = True, False, False
        async def set_image(self, data): pytest.fail('must reject before storing')
        async def close(self): pass
    monkeypatch.setattr(service, 'active_session', ImageSession())
    with TestClient(service.app) as client:
        assert client.post('/api/sessions/' + 'b' * 12 + '/image', content=PNG).status_code == 409
        response = client.post('/api/sessions/' + 'a' * 12 + '/image', content=b'x' * (5 * 1024 * 1024 + 1))
        assert response.status_code == 413
    monkeypatch.setattr(service, 'active_session', None)


def test_image_repair_keeps_picture_and_http_failure_never_retries(tmp_path, monkeypatch):
    from backend.classroom_images import save_image
    from backend.llm import ModelError
    monkeypatch.setattr(config, 'API_KEY', 'offline-key')
    image = save_image(tmp_path, 'a' * 12, PNG)
    async def run():
        calls = []
        def respond(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'choices':[{'message':{'content':'not json' if len(calls)==1 else '{}'}}]})
        async def emit(*args): pass
        model = ModelClient(emit); await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            await model.generate('student', '学生规则', {}, StudentResult, images=[image])
            assert len(calls) == 2
            assert calls[0]['messages'][-1]['content'][1] == calls[1]['messages'][-3]['content'][1]
            await model.http.aclose(); calls.clear()
            def denied(request):
                calls.append(request)
                return httpx.Response(400)
            model.http = httpx.AsyncClient(transport=httpx.MockTransport(denied))
            with pytest.raises(ModelError, match='HTTP 400'):
                await model.generate('student', '学生规则', {}, StudentResult, images=[image])
            assert len(calls) == 1
        finally: await model.close()
    asyncio.run(run())


def test_microphone_segments_keep_capture_image_and_mixed_text_is_split(tmp_path, monkeypatch):
    async def run():
        session, _ = await make_session(tmp_path, monkeypatch)
        for task in session.tasks:
            task.cancel()
        try:
            await session.set_image(PNG)
            # This speech was captured before the first upload, but ASR finished
            # later: an explicit None must not silently select the newer image.
            await session.add_transcript('先前录音', 'microphone', image_id=None)
            await session.add_transcript('现在看图')
            await session.flush()
            batches = [session.teaching_queue.get_nowait(), session.teaching_queue.get_nowait()]
            assert 'image_id' not in batches[0][0]
            assert batches[1][0]['image_id'] == session.current_image
            await session.clear_image()
            assert session.current_image is None and session.state.version == 0
            assert session.images, 'clearing visible material must not erase historical files'
        finally: await session.close()
    asyncio.run(run())


def test_audio_capture_offsets_select_old_picture_even_when_vad_finishes_later(tmp_path, monkeypatch):
    import numpy as np
    async def run():
        session, _ = await make_session(tmp_path, monkeypatch)
        session.audio_enabled = True
        session.audio_rate = 16000
        try:
            session.current_image = 'old-picture'
            await session.accept_audio(np.zeros(8000, dtype='<f4').tobytes())
            session.current_image = 'new-picture'
            await session.accept_audio(np.zeros(8000, dtype='<f4').tobytes())
            assert session.image_for_audio_start(0) == 'old-picture'
            assert session.image_for_audio_start(7999) == 'old-picture'
            assert session.image_for_audio_start(8000) == 'new-picture'
        finally: await session.close()
    asyncio.run(run())


def test_report_knows_text_analysis_does_not_include_visual_content(tmp_path):
    from backend.teaching_report import read_classroom
    from backend.report_pipeline import compact
    logs = tmp_path / 'logs'; logs.mkdir()
    sid = 'a' * 12
    events = [
        {'type':'transcript','data':{'id':'t1','text':'看图里的条件。','image_id':'b'*64}},
        {'type':'finished','data':{'session_id':sid,'lesson':{'topic':'看图讲解'},'state':{},'unprocessed_sources':[]}},
    ]
    (logs / (sid + '.jsonl')).write_text('\n'.join(json.dumps(e,ensure_ascii=False) for e in events),encoding='utf-8')
    payload = compact(read_classroom(tmp_path, sid))
    assert payload['lesson']['has_visual_material'] is True
    assert payload['turns'][0]['image_id'] == 'b' * 64


def test_audio_worker_uses_segment_start_when_two_vad_transitions_share_frame(tmp_path, monkeypatch):
    import numpy as np
    from PIL import Image
    async def run():
        calls = []
        class Model:
            async def generate(self, phase, system, payload, output_type, **kwargs):
                if phase == 'preparation': return Preparation(scope=['课堂'], boundary_note='待学')
                calls.append((payload, kwargs['images']))
                return StudentResult()
            async def close(self): pass
        class Audio:
            calls = 0
            segment_starts = []
            def feed(self, samples):
                self.calls += 1
                if self.calls == 1: return [], [True], True
                self.segment_starts = [0]
                return [np.zeros(8)], [False, True], True
        class Recognizer:
            def transcribe(self, samples): return '请回答上一段图里的条件。'
            def close(self): pass
        session, _ = await make_session(tmp_path, monkeypatch, Model())
        try:
            first = await session.set_image(PNG)
            session.recognizer = Recognizer(); session.audio = Audio(); session.audio_enabled = True
            await session.accept_audio(np.zeros(8000, dtype='<f4').tobytes())
            await until(lambda: session.audio.calls == 1)
            second = io.BytesIO(); Image.new('RGB',(2,2),'green').save(second,format='PNG')
            await session.set_image(second.getvalue())
            await session.accept_audio(np.zeros(8000, dtype='<f4').tobytes())
            await until(lambda: bool(session.pending))
            await session.flush(); await until(lambda: session.state.version == 1)
            assert calls[0][0]['new_teacher_segments'][0]['image_id'] == first['id']
            assert calls[0][1][0].id == first['id']
        finally: await session.close()
    asyncio.run(run())
