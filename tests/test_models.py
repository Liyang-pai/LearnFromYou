import asyncio
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time

import httpx
import pytest

from asr.catalog import Asset, CATALOG, LEGACY_DIRECTORY, LEGACY_ID, Model
from asr.manager import ModelManager
from asr.recognizer import Recognizer


def asset(name, data):
    return Asset(name, len(data), hashlib.sha256(data).hexdigest(), 'https://models.test/' + name)


@pytest.fixture
def manager(tmp_path):
    data = {'model.int8.onnx': b'fake-weight', 'tokens.txt': b'tokens', 'silero_vad.onnx': b'vad'}
    files = tuple(asset(name, data[name]) for name in ('model.int8.onnx', 'tokens.txt'))
    model = Model('first', '测试模型', '测试模型 带空格', 'sense_voice', files, '中文 · 英文',
                  'test', 'light', 'https://models.test', 'https://models.test/license', 'fixed')
    second = replace(model, id='second', directory='第二个模型')
    vad = asset('silero_vad.onnx', data['silero_vad.onnx'])
    transport = httpx.MockTransport(lambda req: httpx.Response(200, content=data[req.url.path.lstrip('/')]))
    return ModelManager(tmp_path / '中文项目 with spaces' / 'models', catalog=(model, second), vad=vad, transport=transport)


async def install(manager, model_id='first'):
    await manager.download(model_id)
    await manager.download_task
    assert manager.jobs[model_id]['state'] == 'installed'


class FakeEngine:
    def __init__(self, model_id):
        self.model_id = model_id
        self.closed = False

    def close(self):
        self.closed = True


def test_catalog_is_small_pinned_and_within_budget():
    assert len(CATALOG) <= 5
    assert sum(m.downloadable for m in CATALOG) == 4
    for model in CATALOG:
        if model.downloadable:
            assert model.size < 1_500_000_000
            assert len(model.revision) == 40
            for file in model.files:
                assert len(file.sha256) == 64
                assert model.revision in file.url
                assert '/' not in file.name
    assert not next(m for m in CATALOG if m.id == LEGACY_ID).downloadable


def test_empty_installation_opens_without_loading(manager):
    state = manager.snapshot()
    assert not state['asr_ready'] and not state['vad_ready']
    assert state['selected_id'] is None and state['engine_state'] == 'idle'
    assert len(state['models']) == 2
    assert not manager.root.exists()


@pytest.mark.parametrize('choice', [{}, [], 3])
def test_malformed_selection_does_not_break_first_run(manager, choice):
    manager.root.mkdir(parents=True)
    (manager.root / '.settings.json').write_text(json.dumps({'selected_id': choice}), encoding='utf-8')
    restored = ModelManager(manager.root, catalog=tuple(manager.catalog.values()), vad=manager.vad)
    assert restored.snapshot()['selected_id'] is None
    assert '无法读取' in restored.snapshot()['notice']


def test_install_switch_persist_and_manual_delete(manager, monkeypatch):
    async def scenario():
        await install(manager)
        await install(manager, 'second')
        assert all(m['installed'] for m in manager.snapshot()['models'])
        manager.select('first')
        restored = ModelManager(manager.root, catalog=tuple(manager.catalog.values()), vad=manager.vad)
        assert restored.snapshot()['selected_id'] == 'first'
        engines = []
        def create(model):
            engine = FakeEngine(model.id)
            engines.append(engine)
            return engine
        monkeypatch.setattr(manager, '_create_engine', create)
        engine, path, model_id = await manager.acquire()
        assert model_id == 'first' and path.parent.name == '_shared'
        assert manager.snapshot()['engine_state'] == 'loaded'
        with pytest.raises(ValueError, match='不能切换'):
            manager.select('second')
        with pytest.raises(ValueError, match='结束试讲'):
            await manager.download('second')
        await manager.release()
        assert engine.closed and not manager.busy
        manager.select('second')
        engine, _, model_id = await manager.acquire()
        assert model_id == 'second' and engines[0].closed
        await manager.release()
        shutil.rmtree(manager.root / manager.catalog['first'].directory)
        assert manager.snapshot()['selected_id'] == 'second'
        shutil.rmtree(manager.root / manager.catalog['second'].directory)
        state = manager.snapshot()
        assert state['selected_id'] is None and not state['asr_ready']
        assert '删除' in state['notice']
        assert ModelManager(manager.root, catalog=tuple(manager.catalog.values()), vad=manager.vad).selected_id is None
    asyncio.run(scenario())


