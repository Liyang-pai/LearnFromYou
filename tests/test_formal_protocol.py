"""Offline formal/debug WebSocket contracts: fake ASR and model, isolated logs."""
import asyncio
import contextlib
import pytest
from fastapi.testclient import TestClient

from backend import config, settings
from backend.schemas import Preparation, ReviewGlossary, StudentResult


LESSON = {'topic': '惯性', 'student_profile': '普通初学者，尚未学习本课内容。'}


class FakeEngine:
    def close(self): pass


class FakeModels:
    def __init__(self):
        self.busy = False
        self.activity = None
        self.acquired = []
        self.releases = 0
        self.on_acquire = None
        self.maintained = []

    async def acquire(self, model_id=None, purpose='lesson'):
        if self.busy:
            raise ValueError('已有试讲或 ASR 调试正在创建或运行，请先结束当前任务')
        self.busy, self.activity = True, purpose
        self.acquired.append(purpose)
        if self.on_acquire:
            self.on_acquire()
        await asyncio.sleep(0)
        return FakeEngine(), None, 'offline-model'

    async def release(self):
        if self.busy:
            self.releases += 1
        self.busy, self.activity = False, None

    async def shutdown(self):
        await self.release()

    async def download(self, model_id):
        self.maintained.append('download')
        return {'accepted': True}

    async def verify(self, model_id):
        self.maintained.append('verify')
        return {'accepted': True}

    async def prepare_vad(self, model_id):
        self.maintained.append('prepare-vad')
        return {'accepted': True}

    def select(self, model_id):
        self.maintained.append('select')
        return {'accepted': True}


class FakeModel:
    instances = []

    def __init__(self, emit, snapshot=None):
        self.emit = emit
        self.snapshot = dict(snapshot or settings.model_settings())
        self.api_key = self.snapshot['api_key']
        self.base_url = self.snapshot['base_url']
        self.model = self.snapshot['model']
        self.image_model = self.snapshot['image_model']
        self.closed = False
        self.instances.append(self)

    async def generate(self, phase, *args, **kwargs):
        if phase == 'preparation':
            return Preparation(scope=['惯性'], boundary_note='教学范围')
        if phase == 'review_glossary':
            return ReviewGlossary(terms=[])
        return StudentResult()

    async def close(self):
        self.closed = True


class FakeDebug:
    @classmethod
    async def create(cls, send, engine, vad_path, model_id, rate, upload):
        result = cls()
        result.send, result.upload = send, upload
        result.worker = asyncio.create_task(asyncio.Event().wait())
        return result

    async def emit(self, kind, data):
        await self.send({'type': kind, 'data': data})

    async def finish(self):
        return {'segments': 0}

    async def close(self):
        self.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.worker


class StubHistory:
    def schedule(self, sid): pass
    async def close(self): pass


class StubReports:
    async def start(self, sid): return {'status': 'idle'}
    async def close(self): pass


@pytest.fixture
def protocol(tmp_path, monkeypatch):
    from backend import app as service
    monkeypatch.setattr(config, 'DATA_ROOT', tmp_path)
    monkeypatch.setattr(config, 'SETTINGS_PATH', tmp_path / 'settings.local.json')
    monkeypatch.setattr(config, 'API_KEY', 'before-key')
    monkeypatch.setattr(config, 'MODEL', 'before-model')
    monkeypatch.setattr('backend.session.ModelClient', FakeModel)
    monkeypatch.setattr(service, 'models', FakeModels())
    monkeypatch.setattr(service, 'active_session', None)
    monkeypatch.setattr(service, 'histories', StubHistory())
    monkeypatch.setattr(service, 'reports', StubReports())
    monkeypatch.setattr(service, 'ASRDebug', FakeDebug)
    FakeModel.instances = []
    settings.save_settings(settings.SettingsUpdate(api_key='', tts=False, asr_review=False))
    return service


def until(ws, kind):
    rows = []
    for _ in range(40):
        row = ws.receive_json()
        rows.append(row)
        if row['type'] in ((kind,) if isinstance(kind, str) else kind):
            return rows
    pytest.fail(f'Missing protocol event: {kind}')


def start(ws, **options):
    ws.send_json({'type': 'start', 'lesson': LESSON, **options})
    return until(ws, 'ready')[-1]['data']


def barrier(ws):
    ws.send_json({'type': 'mute', 'value': False})
    return until(ws, 'mute')


def test_start_uses_persisted_defaults_and_explicit_overrides(protocol):
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            ready = start(ws)
            assert ready['asr_review'] is False
            assert protocol.active_session.tts_enabled is False
        with client.websocket_connect('/ws/session') as ws:
            ready = start(ws, tts=True, asr_review=True)
            assert ready['asr_review'] is True
            assert protocol.active_session.tts_enabled is True


@pytest.mark.parametrize('text', ['', '  \n  ', None, {'unexpected': 'object'}])
def test_rejected_input_is_not_falsely_acknowledged(protocol, text):
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            start(ws, tts=False, asr_review=False)
            ws.send_json({'type': 'text', 'text': text, 'client_message_id': 'rejected'})
            rows = barrier(ws)
            assert not any(r['type'] == 'teacher_input_accepted' for r in rows)
            errors = [r['data'] for r in rows if r['type'] == 'error']
            assert errors and errors[-1]['client_message_id'] == 'rejected'
            assert not protocol.active_session.sources


