"""验证本机设置、凭据快照与脱敏；仅使用隔离文件和模拟网络。"""
import asyncio
import json

import httpx
import pytest
from pydantic import BaseModel, ValidationError

from backend import config


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr(config, 'DATA_ROOT', None, raising=False)
    monkeypatch.setattr(config, 'SETTINGS_PATH', None, raising=False)
    monkeypatch.setattr(config, 'API_KEY', 'fallback-key')
    monkeypatch.setattr(config, 'BASE_URL', 'https://provider.test/v1')
    monkeypatch.setattr(config, 'MODEL', 'text-old')
    monkeypatch.setattr(config, 'IMAGE_MODEL', 'image-old')
    return tmp_path


def test_data_root_falls_back_and_can_be_isolated(isolated, monkeypatch):
    assert config.data_root() == isolated
    monkeypatch.setattr(config, 'DATA_ROOT', isolated / 'separate')
    assert config.data_root() == isolated / 'separate'


def test_settings_persist_blank_preserves_key_and_env_untouched(isolated):
    from backend import settings
    original = isolated / '.env'
    original.write_text('DEEPSEEK_API_KEY=original\n', encoding='utf-8')
    public = settings.save_settings(settings.SettingsUpdate(api_key='  new-key  ', tts=False, asr_review=True))
    assert public['model_configured'] and public['tts'] is False
    assert public['student_model'] == 'text-old' and public['image_model'] == 'image-old'
    assert 'new-key' not in json.dumps(public) and 'api_key' not in public
    settings.save_settings(settings.SettingsUpdate(api_key='  ', tts=True, asr_review=False))
    assert settings.model_settings()['api_key'] == 'new-key'
    assert settings.public_settings()['asr_review'] is False
    assert original.read_text(encoding='utf-8') == 'DEEPSEEK_API_KEY=original\n'
    saved = json.loads((isolated / 'settings.local.json').read_text(encoding='utf-8'))
    assert saved == {'api_key': 'new-key', 'tts': True, 'asr_review': False}


@pytest.mark.parametrize('key', ['abc\nsecret', 'abc\rsecret', 'x' * 4097])
def test_invalid_key_is_rejected_without_exposing_input(isolated, key):
    from backend.settings import SettingsUpdate
    with pytest.raises(ValidationError):
        SettingsUpdate(api_key=key, tts=True, asr_review=True)
    assert not (isolated / 'settings.local.json').exists()


def test_failed_atomic_write_preserves_saved_settings(isolated, monkeypatch):
    from backend import settings
    settings.save_settings(settings.SettingsUpdate(api_key='safe-key', tts=True, asr_review=True))
    previous = (isolated / 'settings.local.json').read_bytes()
    def fail(*args):
        raise OSError('path containing safe-key')
    monkeypatch.setattr(settings.os, 'replace', fail)
    with pytest.raises(ValueError) as error:
        settings.save_settings(settings.SettingsUpdate(api_key='replacement', tts=False, asr_review=False))
    assert 'safe-key' not in str(error.value)
    assert (isolated / 'settings.local.json').read_bytes() == previous
    assert not list(isolated.glob('*.tmp'))


def test_connection_only_gets_models_and_sanitizes_failure(isolated, monkeypatch):
    from backend import settings
    requests = []
    original = httpx.AsyncClient
    def respond(request):
        requests.append(request)
        return httpx.Response(401, text='fallback-key')
    monkeypatch.setattr(settings.httpx, 'AsyncClient', lambda **kw: original(
        transport=httpx.MockTransport(respond), **kw))
    result = asyncio.run(settings.test_connection())
    assert not result['connected'] and 'fallback-key' not in json.dumps(result)
    assert len(requests) == 1 and requests[0].method == 'GET'
    assert str(requests[0].url) == 'https://provider.test/v1/models'
    assert requests[0].headers['authorization'] == 'Bearer fallback-key'


def test_model_client_captures_settings_and_redacts_old_key(isolated):
    from backend import settings
    from backend.llm import ModelClient
    class Result(BaseModel):
        message: str
    async def scenario():
        events, calls = [], []
        async def emit(kind, data):
            events.append((kind, data))
        client = ModelClient(emit)
        await client.http.aclose()
        def respond(request):
            calls.append(request)
            return httpx.Response(200, json={'choices': [{'message': {'content': '{"message":"fallback-key"}'}}]})
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        settings.save_settings(settings.SettingsUpdate(api_key='new-key', tts=False, asr_review=False))
        config.BASE_URL, config.MODEL, config.IMAGE_MODEL = 'https://new.test', 'text-new', 'image-new'
        try:
            await client.generate('student_profile', 'rules', {'secret': 'fallback-key'}, Result)
        finally:
            await client.close()
        assert client.api_key == 'fallback-key'
        assert str(calls[0].url) == 'https://provider.test/v1/chat/completions'
        assert calls[0].headers['authorization'] == 'Bearer fallback-key'
        assert json.loads(calls[0].content)['model'] == 'text-old'
        assert 'fallback-key' not in json.dumps(events)
        assert '[REDACTED]' in json.dumps(events)
    asyncio.run(scenario())


