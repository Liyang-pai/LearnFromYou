"""管理独立于原安装 .env 的本机设置、公开状态与安全连接测试。"""
import json
import os
from pathlib import Path
import tempfile
import threading
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, StrictBool, field_validator

from . import config

_lock = threading.RLock()


class SettingsUpdate(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    api_key: str | None = None
    tts: StrictBool
    asr_review: StrictBool

    @field_validator('api_key')
    @classmethod
    def valid_key(cls, value):
        if value is None:
            return value
        if len(value) > 4096 or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError('API 密钥长度或格式无效')
        return value.strip()


def _path():
    return Path(config.SETTINGS_PATH) if config.SETTINGS_PATH is not None else config.data_root() / 'settings.local.json'


def _read():
    try:
        value = json.loads(_path().read_text(encoding='utf-8'))
        if not isinstance(value, dict):
            raise ValueError()
        SettingsUpdate(api_key=value.get('api_key'), tts=value.get('tts', True),
                       asr_review=value.get('asr_review', True))
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, TypeError):
        raise ValueError('本机设置无法读取，请检查设置文件或目录权限') from None


def model_settings():
    """Return a fresh private snapshot; callers must never expose this mapping."""
    with _lock:
        saved = _read()
        return {'api_key': saved.get('api_key') or config.API_KEY,
                'base_url': config.BASE_URL, 'model': config.MODEL, 'image_model': config.IMAGE_MODEL}


def public_settings():
    with _lock:
        saved = _read()
        return {'tts': saved.get('tts', True), 'asr_review': saved.get('asr_review', True),
                'model_configured': bool(saved.get('api_key') or config.API_KEY),
                'service': urlsplit(config.BASE_URL).hostname or '兼容模型服务',
                'student_model': config.MODEL, 'image_model': config.IMAGE_MODEL}


def save_settings(update: SettingsUpdate):
    # Serialize the complete read-modify-replace cycle, including blank-key saves.
    with _lock:
        saved = _read()
        saved.update(tts=update.tts, asr_review=update.asr_review)
        if update.api_key:
            saved['api_key'] = update.api_key
        temporary = None
        try:
            target = _path()
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                             prefix='.settings-', suffix='.tmp', delete=False) as file:
                temporary = Path(file.name)
                json.dump(saved, file, ensure_ascii=False)
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, target)
        except OSError:
            raise ValueError('本机设置无法保存，原有设置已保留；请检查目录权限或磁盘空间') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return public_settings()


async def test_connection():
    snapshot = model_settings()
    if not snapshot['api_key']:
        return {'connected': False, 'message': '请先保存 API 密钥'}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=10), follow_redirects=False) as client:
            response = await client.get(snapshot['base_url'].rstrip('/') + '/models',
                                        headers={'Authorization': 'Bearer ' + snapshot['api_key']})
        response.raise_for_status()
        return {'connected': True, 'message': '模型服务连接正常'}
    except httpx.HTTPStatusError as exc:
        return {'connected': False, 'message': f'模型服务返回 HTTP {exc.response.status_code}，请检查密钥或服务配置'}
    except (httpx.HTTPError, ValueError):
        return {'connected': False, 'message': '模型服务连接失败，请检查网络或服务配置'}
