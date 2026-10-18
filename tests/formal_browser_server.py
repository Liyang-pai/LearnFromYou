"""正式界面离线浏览器验收夹具：真实 API/协议，模型与 ASR 替身，禁止外网请求。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / 'tests'))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--port', type=int, default=18770)
    parser.add_argument('--report-delay', type=float, default=.3)
    args = parser.parse_args()
    args.data.mkdir(parents=True, exist_ok=True)
    from backend import config
    config.PORT, config.DATA_ROOT, config.SETTINGS_PATH = args.port, args.data, args.data / 'settings.local.json'
    config.API_KEY = 'offline-fixture-key'
    from backend import app as service, session, student_profile
    from backend.teaching_report import ReportService
    from test_teaching_report import save_classroom, FakeModel, model_output
    from test_history import student_request, event
    import httpx
    import uvicorn

    async def forbidden(*args, **kwargs):
        raise httpx.RequestError('离线夹具禁止提供方与外部网络请求')
    httpx.AsyncClient.send = forbidden

    class OfflineModels:
        busy = False
        activity = None
        async def acquire(self, *args, purpose='lesson'):
            if self.busy:
                raise ValueError('已有课堂或识别测试占用资源')
            self.busy, self.activity = True, purpose
            return None, None, 'offline'
        async def release(self): self.busy, self.activity = False, None
        async def shutdown(self): await self.release()
        def snapshot(self):
            return {'asr_ready': True, 'selected_id': 'offline', 'vad_ready': True, 'engine_state': 'idle',
                    'busy': self.busy, 'activity': self.activity, 'error': None, 'notice': '离线浏览器验收夹具',
                    'models_dir': str(args.data / 'models'), 'models': [{'id': 'offline', 'name': '离线验收模型',
                    'description': '仅用于协议和界面验收，不做真实语音识别', 'directory': 'offline',
                    'installed': True, 'selected': True, 'state': 'installed', 'size_bytes': 0,
                    'downloadable': False, 'languages': ['zh'], 'files': [], 'job': {}, 'source': '', 'license_url': ''}]}
    service.models = OfflineModels()

    class ClassroomModel:
        def __init__(self, emit, snapshot=None): self.emit = emit
        async def close(self): pass
        async def generate(self, phase, system, payload, output_type, **kwargs):
            await self.emit('llm_request', {'phase': phase, 'attempt': 1, 'body': {'messages': [{'role': 'user', 'content': json.dumps(payload, ensure_ascii=False)}]}})
            await asyncio.sleep(.15)
            if phase == 'preparation': return output_type(scope=[payload['topic']], boundary_note='离线验收范围')
            if phase == 'review_glossary': return output_type(terms=[])
            if phase == 'student_profile': return output_type(grade=payload.get('grade') or '初中八年级', subject=payload.get('subject') or '物理',
                points=payload.get('points') or '惯性概念', student_profile='知道物体可以运动和静止，尚未学习惯性。')
            sources = payload.get('new_teacher_segments', [])
            return output_type(addressed=True, candidate={'kind': 'answer', 'text': '我收到了这次完整讲授。', 'sources': [s['id'] for s in sources]})
    session.ModelClient = student_profile.ModelClient = ClassroomModel

    # Seed enough existing records to verify list and dialogue pagination.
    log = save_classroom(args.data)
    rows = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
    lesson = {'topic': '离线历史：链表', 'grade': '大学', 'subject': '数据结构', 'student_profile': '知道变量与指针。'}
    rows[0]['data']['lesson'] = rows[-1]['data']['lesson'] = lesson
    rows.insert(6, student_request([{'id': 't1', 'text': '链表由节点组成。'}, {'id': 't2', 'text': '沿 next 指针逐个访问节点。'}, {'id': 't3', 'text': '最后一个节点指向空。'}]))
    rows.insert(7, event('state', {'processed_sources': ['t1', 't2', 't3']}))
    for index in range(4, 65):
        text = f'第{index}次完整讲授。'
        rows[-1:-1] = [event('transcript', {'id': f't{index}', 'text': text}), student_request([{'id': f't{index}', 'text': text}]),
                        event('state', {'processed_sources': [f't{index}']})]
    log.write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in rows), encoding='utf-8')
    for index in range(22):
        sid = f'{index + 1:012x}'
        (args.data / 'logs' / f'{sid}.jsonl').write_text(json.dumps({'type': 'ready', 'data': {'session_id': sid,
            'lesson': {'topic': f'分页课堂 {index + 1}', 'grade': '初中', 'subject': '物理'}}}), encoding='utf-8')
    calls = []
    class ReportModel(FakeModel):
        async def generate(self, *a, **kw):
            if (args.data / 'fail-report').exists():
                (args.data / 'fail-report').unlink()
                from backend.llm import ModelError
                raise ModelError('离线注入的报告失败，可点击重试')
            await asyncio.sleep(args.report_delay)
            return await super().generate(*a, **kw)
    service.reports = ReportService(lambda emit: ReportModel(emit, calls))
    uvicorn.run(service.app, host='127.0.0.1', port=args.port, log_level='warning')


if __name__ == '__main__': main()
