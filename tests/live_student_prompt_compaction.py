"""以最多 16 次真实模型请求回放两段合成文字课堂，检查精简后的互动。"""

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, session as classroom
from backend.llm import ModelClient, ModelError
from backend.schemas import Lesson
from backend.settings import model_settings


MAX_REQUESTS = 16


async def wait_for(predicate):
    async with asyncio.timeout(185):
        while not predicate():
            await asyncio.sleep(.05)


async def run(output):
    snapshot = model_settings()
    if not snapshot['api_key']:
        raise RuntimeError('未配置模型密钥，无法回放')
    output.mkdir(parents=True, exist_ok=False)
    config.DATA_ROOT = output
    summary = {'model': snapshot['model'], 'request_cap': MAX_REQUESTS,
               'requests': 0, 'usage': [], 'cases': []}

    class BoundedClient(ModelClient):
        def __init__(self, emit, snapshot=None):
            async def record(kind, data):
                if kind == 'llm_request':
                    if summary['requests'] >= MAX_REQUESTS:
                        raise ModelError('达到 16 次 API 请求上限')
                    summary['requests'] += 1
                if kind == 'llm_output':
                    summary['usage'].append({key: data[key] for key in ('phase', 'attempt', 'usage')})
                await emit(kind, data)

            super().__init__(record, snapshot=snapshot)

    classroom.ModelClient = BoundedClient

    async def run_case(name, muted, exercise):
        events = []

        async def receive(event):
            events.append(event)

        session = classroom.Session(receive, None, model_snapshot=snapshot)
        case = {'name': name, 'steps': []}
        summary['cases'].append(case)

        async def step(label, text=None):
            before = len(events)
            if text is None:
                session.last_voice = time.monotonic() - 10
                await session.set_muted(False)
                await wait_for(lambda: any(
                    e['type'] == 'resume_response' and e['data']['status'] in ('generated', 'skipped', 'failed')
                    for e in events[before:]))
                failure = session.resume_failed
            else:
                await session.add_transcript(text)
                await session.flush()
                await wait_for(lambda: session.failed_batch is not None
                               or any(e['type'] == 'state' for e in events[before:]))
                failure = session.failed_batch is not None
            if failure:
                error = next((e['data'].get('message') for e in reversed(events[before:])
                              if e['type'] in ('error', 'resume_response')), None)
                raise AssertionError(f'{label} 未完成：{error}')
            await wait_for(lambda: not session.processing)
            await session.try_speak(10)
            replies = [{key: e['data'][key] for key in
                        ('kind', 'text', 'question_id', 'sources', 'response_request_ids')}
                       for e in events[before:] if e['type'] == 'reply']
            case['steps'].append({'label': label, 'teacher': text, 'replies': replies,
                                  'requests': [r.model_dump() for r in session.state.response_requests],
                                  'questions': [q.model_dump() for q in session.state.questions]})
            return replies

        try:
            await session.start(Lesson(topic='合成整数运算课堂', student_profile=(
                '已有知识：会做整数加减法。\n知识边界：未学过本课自定义的操作和符号。'
            )), muted=muted, tts=False, asr_review=False)
            case['log'] = session.log_file
            await exercise(session, step)
            case['status'] = 'passed'
        except Exception as exc:
            case['status'] = 'failed'
            case['error'] = f'{type(exc).__name__}: {exc}'
        finally:
            await session.close()
        print(name, case['status'], case.get('error', ''), flush=True)

    async def withdrawal(session, step):
        assert not await step('第一题', '请回答第一题，3加4等于多少？')
        assert not await step('第二题', '再请回答第二题，5加6等于多少？第一题先保留。')
        assert not await step('撤回第一题', '第一题不用回答了，只需要回答第二题。')
        first, second = session.state.response_requests
        assert (first.status, second.status) == ('dismissed', 'pending')
        replies = await step('解除静音')
        assert len(replies) == 1 and '11' in replies[0]['text']
        assert replies[0]['response_request_ids'] == [second.id]
        assert [r.status for r in session.state.response_requests] == ['dismissed', 'answered']
        replies = await step('追问实际发言', '你刚才为什么答 11？请说出你用的是哪两个数。')
        assert len(replies) == 1 and replies[0]['kind'] == 'answer'
        assert '5' in replies[0]['text'] and '6' in replies[0]['text']
        assert second.id not in replies[0]['response_request_ids']

    async def questions(session, step):
        replies = await step('引入未解释的操作', '三号盒的操作是先把输入做一次折光，再加 4。请说说你哪里没理解。')
        assert len(replies) == 1 and replies[0]['kind'] == 'question' and '折光' in replies[0]['text']
        old_id = replies[0]['question_id']
        await step('补足定义', '这里的折光就是把原数加 3。所以三号盒是先加 3，再加 4。')
        assert next(q for q in session.state.questions if q.id == old_id).status == 'resolved'
        replies = await step('新邀请', '同学，你现在对刚才的内容还有问题吗？')
        assert len(replies) == 1 and replies[0]['kind'] in ('answer', 'ack')
        assert next(q for q in session.state.questions if q.id == old_id).attempts == 1
        replies = await step('独立的新卡点', '五号盒要检查校验印。印记合格才能通过。我尚未说明如何判定印记合格。请说说你还有哪里不明白。')
        assert len(replies) == 1 and replies[0]['kind'] == 'question' and '印' in replies[0]['text']
        assert replies[0]['question_id'] != old_id

    try:
        await run_case('静音撤回与追问', True, withdrawal)
        if summary['requests'] < MAX_REQUESTS:
            await run_case('旧疑问解决和新卡点', False, questions)
    finally:
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))
        print('请求数', summary['requests'], '记录', output / 'summary.json', flush=True)
    return 0 if len(summary['cases']) == 2 and all(c['status'] == 'passed' for c in summary['cases']) else 1


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    raise SystemExit(asyncio.run(run(parser.parse_args().output.resolve())))
