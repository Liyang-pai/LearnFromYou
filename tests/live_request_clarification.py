"""Opt-in bounded real-API acceptance for ordinary-request clarification."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config
from backend.schemas import Lesson
from backend.session import Session
from backend.runtime import RUNTIME


async def run(output, prior=()):
    output.mkdir(parents=True, exist_ok=True)
    config.ROOT = output
    config.DATA_ROOT = output
    config.DIRECT_PAUSE = config.SPEAK_PAUSE = .05
    events, checks = [], []
    budget = {'requests': 0, 'reserved_output_tokens': 0, 'input_bytes': 0}
    for path in prior:
        previous = json.loads(path.read_text(encoding='utf-8'))['budget']
        for key in budget:
            budget[key] += previous.get(key, 0)

    async def receive(event):
        events.append(event)
        if event['type'] == 'llm_request':
            body = event['data']['body']
            budget['requests'] += 1
            budget['reserved_output_tokens'] += body['max_tokens']
            budget['input_bytes'] += len(json.dumps(body['messages'], ensure_ascii=False).encode('utf-8'))
            if (budget['requests'] > 8 or budget['reserved_output_tokens'] > 36000
                    or budget['input_bytes'] > 600000):
                raise RuntimeError('Shared live-test budget exhausted before HTTP request')
            print('request', event['data']['phase'], event['data']['attempt'], flush=True)

    session = Session(receive, None)

    def check(name, passed, detail):
        checks.append({'case': name, 'passed': bool(passed), 'detail': detail})
        print(name, 'PASS' if passed else 'FAIL', json.dumps(detail, ensure_ascii=False), flush=True)
        if not passed:
            raise AssertionError(name)

    async def teach(text):
        before = len(events)
        version = session.state.version
        await session.add_transcript(text)
        await session.flush()
        async with asyncio.timeout(190):
            while session.state.version == version and not session.failed_batch:
                await asyncio.sleep(.1)
            while session.processing:
                await asyncio.sleep(.1)
        if session.failed_batch:
            raise AssertionError('Model batch failed')
        await session.try_speak(10)
        return [e['data'] for e in events[before:] if e['type'] == 'reply']

    def linked(reply, kind):
        requests = {r.id: r for r in session.state.response_requests}
        return bool(reply['response_request_ids']) and all(
            requests[rid].kind == kind and requests[rid].status == 'answered'
            for rid in reply['response_request_ids'])

    try:
        await session.start(Lesson(topic='自定义盒子规则', points='按折光操作计算盒子结果',
            student_profile='已有知识：知道整数和加法。未知：折光是老师自定义的新操作，尚未学习其定义。'),
            tts=False, asr_review=False)
        first = await teach('今天的盒子规则：把盒子里的数做一次“折光”操作，再加2。“折光”是我自定义的新操作，我还没有解释它是什么意思。同学，你听懂了吗？')
        check('understanding_first_clarification', len(first) == 1 and first[0]['kind'] == 'question'
              and linked(first[0], 'check_understanding'), first)
        qid = first[0]['question_id']
        q = next(q for q in session.state.questions if q.id == qid)
        check('first_counted', q.attempts == 1, q.model_dump())
        check('ordinary_request_no_exception_needed', first[0]['question_release_request'] is None,
              {'question_id': qid, 'request_ids': first[0]['response_request_ids']})
    finally:
        await session.close()
        usage = [e['data'].get('usage', {}) for e in events if e['type'] == 'llm_output']
        summary = {'runtime': RUNTIME, 'session_id': session.id, 'checks': checks, 'budget': budget,
                   'usage': usage, 'log': str(output / 'logs' / f'{session.id}.jsonl')}
        (output / 'acceptance.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
        print('RESULT', output / 'acceptance.json', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--prior', type=Path, nargs='*', default=[],
                        help='Prior acceptance summaries charged to the same shared budget')
    args = parser.parse_args()
    asyncio.run(run(args.output.resolve(), args.prior))
