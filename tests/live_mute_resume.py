"""真实模型静音恢复验收；仅使用合成课堂内容，最多 14 次模型请求。"""
import asyncio
from datetime import datetime
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, session as session_module
from backend.llm import ModelClient, ModelError
from backend.schemas import Lesson

MAX_REQUESTS = 14
OUTPUT = Path('logs/mute-resume-live-20261014').resolve()


async def until(predicate, seconds=90):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.1)


async def run():
    if not config.API_KEY:
        raise RuntimeError('未配置模型密钥，无法进行真实模型验收')
    OUTPUT.mkdir(parents=True, exist_ok=True)
    summary_path = OUTPUT / 'summary.json'
    summary = {'date': datetime.now().isoformat(), 'request_cap': MAX_REQUESTS, 'requests': 0, 'cases': []}
    def save():
        summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    class BoundedClient(ModelClient):
        def __init__(self, emit):
            async def wrapped(kind, data):
                if kind == 'llm_request':
                    if summary['requests'] >= MAX_REQUESTS:
                        raise ModelError('达到真实模型验收的 14 次请求上限')
                    summary['requests'] += 1
                    save()
                await emit(kind, data)
            super().__init__(wrapped)

    session_module.ModelClient = BoundedClient
    config.ROOT = OUTPUT
    scenarios = [
        ('单个问题', [
            '今天定义“绒运算”：把两个整数相加。现在请你回答，2绒3等于多少？',
        ]),
        ('两个问题并撤回其一', [
            '现在请你回答第一个问题：3加4等于多少？',
            '另外请你回答第二个问题：5加6等于多少？第一个问题先保留。',
            '第一个问题不用回答了，只需要你回答第二个问题。',
        ]),
        ('静音中的学生疑问', [
            '定义“绒运算”：两个整数相加。再用一个从未解释过的“斜条映射”画出绒运算结果，我跳过如何从数字映射到斜条，后面你自己想一想。',
        ]),
    ]
    for name, turns in scenarios:
        case = {'name': name, 'turns': [], 'status': 'running'}
        summary['cases'].append(case)
        save()
        events = []
        async def emit(event):
            events.append(event)
        session = session_module.Session(emit, None)
        try:
            await session.start(Lesson(topic='整数运算与未讲过的图示', student_profile=(
                '已有知识：知道整数加法。\n理解基础：能计算个位数相加。\n'
                '知识边界：本课新定义尚未学习。\n理解困难：尚未学过斜条映射。')),
                muted=True, tts=False, asr_review=False)
            for turn in turns:
                previous = session.state.version
                await session.add_transcript(turn)
                await session.flush()
                await until(lambda: session.state.version > previous or session.failed_batch is not None)
                if session.failed_batch:
                    raise RuntimeError(next(e['data']['message'] for e in reversed(events) if e['type'] == 'error'))
                case['turns'].append({'teacher': turn, 'state': session.state.model_dump(),
                    'candidate': next(e['data']['candidate'] for e in reversed(events) if e['type'] == 'state')})
                save()
            before = len([e for e in events if e['type'] == 'reply'])
            if before:
                raise AssertionError('静音期间发生了实际回复')
            case['muted_requests'] = [r.model_dump() for r in session.state.response_requests]
            case['muted_questions'] = [q.model_dump() for q in session.state.questions]
            session.last_voice = time.monotonic() - 5
            await session.set_muted(False)
            await until(lambda: any(e['type'] == 'resume_response' and e['data']['status'] in
                ('generated', 'skipped', 'failed') for e in events), 180)
            if any(e['type'] == 'resume_response' and e['data']['status'] == 'failed' for e in events):
                raise RuntimeError(next(e['data']['message'] for e in reversed(events)
                    if e['type'] == 'resume_response' and e['data']['status'] == 'failed'))
            await session.try_speak(10)
            case['resume_events'] = [e['data'] for e in events if e['type'] == 'resume_response']
            case['replies'] = [e['data'] for e in events if e['type'] == 'reply']
            case['final_requests'] = [r.model_dump() for r in session.state.response_requests]
            case['final_questions'] = [q.model_dump() for q in session.state.questions]
            if name == '单个问题':
                assert len(case['muted_requests']) == 1
                assert len(case['replies']) == 1
                assert case['final_requests'][0]['status'] == 'answered'
            elif name == '两个问题并撤回其一':
                assert len(case['muted_requests']) == 2
                assert sorted(r['status'] for r in case['final_requests']) == ['answered', 'dismissed']
                assert len(case['replies']) == 1
            else:
                assert case['replies'] or not case['final_questions']
            case['status'] = 'passed'
        except Exception as exc:
            case['status'] = 'failed'
            case['error'] = str(exc)
        finally:
            case['session_id'] = session.id
            case['model_requests_so_far'] = summary['requests']
            save()
            await session.close()
        print(name, case['status'], case.get('error', ''), 'requests', summary['requests'], flush=True)
        if summary['requests'] >= MAX_REQUESTS:
            break
    print('SUMMARY', summary_path, flush=True)
    return 1 if any(c['status'] != 'passed' for c in summary['cases']) else 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(run()))
