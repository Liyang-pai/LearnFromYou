"""使用微型本地文件与模拟 HTTP 验证 ASR 校验、VAD 准备和维护互斥。"""
import asyncio
from dataclasses import replace
import hashlib
import os
import threading

import httpx
import pytest

from asr.catalog import Asset, Model
from asr.manager import ModelManager


@pytest.fixture
def manager(tmp_path):
    values = {'model.onnx': b'model', 'tokens.txt': b'tokens', 'vad.onnx': b'vad'}
    def asset(name):
        return Asset(name, len(values[name]), hashlib.sha256(values[name]).hexdigest(), 'https://stub.test/' + name)
    model = Model('test', 'test', 'test', 'sense_voice', (asset('model.onnx'), asset('tokens.txt')),
                  '中文', 'test', 'test', 'https://stub.test', 'https://stub.test', 'revision')
    calls = []
    def respond(request):
        calls.append(request.url.path)
        return httpx.Response(200, content=values[request.url.path[1:]])
    result = ModelManager(tmp_path / 'models', catalog=(model,), vad=asset('vad.onnx'),
                          transport=httpx.MockTransport(respond))
    result.test_calls = calls
    return result


def test_prepare_vad_downloads_only_shared_asset_without_selecting(manager):
    async def scenario():
        result = await manager.prepare_vad('test')
        assert result['action'] == 'prepare_vad'
        await manager.download_task
        assert manager.jobs['test']['state'] == 'prepared'
        assert manager.test_calls == ['/vad.onnx']
        assert manager.vad_path.read_bytes() == b'vad'
        assert not manager.directory(manager.model('test')).exists()
        assert manager.selected_id is None
    asyncio.run(scenario())


def test_verify_hashes_existing_files_without_network_even_if_stat_unchanged(manager):
    async def scenario():
        await manager.download('test')
        await manager.download_task
        manager.test_calls.clear()
        result = await manager.verify('test')
        assert result['verified'] and result['state'] == 'verified'
        assert len(result['files']) == 3
        model_path = manager.directory(manager.model('test')) / 'model.onnx'
        stat = model_path.stat()
        model_path.write_bytes(b'wrong')
        os.utime(model_path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        result = await manager.verify('test')
        assert not result['verified'] and result['state'] == 'failed'
        assert '校验失败' in result['error']
        assert not manager.snapshot()['models'][0]['installed']
        assert manager.test_calls == []
    asyncio.run(scenario())


def test_verify_missing_files_is_an_honest_failure_without_download(manager):
    result = asyncio.run(manager.verify('test'))
    assert not result['verified'] and result['state'] == 'failed'
    assert manager.test_calls == []
    assert not manager.root.exists()


def test_maintenance_blocks_active_asr_and_other_maintenance(manager, monkeypatch):
    async def scenario():
        manager.busy = True
        for operation in (manager.verify, manager.prepare_vad):
            with pytest.raises(ValueError, match='结束'):
                await operation('test')
        manager.busy = False
        entered, release = threading.Event(), threading.Event()
        def blocked(*args):
            entered.set()
            release.wait(5)
        monkeypatch.setattr(manager, '_prepare_vad', blocked)
        await manager.prepare_vad('test')
        assert await asyncio.to_thread(entered.wait, 2)
        try:
            for operation in (manager.verify, manager.prepare_vad, manager.download):
                with pytest.raises(ValueError, match='进行'):
                    await operation('test')
            with pytest.raises(ValueError, match='进行'):
                manager.select('test')
        finally:
            release.set()
            await manager.download_task
    asyncio.run(scenario())


def test_vad_preparation_cancel_releases_slot_and_keeps_model_intact(manager, monkeypatch):
    async def scenario():
        entered, release = threading.Event(), threading.Event()
        original = manager._fetch
        def blocked(*args):
            entered.set()
            release.wait(5)
            return original(*args)
        monkeypatch.setattr(manager, '_fetch', blocked)
        await manager.prepare_vad('test')
        assert await asyncio.to_thread(entered.wait, 2)
        assert manager.cancel('test')['state'] == 'cancelling'
        release.set()
        await manager.download_task
        assert manager.jobs['test']['state'] == 'cancelled'
        assert not manager.vad_path.exists()
        assert not manager.directory(manager.model('test')).exists()
    asyncio.run(scenario())


def test_legacy_verification_reports_presence_only_instead_of_hash_success(manager):
    async def scenario():
        model = manager.model('test')
        manager.catalog['test'] = replace(model, downloadable=False,
            files=tuple(replace(asset, size=0, sha256='', url='') for asset in model.files))
        directory = manager.directory(model)
        directory.mkdir(parents=True)
        for asset in model.files:
            (directory / asset.name).write_bytes(b'legacy local data')
        await manager.prepare_vad('test')
        await manager.download_task
        result = await manager.verify('test')
        assert result['state'] == 'checked' and result['verified'] is False
        assert '仅检查文件存在' in result['message']
        assert [detail['state'] for detail in result['files']] == ['present', 'present', 'verified']
    asyncio.run(scenario())