def test_incomplete_missing_changed_and_corrupt_files(manager):
    async def scenario():
        await install(manager)
        manager.select('first')
        directory = manager.root / manager.catalog['first'].directory
        token = directory / 'tokens.txt'
        token.unlink()
        assert manager.snapshot()['selected_id'] is None
        await install(manager)
        manager.select('first')
        weight = directory / 'model.int8.onnx'
        # Same size tampering is caught by the signature scan.
        weight.write_bytes(b'x' * weight.stat().st_size)
        assert not manager.snapshot()['asr_ready']
        await install(manager)
        assert weight.read_bytes() == b'fake-weight'
        (directory / '.installed.json').write_text('not json', encoding='utf-8')
        assert not manager.snapshot()['models'][0]['installed']
    asyncio.run(scenario())


@pytest.mark.parametrize('response', [httpx.Response(503), httpx.Response(200, content=b'bad-weight!'),
                                      httpx.Response(200, content=b'too-large' * 100)])
def test_network_hash_and_oversize_failures_can_retry(manager, response):
    async def scenario():
        good = manager.transport
        manager.transport = httpx.MockTransport(lambda _: response)
        await manager.download('first')
        await manager.download_task
        assert manager.jobs['first']['state'] == 'failed'
        assert manager.jobs['first']['error']
        assert not manager.snapshot()['models'][0]['installed']
        assert not list(manager.root.rglob('*.part'))
        manager.transport = good
        await install(manager)
    asyncio.run(scenario())


def test_cancel_waits_for_worker_and_keeps_installation_incomplete(manager):
    entered, proceed = threading.Event(), threading.Event()
    class SlowBody(httpx.SyncByteStream):
        def __iter__(self):
            entered.set()
            assert proceed.wait(3)
            yield b'fake-weight'
    manager.transport = httpx.MockTransport(lambda _: httpx.Response(200, stream=SlowBody()))
    async def scenario():
        await manager.download('first')
        assert await asyncio.to_thread(entered.wait, 3)
        with pytest.raises(ValueError, match='已有下载'):
            await manager.download('second')
        with pytest.raises(ValueError, match='正在下载'):
            await manager.acquire('second')
        assert manager.cancel('first')['state'] == 'cancelling'
        proceed.set()
        await manager.download_task
        assert manager.jobs['first']['state'] == 'cancelled'
        assert not manager.snapshot()['models'][0]['installed']
        assert not list(manager.root.rglob('*.part'))
    try:
        asyncio.run(scenario())
    finally:
        proceed.set()


def test_disk_space_unknown_model_and_legacy_download(manager, monkeypatch):
    monkeypatch.setattr('asr.manager.shutil.disk_usage', lambda _: shutil._ntuple_diskusage(10, 10, 0))
    with pytest.raises(ValueError, match='空间不足'):
        asyncio.run(manager.download('first'))
    with pytest.raises(ValueError, match='未知模型'):
        manager.select('../outside')
    legacy = next(m for m in CATALOG if m.id == LEGACY_ID)
    local = ModelManager(manager.root, catalog=(legacy,))
    with pytest.raises(ValueError, match='不提供下载'):
        asyncio.run(local.download(LEGACY_ID))


def test_legacy_adoption_is_one_time_and_does_not_move_files(tmp_path):
    root = tmp_path / 'models'
    directory = root / LEGACY_DIRECTORY
    directory.mkdir(parents=True)
    for name in ('model.int8.onnx', 'tokens.txt'):
        (directory / name).write_bytes(b'old-model')
    (root / 'silero_vad.onnx').write_bytes(b'old-vad')
    manager = ModelManager(root)
    assert manager.selected_id == LEGACY_ID
    assert len(manager.snapshot()['models']) == 5
    assert manager.vad_path == root / 'silero_vad.onnx'
    shutil.rmtree(directory)
    assert manager.snapshot()['selected_id'] is None
    assert len(manager.snapshot()['models']) == 4
    directory.mkdir()
    for name in ('model.int8.onnx', 'tokens.txt'):
        (directory / name).write_bytes(b'old-model')
    assert ModelManager(root).selected_id is None
    assert (root / 'silero_vad.onnx').read_bytes() == b'old-vad'


