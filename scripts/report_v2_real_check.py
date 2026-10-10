"""对已有课堂缓存只重试未完成阶段；需显式 --real，不覆盖原课堂记录。"""
import argparse
import asyncio
import json
import re
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import config
from backend.llm import ModelClient, ModelError
from backend.report_v2 import ReportService, markdown


async def check(args):
    if not args.real: raise ValueError('真实请求需要显式 --real；本程序不提供模拟成功结果。')
    if not re.fullmatch(r'[a-f0-9]{12}',args.session): raise ValueError('课堂编号无效，未发出请求。')
    if args.env_file:
        from dotenv import dotenv_values
        values = dotenv_values(args.env_file)
        config.API_KEY = values.get('DEEPSEEK_API_KEY') or values.get('Deepseek_API') or config.API_KEY
        config.BASE_URL = (values.get('LLM_BASE_URL') or config.BASE_URL).rstrip('/')
        config.MODEL = values.get('LLM_MODEL') or config.MODEL
    if not config.API_KEY: raise ValueError('未配置模型密钥，未发出请求。')
    if not 1 <= args.max_requests <= 8: raise ValueError('请求上限须在1～8之间。')
    source = ROOT/'logs'/f'{args.session}.report-v2.json'
    target_root = ROOT/'logs'/'quality-acceptance'
    target = target_root/'logs'/source.name
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists(): shutil.copy2(source, target)
    config.ROOT = target_root
    count = 0
    existing = json.loads(target.read_text(encoding='utf-8'))
    previous_requests = len(existing.get('model_usage', []))
    class BudgetClient(ModelClient):
        def __init__(self, emit):
            async def limited_emit(kind, data):
                nonlocal count
                if kind == 'llm_request':
                    if count >= args.max_requests or previous_requests + count >= 8:
                        raise ModelError('验收请求已达到本轮或该缓存累计上限（8次），停止调用。')
                    count += 1
                await emit(kind, data)
            super().__init__(limited_emit)
    service = ReportService(BudgetClient)
    before = service.get(args.session)
    print(json.dumps({'configured':True,'model':config.MODEL,'max_requests':args.max_requests,
                      'reuse':[k for k,v in before['stages'].items() if v['status'] in ('success','skipped')]},ensure_ascii=False),flush=True)
    service.start(args.session)
    if args.session in service.tasks: await service.tasks[args.session]
    view = ReportService().get(args.session)  # Reopen the persisted report, not only in-memory output.
    view['markdown'] = markdown(view)
    (target_root/'report.json').write_text(json.dumps(view,ensure_ascii=False,indent=2),encoding='utf-8')
    (target_root/'report.md').write_text(view['markdown'],encoding='utf-8')
    result = {'status':view['status'],'requests_this_run':count,'stages':view['stages'],
              'usage':view.get('model_usage', []),'export':str(target_root/'report.md')}
    (target_root/'validation.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--real',action='store_true')
    parser.add_argument('--session',required=True)
    parser.add_argument('--env-file',type=Path)
    parser.add_argument('--max-requests',type=int,default=4)
    asyncio.run(check(parser.parse_args()))
