import asyncio
import time

from fastapi.testclient import TestClient
import numpy as np
import pytest

from test_models import manager, install, FakeEngine


class FakeAudio:
    def __init__(self, path, rate):
        self.rate = rate

    def feed(self, samples):
        # Each input frame becomes a deterministic VAD segment in protocol tests.
        return [samples], [True, False], False

    def finish(self):
        return [np.zeros(1600, dtype=np.float32)]


class TextEngine(FakeEngine):
    def transcribe(self, samples, sample_rate=16000):
        assert np.isfinite(samples).all()
        return '链表 node 包含 3 个数据'


def receive(ws, kind):
    for _ in range(20):
        event = ws.receive_json()
        if event['type'] == kind:
            return event['data']
    pytest.fail(f'没有收到 {kind}')


@pytest.fixture
def debug_service(manager, monkeypatch):
    from backend import app as service
    asyncio.run(install(manager))
    asyncio.run(install(manager, 'second'))
    manager.select('first')
    engines = []
    def create(model):
        engine = TextEngine(model.id); engines.append(engine); return engine
    monkeypatch.setattr(manager, '_create_engine', create)
    monkeypatch.setattr(service, 'models', manager)
    monkeypatch.setattr(service, 'active_session', None)
    monkeypatch.setattr('asr.debug.AudioStream', FakeAudio)
    monkeypatch.setattr('backend.config.API_KEY', '')
    def forbidden(*args, **kwargs):
        pytest.fail('独立 ASR 调试不能初始化 LLM 或 TTS')
    monkeypatch.setattr('backend.session.ModelClient', forbidden)
    monkeypatch.setattr('backend.session.Speaker', forbidden)
    return service, manager, engines


def test_microphone_debug_without_llm_drains_tail_and_releases(debug_service):
    service, manager, engines = debug_service
    with TestClient(service.app, base_url='http://localhost:8765') as client:
        page = client.get('/models').text
        assert '讲给我听' in page and 'Learn from Me' in page
        assert '开发调试界面' in page and '正式前端将另行开发' in page
        assert 'ASR 转文字调试' in page
        assert client.get('/api/health').json()['asr_debug_available'] is True
        assert not client.get('/api/health').json()['model_configured']
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type': 'start', 'sample_rate': 48000})
            ready = receive(ws, 'ready')
            assert ready == {'model_id': 'first', 'sample_rate': 48000}
            assert client.get('/api/health').json()['asr_activity'] == 'debug'
            assert service.active_session is None
            assert client.post('/api/asr/models/second/select').status_code == 409
            assert client.post('/api/asr/models/second/download').status_code == 400
            with client.websocket_connect('/ws/session') as lesson:
                lesson.send_json({'type': 'start', 'lesson': {'topic': '主题'}, 'tts': False})
                assert 'ASR 调试' in receive(lesson, 'error')['message']
            assert manager.busy
            ws.send_bytes(np.zeros(4800, dtype='<f4').tobytes())
            row = receive(ws, 'transcript')
            assert row['text'] == '链表 node 包含 3 个数据' and row['model_id'] == 'first'
            assert row['index'] == 1 and row['audio_seconds'] > 0
            assert row['asr_seconds'] >= 0 and row['real_time_factor'] >= 0
            ws.send_json({'type': 'stop'})
            tail = receive(ws, 'transcript')
            assert tail['index'] == 2
            result = receive(ws, 'finished')
            assert result['segments'] == 2 and result['audio_seconds'] == .1
            assert result['characters'] == 2 * len(row['text'])
        assert engines[0].closed and not manager.busy
        assert client.get('/api/health').json()['engine_state'] == 'idle'
        assert client.post('/api/asr/models/second/select').status_code == 200
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type':'start', 'sample_rate':44100})
            assert receive(ws, 'ready')['model_id'] == 'second'
        assert engines[1].closed and not manager.busy


def test_debug_page_and_scripts_do_not_reuse_cached_versions(debug_service):
    service, _, _ = debug_service
    with TestClient(service.app) as client:
        for path in ('/', '/models', '/static/app.js', '/static/asr-debug.js',
                     '/static/pcm-recorder.js', '/static/style.css', '/static/audio-worklet.js'):
            response = client.get(path)
            assert response.status_code == 200
            assert response.headers['cache-control'] == 'no-store'


@pytest.mark.parametrize('rate', [0, 7999, 96001, 48000.0, True, '48000'])
def test_invalid_sample_rate_does_not_load_model(debug_service, rate):
    service, manager, engines = debug_service
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type':'start', 'sample_rate':rate})
            assert '采样率' in receive(ws, 'error')['message']
        assert not engines and not manager.busy


@pytest.mark.parametrize('frame', [b'x', b'', b'x'*65540, np.array([float('nan')],dtype='<f4').tobytes()])
def test_invalid_pcm_releases_model(debug_service, frame):
    service, manager, engines = debug_service
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type':'start', 'sample_rate':16000})
            receive(ws, 'ready')
            ws.send_bytes(frame)
            assert receive(ws, 'error')['code'] == 'debug_failure'
        assert engines[0].closed and not manager.busy


def test_native_worker_failure_is_reported_without_extra_frames(debug_service, monkeypatch):
    service, manager, engines = debug_service
    def failing(self, samples):
        raise RuntimeError('模拟 native decode 失败')
    monkeypatch.setattr(TextEngine, 'transcribe', failing)
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type':'start', 'sample_rate':16000})
            receive(ws, 'ready')
            ws.send_bytes(np.zeros(1600, dtype='<f4').tobytes())
            assert '调试失败' in receive(ws, 'error')['message']
        assert engines[0].closed and not manager.busy


def test_second_debug_client_does_not_release_first_lease(debug_service):
    service, manager, engines = debug_service
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/asr-debug') as first:
            first.send_json({'type':'start', 'sample_rate':16000}); receive(first, 'ready')
            with client.websocket_connect('/ws/asr-debug') as second:
                second.send_json({'type':'start', 'sample_rate':16000})
                assert '已有' in receive(second, 'error')['message']
            assert manager.busy and not engines[0].closed
            first.send_json({'type':'stop'}); receive(first, 'finished')
        assert engines[0].closed and not manager.busy


def test_debug_rejects_external_origin(debug_service):
    from starlette.websockets import WebSocketDisconnect
    service, manager, _ = debug_service
    with TestClient(service.app) as client:
        with pytest.raises(WebSocketDisconnect):
            with client.websocket_connect('/ws/asr-debug', headers={'origin':'https://outside.test'}):
                pass
        assert not manager.busy


def test_classroom_lease_blocks_debug_without_closing_classroom(debug_service):
    service, manager, engines = debug_service
    asyncio.run(manager.acquire())
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type':'start', 'sample_rate':16000})
            assert '已有试讲' in receive(ws, 'error')['message']
        assert manager.busy and not engines[0].closed
        asyncio.run(manager.release())