def test_loading_locks_selection_and_failure_releases(manager, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    def load(model):
        entered.set()
        assert proceed.wait(3)
        raise ValueError('模拟校验失败')
    async def scenario():
        await install(manager)
        await install(manager, 'second')
        manager.select('first')
        monkeypatch.setattr(manager, '_create_engine', load)
        task = asyncio.create_task(manager.acquire())
        assert await asyncio.to_thread(entered.wait, 3)
        assert manager.snapshot()['engine_state'] == 'loading'
        with pytest.raises(ValueError, match='不能切换'):
            manager.select('second')
        proceed.set()
        with pytest.raises(ValueError, match='校验失败'):
            await task
        assert not manager.busy and manager.snapshot()['engine_state'] == 'failed'
        manager.select('second')
        assert manager.error is None
    try:
        asyncio.run(scenario())
    finally:
        proceed.set()


def test_cancelled_load_waits_for_native_worker(manager, monkeypatch):
    entered, proceed = threading.Event(), threading.Event()
    engine = FakeEngine('first')
    def load(_):
        entered.set()
        assert proceed.wait(3)
        return engine
    async def scenario():
        await install(manager)
        manager.select('first')
        monkeypatch.setattr(manager, '_create_engine', load)
        task = asyncio.create_task(manager.acquire())
        assert await asyncio.to_thread(entered.wait, 3)
        task.cancel()
        await asyncio.sleep(0)
        assert manager.busy
        proceed.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert engine.closed and not manager.busy
    try:
        asyncio.run(scenario())
    finally:
        proceed.set()


@pytest.mark.parametrize('model', [m for m in CATALOG if m.downloadable])
def test_recognizer_selects_correct_cpu_backend(model, tmp_path, monkeypatch):
    import sherpa_onnx
    called = []
    def create(**kwargs):
        called.append(kwargs)
        return object()
    method = {'sense_voice': 'from_sense_voice', 'paraformer': 'from_paraformer', 'whisper': 'from_whisper'}[model.family]
    monkeypatch.setattr(sherpa_onnx.OfflineRecognizer, method, create)
    recognizer = Recognizer(tmp_path / '中文 with spaces', 3, model)
    assert called[0]['provider'] == 'cpu' and called[0]['num_threads'] == 3
    if model.family == 'whisper':
        assert called[0]['language'] == 'zh' and called[0]['task'] == 'transcribe'
        assert called[0]['encoder'].endswith('-encoder.int8.onnx')
    recognizer.close()
    with pytest.raises(ValueError, match='释放'):
        recognizer.transcribe([])


def test_recognizer_close_waits_for_decode_thread():
    recognizer = Recognizer.__new__(Recognizer)
    recognizer.lock = threading.Lock()
    recognizer.engine = object()
    entered, proceed = threading.Event(), threading.Event()
    def decoding():
        with recognizer.lock:
            entered.set()
            assert proceed.wait(3)
    worker = threading.Thread(target=decoding)
    worker.start()
    assert entered.wait(3)
    close = threading.Thread(target=recognizer.close)
    close.start()
    assert recognizer.engine is not None
    proceed.set()
    worker.join(3); close.join(3)
    assert not close.is_alive() and recognizer.engine is None


def test_open_folder_uses_native_platform_and_fixed_path(manager, monkeypatch):
    calls = []
    monkeypatch.setattr('asr.manager.sys.platform', 'darwin')
    monkeypatch.setattr('asr.manager.subprocess.Popen', lambda argv: calls.append(argv))
    manager.open_folder()
    assert calls == [['open', str(manager.root.resolve())]]
    monkeypatch.setattr('asr.manager.sys.platform', 'win32')
    monkeypatch.setattr('asr.manager.os.startfile', lambda path: calls.append(path), raising=False)
    manager.open_folder()
    assert calls[-1] == str(manager.root.resolve())


def test_no_symlink_download_escape(manager, tmp_path):
    if sys.platform == 'win32':
        pytest.skip('Windows 创建符号链接需要额外权限')
    outside = tmp_path / 'outside'; outside.mkdir()
    manager.root.mkdir(parents=True)
    (manager.root / manager.catalog['first'].directory).symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match='链接'):
        asyncio.run(manager.download('first'))
    assert not list(outside.iterdir())


def test_git_guard_catches_even_tiny_forced_add(tmp_path):
    project = Path(__file__).resolve().parents[1]
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    (tmp_path / '.gitignore').write_text('/asr/models/\n', encoding='utf-8')
    directory = tmp_path / 'asr/models/轻量模型'; directory.mkdir(parents=True)
    (directory / 'tiny.onnx').write_bytes(b'small')
    (directory / 'tokens.txt').write_bytes(b'tokens')
    subprocess.run(['git', 'add', '.'], cwd=tmp_path, check=True)
    guard = [sys.executable, str(project / 'scripts/check_model_files.py')]
    assert subprocess.run(guard, cwd=tmp_path, capture_output=True).returncode == 0
    subprocess.run(['git', 'add', '-f', 'asr/models'], cwd=tmp_path, check=True)
    result = subprocess.run(guard, cwd=tmp_path, capture_output=True)
    assert result.returncode == 1 and b'tiny.onnx' in result.stderr and b'tokens.txt' in result.stderr


