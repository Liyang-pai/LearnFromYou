"""为浏览器恢复测试提供真实本机服务，使用模拟 ASR 和内存模型响应，禁止外部模型调用。"""
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


class OfflineModel(ModelClient):
    def __init__(self, emit):
        self.emit = emit
        # 直接使用内存传输，不建立任何外部模型连接。
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        body = json.loads(request.content)
        if body['messages'][0]['content'].startswith(prompts.PREPARE):
            result = {'scope': ['一元一次方程'], 'boundary_note': '尚未讲授'}
        else:
            # 修复请求的末尾是自然语言反馈，从前面的结构化请求读取批次。
            payload = next(json.loads(m['content']) for m in reversed(body['messages'])
                           if m['role'] == 'user' and m['content'].startswith('{'))
            sid = payload['new_teacher_segments'][-1]['id']
            result = {'knowledge_updates': [{'id': 'k_' + sid, 'text': '教师讲授的方程变形。',
                      'status': 'tentative', 'reason': '教师省略中间步骤，已有基础支持暂定推断', 'sources': [sid]}]}
            if sid == 't1':
                result['candidate'] = {'kind': 'question', 'text': '为什么移项变号？',
                                       'question_id': 'q2', 'sources': [sid]}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result, ensure_ascii=False)}}]})


class OfflineModels:
    busy = False

    def snapshot(self):
        return {'asr_ready': True, 'selected_id': 'offline', 'vad_ready': True,
                'engine_state': 'loaded' if self.busy else 'idle', 'busy': self.busy,
                'activity': 'session' if self.busy else None, 'error': '', 'notice': '',
                'models_dir': '离线测试', 'models': []}

    async def acquire(self):
        self.busy = True
        return None, None, 'offline'

    async def release(self): self.busy = False
    async def shutdown(self): pass


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    # 前端与课堂日志均放在临时目录，测试不污染用户的生产日志。
    shutil.copytree(config.ROOT / 'frontend', args.root / 'frontend')
    config.ROOT, config.PORT = args.root, args.port
    config.API_KEY = 'offline-test-key'
    config.BASE_URL = 'https://offline-model.invalid'
    from backend import app as service, session
    session.ModelClient = OfflineModel
    service.models = OfflineModels()
    uvicorn.run(service.app, host='127.0.0.1', port=args.port, log_level='warning')
