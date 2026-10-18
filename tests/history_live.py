"""Bounded real websocket classroom and historical report acceptance driver.

Start history_server.py --live in a separate process first. No mock student replies.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import websockets


async def main(args):
    base = f'http://127.0.0.1:{args.port}'
    received = []
    async with httpx.AsyncClient(timeout=20) as client:
        health = (await client.get(base + '/api/health')).json()
        assert health['runtime']['code_root'] == str(Path(__file__).resolve().parents[1])
        assert health['model_configured']
        lesson = {'topic': '惯性的概念', 'grade': '初中八年级', 'subject': '物理',
                  'points': '惯性的含义；公交车刹车时乘客前倾；惯性不是力',
                  'student_profile': '初中八年级普通水平学生。已知物体可以静止或运动，知道运动快慢用速度描述。尚未学习惯性，需要通过生活例子理解惯性的含义。'}
        async with websockets.connect(f'ws://127.0.0.1:{args.port}/ws/session', max_size=12 * 1024 * 1024, open_timeout=60) as ws:
            async def send(**data): await ws.send(json.dumps(data, ensure_ascii=False))
            async def until(kind, timeout=180):
                async with asyncio.timeout(timeout):
                    while True:
                        event = json.loads(await ws.recv()); received.append(event)
                        if event['type'] == 'error': raise AssertionError(event['data']['message'])
                        if event['type'] == kind: return event['data']
            await send(type='start', lesson=lesson, muted=False, tts=False, asr_review=False)
            ready = await until('ready'); sid = ready['session_id']
            await send(type='text', text='这节课讲惯性。物体保持原来静止或匀速直线运动状态的性质叫惯性。惯性不是一种力。公交车突然刹车时，乘客仍有保持原来向前运动状态的性质，所以会向前倾。请你用自己的话回答：刹车时乘客为什么向前倾？惯性是不是一种力？')
            await until('state'); reply = await until('reply')
            assert reply['text'].strip()
            await send(type='text', text='补充总结：惯性是物体本身的性质，不能说受到惯性力。刹车时乘客前倾，是保持原来向前运动状态的表现。今天讲到这里。')
            await until('state')
            await send(type='end'); finished = await until('finished')
            assert not finished['unprocessed_sources']
        async def archive(id):
            async with asyncio.timeout(30):
                while True:
                    result = (await client.get(f'{base}/api/history/{id}')).json()
                    if result.get('status') == 'ready': return result
                    await asyncio.sleep(.15)
        async def report(id):
            async with asyncio.timeout(310):
                while True:
                    response = await client.get(f'{base}/api/teaching-report/{id}')
                    assert response.is_success, response.text
                    result = response.json()
                    if result['status'] in ('ready','failed'): return result
                    await asyncio.sleep(.5)
        meta = await archive(sid); assert meta['student_profile'] == lesson['student_profile']
        rows = (await client.get(f'{base}/api/history/{sid}/dialogue')).json()['items']
        log = args.data / 'logs' / f'{sid}.jsonl'
        events = [json.loads(line) for line in log.read_text().splitlines()]
        expected = [e for e in events if e['type'] in ('transcript','reply')]
        assert len(rows) == len(expected)
        assert [r['text'] for r in rows] == [e['data']['text'] for e in expected]
        downloaded = await client.get(f'{base}/api/history/{sid}/log')
        assert downloaded.content == log.read_bytes()
        primary = await report(sid); assert primary['status'] == 'ready', primary.get('error')
        # A separate copy exercises history's missing-report path; production cache stays intact.
        copy_sid = '123456abcdee'
        copy_log = args.data / 'logs' / f'{copy_sid}.jsonl'
        copy_log.write_text(log.read_text().replace(sid, copy_sid))
        await archive(copy_sid)
        assert (await client.get(f'{base}/api/teaching-report/{copy_sid}')).json()['status'] == 'idle'
        # Fail before the HTTP call; no paid API attempt is wasted on failure injection.
        (args.data / 'fail-next-report').write_text('1')
        evidence = {'session_id': sid, 'copy_session_id': copy_sid, 'student_reply': reply['text'],
                    'dialogue_count': len(rows), 'profile_matches': True, 'dialogue_matches_log': True,
                    'log_sha256': hashlib.sha256(downloaded.content).hexdigest(),
                    'primary_report_status': primary['status'], 'runtime': health['runtime']}
        (args.data / 'live-classroom.json').write_text(json.dumps(evidence, ensure_ascii=False, indent=2))
        print(json.dumps(evidence, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--port', type=int, default=8895)
    parser.add_argument('--data', type=Path, required=True)
    asyncio.run(main(parser.parse_args()))