def test_valid_input_ack_is_correlated_and_repeated_id_is_idempotent(protocol):
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            start(ws, tts=False, asr_review=False)
            value = {'type': 'text', 'text': '这是第一段讲授。', 'client_message_id': 'one'}
            for _ in range(2):
                ws.send_json(value)
                ack = until(ws, 'teacher_input_accepted')[-1]['data']
                assert ack['client_message_id'] == 'one'
            barrier(ws)
            teachers = [s for sid, s in protocol.active_session.sources.items() if sid.startswith('t')]
            assert len(teachers) == 1


@pytest.mark.parametrize('message', ['[1,2]', 'null', '{malformed'], ids=['array', 'null', 'json'])
def test_malformed_ws_message_reports_error_and_connection_survives(protocol, message):
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            start(ws, tts=False, asr_review=False)
            ws.send_text(message)
            rows = barrier(ws)
            assert any(r['type'] == 'error' for r in rows)
            assert protocol.models.busy and not protocol.active_session.closed


def test_start_freezes_credentials_before_asr_load_yields(protocol, monkeypatch):
    def changed_during_load():
        settings.save_settings(settings.SettingsUpdate(api_key='after-key', tts=True, asr_review=True))
        monkeypatch.setattr(config, 'MODEL', 'after-model')
    protocol.models.on_acquire = changed_during_load
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            start(ws, tts=False, asr_review=False)
            assert protocol.active_session.llm.api_key == 'before-key'
            assert protocol.active_session.llm.model == 'before-model'


def test_invalid_json_does_not_reuse_previous_client_message_id(protocol):
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            start(ws, tts=False, asr_review=False)
            ws.send_json({'type': 'text', 'text': '第一段讲授。', 'client_message_id': 'old-id'})
            until(ws, 'teacher_input_accepted')
            ws.send_text('{invalid')
            errors = [row['data'] for row in barrier(ws) if row['type'] == 'error']
            assert errors and 'client_message_id' not in errors[-1]


@pytest.mark.parametrize('operation', ['verify', 'download', 'select', 'prepare-vad'])
def test_maintenance_checks_active_session_even_when_manager_flag_is_clear(protocol, operation):
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            start(ws, tts=False, asr_review=False)
            protocol.models.busy = False
            response = client.post(f'/api/asr/models/offline-model/{operation}')
            assert response.status_code == 409
            assert protocol.models.maintained == []


@pytest.mark.parametrize('field', ['tts', 'muted', 'asr_review'])
def test_start_rejects_nonboolean_controls_before_acquiring_model(protocol, field):
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            ws.send_json({'type': 'start', 'lesson': LESSON, field: 'false'})
            result = until(ws, ('error', 'ready'))[-1]
            assert result['type'] == 'error' and result['data']['message']
            assert not protocol.models.acquired


def test_second_classroom_cannot_release_first_classroom_lease(protocol):
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as first:
            first_ready = start(first, tts=False, asr_review=False)
            with client.websocket_connect('/ws/session') as second:
                second.send_json({'type': 'start', 'lesson': LESSON})
                assert '已有' in until(second, 'error')[-1]['data']['message']
            assert protocol.active_session.id == first_ready['session_id']
            assert protocol.models.busy and protocol.models.releases == 0
            first.send_json({'type': 'end'})
            until(first, 'finished')
        assert not protocol.models.busy and protocol.models.releases == 1
        assert protocol.active_session is None


def test_session_constructor_failure_releases_model_without_disconnect(protocol, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError('模拟课堂初始化失败')
    monkeypatch.setattr(protocol, 'Session', broken)
    with TestClient(protocol.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            ws.send_json({'type': 'start', 'lesson': LESSON})
            assert '初始化失败' in until(ws, 'error')[-1]['data']['message']
            assert not protocol.models.busy
            assert protocol.active_session is None
            assert not protocol.classroom_starting


@pytest.mark.parametrize('owner', ['lesson', 'debug'])
def test_classroom_and_debug_exclude_each_other_and_release_on_disconnect(protocol, owner):
    with TestClient(protocol.app) as client:
        owner_path = '/ws/session' if owner == 'lesson' else '/ws/asr-debug'
        other_path = '/ws/asr-debug' if owner == 'lesson' else '/ws/session'
        with client.websocket_connect(owner_path) as first:
            if owner == 'lesson':
                start(first, tts=False, asr_review=False)
            else:
                first.send_json({'type': 'start', 'sample_rate': 16000})
                until(first, 'ready')
            with client.websocket_connect(other_path) as second:
                second.send_json({'type': 'start', 'sample_rate': 16000, 'lesson': LESSON})
                assert '已有' in until(second, 'error')[-1]['data']['message']
            assert protocol.models.busy and protocol.models.activity == owner
            assert protocol.models.releases == 0
        assert not protocol.models.busy and protocol.models.releases == 1