def test_client_redacts_keys_containing_json_escape_characters(isolated, monkeypatch):
    from backend.llm import ModelClient
    monkeypatch.setattr(config, 'API_KEY', 'key-"quoted"-\\value')
    async def scenario():
        events = []
        async def emit(kind, data):
            events.append(data)
        client = ModelClient(emit)
        try:
            await client.emit('error', {'message': 'reflected ' + config.API_KEY})
        finally:
            await client.close()
        assert events == [{'message': 'reflected [REDACTED]'}]
    asyncio.run(scenario())


@pytest.mark.parametrize('invalid_key', ['super-secret-do-not-echo\ninvalid',
                                       {'nested': 'super-secret-do-not-echo'}],
                         ids=['newline', 'wrong-type'])
def test_settings_validation_route_does_not_echo_invalid_credentials(tmp_path, monkeypatch, invalid_key):
    monkeypatch.setattr(config, 'DATA_ROOT', tmp_path)
    monkeypatch.setattr(config, 'SETTINGS_PATH', tmp_path / 'settings.local.json')
    monkeypatch.setattr(config, 'MODELS_DIR', tmp_path / 'models')
    from fastapi.testclient import TestClient
    from backend.app import app
    with TestClient(app) as client:
        response = client.post('/api/settings', json={'api_key': invalid_key, 'tts': True, 'asr_review': True})
    assert response.status_code in (400, 422)
    assert 'super-secret-do-not-echo' not in response.text


def test_runner_budget_counts_before_request_and_survives_restart(tmp_path):
    from scripts.run_formal import ProviderBudget
    async def scenario():
        path = tmp_path / 'provider-requests.jsonl'
        calls = []
        def respond(request):
            calls.append(request)
            records = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            assert any(record['event'] == 'request' for record in records)
            return httpx.Response(200, json={'usage': {'prompt_tokens': 2, 'completion_tokens': 3,
                                                     'total_tokens': 5}, 'secret': 'do-not-log'})
        budget = ProviderBudget(path, 'https://provider.test/v1', 1, models=('text-model',))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            send = budget.wrap(httpx.AsyncClient.send)
            request = client.build_request('POST', 'https://provider.test/v1/chat/completions',
                                          json={'model': 'text-model', 'messages': 'do-not-log'},
                                          headers={'Authorization': 'Bearer do-not-log'})
            response = await send(client, request)
            assert response.json()['usage']['total_tokens'] == 5
            restarted = ProviderBudget(path, 'https://provider.test/v1', 1, models=('text-model',))
            with pytest.raises(httpx.RequestError, match='上限'):
                await restarted.wrap(httpx.AsyncClient.send)(client, request)
        assert len(calls) == 1
        ledger = path.read_text(encoding='utf-8')
        assert 'do-not-log' not in ledger and 'messages' not in ledger
        assert 'prompt_tokens' in ledger
    asyncio.run(scenario())


def test_runner_budget_counts_models_failures_and_ignores_other_origins(tmp_path):
    from scripts.run_formal import ProviderBudget
    async def scenario():
        path = tmp_path / 'provider-requests.jsonl'
        budget = ProviderBudget(path, 'https://provider.test/v1', 1)
        def respond(request):
            if request.url.host == 'provider.test':
                raise httpx.ConnectError('secret provider failure', request=request)
            return httpx.Response(200)
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            send = budget.wrap(httpx.AsyncClient.send)
            await send(client, client.build_request('GET', 'https://models.test/vad'))
            assert not path.exists()
            with pytest.raises(httpx.ConnectError):
                await send(client, client.build_request('GET', 'https://provider.test/v1/models'))
            with pytest.raises(httpx.RequestError, match='上限'):
                await send(client, client.build_request('GET', 'https://provider.test/v1/models'))
        assert 'secret provider failure' not in path.read_text(encoding='utf-8')
    asyncio.run(scenario())


def test_runner_budget_fails_closed_on_corrupt_ledger(tmp_path):
    from scripts.run_formal import ProviderBudget
    path = tmp_path / 'provider-requests.jsonl'
    path.write_text('not-json\n', encoding='utf-8')
    with pytest.raises(ValueError):
        ProviderBudget(path, 'https://provider.test/v1', 10)
