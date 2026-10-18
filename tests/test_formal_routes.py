"""正式与调试入口隔离，以及本机设置接口的离线验收。"""
from fastapi.testclient import TestClient
from backend import app as service, config


def test_independent_entries_and_client_routes():
    with TestClient(service.app) as client:
        formal = client.get('/')
        debug = client.get('/debug')
        assert formal.status_code == debug.status_code == 200
        assert '/static/formal/app.js' in formal.text
        assert '/static/app.js' not in formal.text
        assert '/static/app.js' in debug.text
        for path in ['/create', '/classroom', '/classroom/details', '/history',
                     '/history/012345abcdef', '/report/012345abcdef', '/settings', '/settings/asr']:
            result = client.get(path)
            assert result.status_code == 200 and result.text == formal.text
            assert result.headers['cache-control'] == 'no-store'
        for path in ['/debug/models', '/debug/history', '/debug/history/012345abcdef']:
            assert client.get(path).text == debug.text
        assert client.get('/models', follow_redirects=False).headers['location'] == '/debug/models'


def test_settings_routes_keep_secrets_backend_only(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'SETTINGS_PATH', tmp_path / 'settings.local.json')
    monkeypatch.setattr(config, 'API_KEY', '')
    with TestClient(service.app) as client:
        saved = client.post('/api/settings', json={'api_key': 'a-private-key', 'tts': False, 'asr_review': True})
        assert saved.status_code == 200
        assert saved.json()['model_configured']
        assert 'a-private-key' not in client.get('/api/settings').text
        assert client.get('/api/health').json()['model_configured']
        assert client.post('/api/settings', headers={'Origin': 'https://elsewhere.test'},
                           json={'tts': True, 'asr_review': True}).status_code == 403
