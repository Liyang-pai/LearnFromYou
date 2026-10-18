"""Isolated history acceptance server; never writes production logs or credentials."""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
import uvicorn
from backend import config
from backend.llm import ModelClient, ModelError


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--port', type=int, default=8894)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--env-file', type=Path)
    parser.add_argument('--models', type=Path)
    parser.add_argument('--max-requests', type=int, default=14)
    args = parser.parse_args()
    root = config.ROOT
    args.data.mkdir(parents=True, exist_ok=True)
    if not (args.data / 'frontend').exists(): (args.data / 'frontend').symlink_to(root / 'frontend', target_is_directory=True)
    config.ROOT = args.data.resolve(); config.PORT = args.port
    if args.live:
        if not args.env_file: parser.error('--live requires --env-file')
        env = dotenv_values(args.env_file)
        config.API_KEY = env.get('DEEPSEEK_API_KEY') or env.get('Deepseek_API', '')
        config.BASE_URL = (env.get('LLM_BASE_URL') or config.BASE_URL).rstrip('/')
        config.MODEL = env.get('LLM_MODEL') or config.MODEL
        if not config.API_KEY: parser.error('API credential is missing')
        if args.models: config.MODELS_DIR = args.models
    else:
        config.API_KEY = ''
    from backend import app as module
    from backend import session as session_module
    from backend.teaching_report import ReportService
    usage_path = args.data / 'usage.json'
    usage = json.loads(usage_path.read_text()) if usage_path.exists() else {'requests': [], 'outputs': [], 'injected_failures': 0}
    def persist(): usage_path.write_text(json.dumps(usage, ensure_ascii=False, indent=2))
    class BoundedModel(ModelClient):
        def __init__(self, emit):
            async def audit(kind, data):
                if kind == 'llm_request':
                    failure = args.data / 'fail-next-report'
                    if data['phase'].startswith('teaching_report') and failure.exists():
                        failure.unlink(); usage['injected_failures'] += 1; persist()
                        raise ModelError('验收注入的一次报告失败，点击重试继续')
                    if len(usage['requests']) >= args.max_requests: raise ModelError('已到共享 API 请求上限')
                    usage['requests'].append({'phase': data['phase'], 'attempt': data['attempt']}); persist()
                if kind == 'llm_output':
                    usage['outputs'].append({'phase': data['phase'], 'usage': data.get('usage', {}), 'seconds': data['seconds']}); persist()
                await emit(kind, data)
            super().__init__(audit)
    if args.live:
        module.reports = ReportService(BoundedModel)
        session_module.ModelClient = BoundedModel
    else:
        from tests.test_teaching_report import save_classroom, FakeModel
        from tests.test_history import student_request, event
        path = save_classroom(args.data)
        events = [json.loads(line) for line in path.read_text().splitlines()]
        events[0]['data']['lesson'] = {'topic': '链表历史验收', 'grade': '大学一年级', 'subject': '数据结构', 'student_profile': '当时确认的画像：已知变量，不知道链表。'}
        events[-1]['data']['lesson'] = events[0]['data']['lesson']
        first_reply = next(i for i, item in enumerate(events) if item['type'] == 'reply')
        first = [{'id': 't1', 'text': '链表由节点组成。'}, {'id': 't2', 'text': '沿 next 指针逐个访问节点。'},
                 {'id': 't3', 'text': '最后一个节点指向空。'}]
        events[first_reply:first_reply] = [student_request(first, multimodal=True),
            student_request(first, attempt=2), event('state', {'processed_sources': ['t1', 't2', 't3']})]
        for index in range(4, 130):
            content = f'第 {index} 段：沿 next 访问节点。'
            events.insert(-1, event('transcript', {'id': f't{index}', 'text': content}))
            if index != 129:
                events.insert(-1, student_request([{'id': f't{index}', 'text': content}]))
                events.insert(-1, event('error', {'code': 'model_failure', 'sources': [f't{index}']}) if index == 5
                              else event('state', {'processed_sources': [f't{index}']}))
        path.write_text('\n'.join(json.dumps(e, ensure_ascii=False) for e in events))
        second = args.data / 'logs/fedcba543210.jsonl'
        second.write_text(json.dumps({'type': 'transcript', 'data': {'id': 't1', 'text': '中断课堂的讲授'}}))
        calls = []
        class OfflineModel(FakeModel):
            async def generate(self, *a, **kw):
                usage['requests'].append({'phase': a[0]}); persist()
                await asyncio.sleep(.2)
                failure = args.data / 'fail-next-report'
                if failure.exists(): failure.unlink(); raise ModelError('验收注入的一次报告失败')
                return await super().generate(*a, **kw)
        module.reports = ReportService(lambda emit: OfflineModel(emit, calls))
    persist()
    uvicorn.run(module.app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__': main()
