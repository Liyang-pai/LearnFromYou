"""真实浏览器联调使用的本机课堂服务；模型响应完全在内存生成。"""
import argparse
import json
from pathlib import Path
import shutil
import sys

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, prompts
from backend.llm import ModelClient
from question_recovery_server import OfflineModels


class OfflineModel(ModelClient):
    def __init__(self, emit):
        self.emit = emit
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        body = json.loads(request.content)
        if body['messages'][0]['content'].startswith(prompts.PREPARE):
            result = {'scope': ['规则A'], 'boundary_note': '本课内容尚未学习'}
        else:
            payload = next(json.loads(m['content']) for m in reversed(body['messages'])
                           if m['role'] == 'user' and m['content'].startswith('{'))
            if payload['interaction_context']['trigger'] == 'unmuted':
                result = {'candidate': {'kind': 'ack', 'text': '老师，第二个问题我还没想明白，我会继续听。',
                    'sources': ['t2'], 'response_request_ids': ['r2']}}
            else:
                sid = payload['new_teacher_segments'][-1]['id']
                updates = {'t1': {'response_request_updates': [
                    {'id': 'r1', 'text': '教师第一个提问', 'sources': ['t1']}]},
                    't2': {'response_request_updates': [
                    {'id': 'r2', 'text': '教师第二个提问', 'sources': ['t2']}]},
                    't3': {'response_request_updates': [
                    {'id': 'r1', 'text': '教师第一个提问', 'sources': ['t1'], 'status': 'dismissed',
                     'reason': '教师明确撤回第一个提问', 'resolution_sources': ['t3']}]}}[sid]
                result = {**updates, 'candidate': {'kind': 'silence'}}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result, ensure_ascii=False)}}]})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    shutil.copytree(config.ROOT / 'frontend', args.root / 'frontend')
    config.ROOT, config.PORT = args.root, args.port
    config.API_KEY = 'offline-test-key'
    config.BASE_URL = 'https://offline-model.invalid'
    from backend import app as service, session
    session.ModelClient = OfflineModel
    service.models = OfflineModels()
    uvicorn.run(service.app, host='127.0.0.1', port=args.port, log_level='warning')
