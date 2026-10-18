"""启动隔离正式前端验收服务，并持久记录最多十次提供方 HTTP 请求。"""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import sys
import threading

import httpx

ROOT = Path(__file__).resolve().parents[1]


class ProviderBudget:
    """Count before sending, retaining spent requests across restarts and failures."""
    _registry_lock = threading.Lock()
    _locks = {}

    def __init__(self, path, base_url, limit=10, models=()):
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError('提供方请求上限必须为 1 至 10')
        self.path = Path(path)
        self.origin = self._origin(httpx.URL(base_url))
        self.limit = limit
        self.models = set(models)
        with self._registry_lock:
            self.lock = self._locks.setdefault(str(self.path.resolve()), threading.RLock())
        with self.lock:
            self.count = self._read_count()

    @staticmethod
    def _origin(url):
        return url.scheme, url.host, url.port

    def _read_count(self):
        count = 0
        try:
            with self.path.open(encoding='utf-8') as stream:
                for line in stream:
                    record = json.loads(line)
                    if (record.get('event') == 'request' and type(record.get('request')) is int
                            and record['request'] == count + 1):
                        count += 1
                    elif record.get('event') == 'response' and type(record.get('request')) is int and 1 <= record['request'] <= count:
                        pass
                    else:
                        raise ValueError()
            return count
        except FileNotFoundError:
            return 0
        except (OSError, ValueError, AttributeError, TypeError):
            raise ValueError('提供方请求台账无法读取，已停止验收以保护请求上限') from None

    def _append(self, record):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {'time': datetime.now(timezone.utc).isoformat(), **record}
        with self.path.open('a', encoding='utf-8', newline='\n') as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + '\n')
            stream.flush()
            os.fsync(stream.fileno())

    def _reserve(self, request):
        with self.lock:
            try:
                self.count = self._read_count()
                if self.count >= self.limit:
                    raise httpx.RequestError('本次验收的提供方 HTTP 请求已达到上限，请停止真实调用', request=request)
                number = self.count + 1
                metadata = {'event': 'request', 'request': number,
                            'endpoint': request.url.path.rstrip('/').rsplit('/', 1)[-1]}
                try:
                    model = json.loads(request.content).get('model')
                    if model in self.models:
                        metadata['model'] = model
                except (ValueError, AttributeError, TypeError, httpx.RequestNotRead):
                    pass
                self._append(metadata)
                self.count = number
                return number
            except (OSError, ValueError):
                raise httpx.RequestError('提供方请求台账无法保存，已阻止本次请求', request=request) from None

    @staticmethod
    def _usage(response):
        try:
            usage = response.json().get('usage', {})
        except (ValueError, AttributeError):
            return {}
        if not isinstance(usage, dict):
            return {}
        fields = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'prompt_cache_hit_tokens',
                  'prompt_cache_miss_tokens', 'reasoning_tokens', 'cached_tokens')
        result = {key: usage[key] for key in fields if type(usage.get(key)) is int}
        for key in ('completion_tokens_details', 'prompt_tokens_details'):
            details = usage.get(key)
            if isinstance(details, dict):
                result[key] = {name: details[name] for name in ('reasoning_tokens', 'cached_tokens',
                    'accepted_prediction_tokens', 'rejected_prediction_tokens') if type(details.get(name)) is int}
        return result

    def wrap(self, original_send):
        async def send(client, request, *args, **kwargs):
            if self._origin(request.url) != self.origin:
                return await original_send(client, request, *args, **kwargs)
            number = self._reserve(request)
            # Prevent an automatic redirect from adding an uncounted HTTP exchange.
            kwargs['follow_redirects'] = False
            record = {'event': 'response', 'request': number, 'status': 'failed'}
            try:
                response = await original_send(client, request, *args, **kwargs)
                await response.aread()  # Cached by httpx; downstream response.json() still works.
                record.update(status=response.status_code, usage=self._usage(response))
                return response
            finally:
                try:
                    with self.lock:
                        self._append(record)
                except OSError:
                    raise httpx.RequestError('提供方响应台账保存失败，已停止本次验收请求', request=request) from None
        return send


def request_limit(value):
    try:
        limit = int(value)
        if not 1 <= limit <= 10:
            raise ValueError()
        return limit
    except ValueError:
        raise argparse.ArgumentTypeError('请求上限必须为 1 至 10') from None


def main(argv=None):
    parser = argparse.ArgumentParser(description='本机隔离正式前端验收，最多十次提供方 HTTP 请求')
    parser.add_argument('--port', type=int, default=18769)
    parser.add_argument('--runtime-dir', type=Path, default=ROOT / 'output' / 'formal-local')
    parser.add_argument('--env-file', type=Path, default=ROOT / '.env')
    parser.add_argument('--models-dir', type=Path, default=ROOT / 'asr' / 'models')
    parser.add_argument('--request-limit', type=request_limit, default=10)
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error('端口必须为 1 至 65535')
    if not args.env_file.is_file():
        parser.error('指定的 .env 文件不存在；请通过 --env-file 指定本机凭据文件')
    # Only check the port; do not stop or modify an existing service.
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if hasattr(socket, 'SO_EXCLUSIVEADDRUSE'):
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            probe.bind(('127.0.0.1', args.port))
    except OSError:
        parser.error('验收端口已被占用，请结束对应验收服务或选择其他端口')
    from dotenv import load_dotenv
    load_dotenv(args.env_file, override=True)
    runtime = args.runtime_dir.resolve()
    runtime.mkdir(parents=True, exist_ok=True)
    os.environ['PORT'] = str(args.port)
    os.environ['LEARNFROMYOU_DATA_ROOT'] = str(runtime)
    os.environ['LEARNFROMYOU_SETTINGS_PATH'] = str(runtime / 'settings.local.json')
    os.environ['ASR_MODELS_DIR'] = str(args.models_dir.resolve())
    sys.path.insert(0, str(ROOT))
    from backend import config
    try:
        budget = ProviderBudget(runtime / 'provider-requests.jsonl', config.BASE_URL, args.request_limit,
                                models=(config.MODEL, config.IMAGE_MODEL))
    except ValueError as exc:
        parser.error(str(exc))
    httpx.AsyncClient.send = budget.wrap(httpx.AsyncClient.send)
    import uvicorn
    print(f'正式前端验收：http://127.0.0.1:{args.port}；已使用提供方请求 {budget.count}/{budget.limit}')
    print('复用模型目录；启动前请确认原试讲或 ASR 调试已结束。不会自动停止其他服务。')
    uvicorn.run('backend.app:app', host='127.0.0.1', port=args.port, reload=False)


if __name__ == '__main__':
    main()