def test_benchmark_reports_errors_terms_and_english_words():
    from asr.benchmark import score
    exact = score('这里使用 API，三点。', '这里使用 API，三点。', ['API', '三点'])
    assert exact == {'cer': 0, 'english_wer': 0, 'term_hits': 2, 'term_total': 2}
    wrong = score('使用 API endpoint', '使用 API point', ['endpoint'])
    assert wrong['cer'] > 0 and wrong['english_wer'] == .5 and wrong['term_hits'] == 0
    assert score('API 3', 'capillary 13', ['API', '3'])['term_hits'] == 0


def test_model_http_api_and_empty_first_run(manager, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app as service
    monkeypatch.setattr(service, 'models', manager)
    monkeypatch.setattr(service, 'active_session', None)
    with TestClient(service.app, base_url='http://localhost:8765') as client:
        assert client.get('/').status_code == 200
        assert 'modelCards' in client.get('/models').text
        assert not client.get('/api/health').json()['asr_ready']
        assert client.get('/api/asr/models').json()['selected_id'] is None
        assert client.post('/api/asr/models/first/select').status_code == 400
        assert client.post('/api/asr/models/unknown/download').status_code == 400
        assert client.post('/api/asr/models/first/download', headers={'origin': 'https://outside.test'}).status_code == 403
        assert client.post('/api/asr/open-folder', headers={'sec-fetch-site': 'cross-site'}).status_code == 403
        response = client.post('/api/asr/models/first/download', headers={'origin': 'http://localhost:8765'})
        assert response.status_code == 200
        deadline = time.monotonic() + 3
        while client.get('/api/asr/models').json()['models'][0]['state'] != 'installed':
            assert time.monotonic() < deadline
            time.sleep(.01)
        assert client.post('/api/asr/models/first/select').status_code == 200
        assert client.get('/api/health').json()['asr_ready']
        monkeypatch.setattr(manager, 'open_folder', lambda: None)
        assert client.post('/api/asr/open-folder').json()['opened']


def test_websocket_locks_model_and_releases_on_end_disconnect_and_failure(manager, monkeypatch):
    from fastapi.testclient import TestClient
    from backend import app as service
    from backend.llm import ModelError
    from backend.schemas import Preparation
    monkeypatch.setattr('backend.config.ROOT', manager.root.parent)
    asyncio.run(install(manager))
    asyncio.run(install(manager, 'second'))
    manager.select('first')
    engines = []
    def create(model):
        engine = FakeEngine(model.id); engines.append(engine); return engine
    monkeypatch.setattr(manager, '_create_engine', create)
    monkeypatch.setattr(service, 'models', manager)
    monkeypatch.setattr(service, 'active_session', None)

    class FakeLLM:
        fail = False
        def __init__(self, emit): pass
        async def generate(self, *args):
            if self.fail:
                raise ModelError('模拟课前失败')
            return Preparation(scope=['主题'], boundary_note='test')
        async def close(self): pass
    monkeypatch.setattr('backend.session.ModelClient', FakeLLM)

    def receive(ws, kind):
        for _ in range(20):
            event = ws.receive_json()
            if event['type'] == kind:
                return event['data']
        pytest.fail(f'没有收到 {kind}')
    def start(ws):
        ws.send_json({'type': 'start', 'lesson': {'topic': '主题'}, 'tts': False})
    with TestClient(service.app, base_url='http://localhost:8765') as client:
        with client.websocket_connect('/ws/session') as ws:
            start(ws)
            assert receive(ws, 'ready')['asr_model'] == 'first'
            assert client.get('/api/health').json()['engine_state'] == 'loaded'
            assert client.post('/api/asr/models/second/select').status_code == 409
            assert client.post('/api/asr/models/second/download').status_code == 400
            ws.send_json({'type': 'end'})
            receive(ws, 'finished')
        assert engines[0].closed and not manager.busy and manager.engine is None
        assert client.post('/api/asr/models/second/select').status_code == 200
        with client.websocket_connect('/ws/session') as ws:
            start(ws)
            assert receive(ws, 'ready')['asr_model'] == 'second'
        assert engines[1].closed and not manager.busy
        FakeLLM.fail = True
        with client.websocket_connect('/ws/session') as ws:
            start(ws)
            assert '课前失败' in receive(ws, 'error')['message']
        assert engines[2].closed and not manager.busy
        assert client.post('/api/asr/models/first/select').status_code == 200
