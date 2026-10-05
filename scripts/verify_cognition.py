"""Replay original star-operation teaching segments through the real server/LLM.

Uses text replay, not original audio. Run while the local server is idle.
"""
import asyncio
import json
from pathlib import Path

import websockets

ROOT = Path(__file__).resolve().parents[1]


async def main():
    original = [json.loads(line) for line in (ROOT / 'logs/3e206b001729.jsonl').read_text(encoding='utf-8').splitlines()]
    segments = [event['data']['text'] for event in original if event['type'] == 'segment']
    old_final = next(event['data']['state'] for event in reversed(original) if event['type'] == 'state')
    assert any(k['id'] == 'k_star_lesson_end' for k in old_final['knowledge'])
    phases = []
    async with websockets.connect('ws://127.0.0.1:8765/ws/session', max_size=2**22) as ws:
        async def send(**event):
            await ws.send(json.dumps(event, ensure_ascii=False))

        async def receive(kind):
            while True:
                event = json.loads(await asyncio.wait_for(ws.recv(), 120))
                if event['type'] == 'error':
                    raise RuntimeError(event['data']['message'])
                if event['type'] == 'llm_request':
                    phases.append(event['data']['phase'])
                if event['type'] == kind:
                    return event['data']

        async def teach(text):
            await send(type='text', text=text)
            return await receive('state')

        await send(type='start', lesson={'topic':'星星运算'}, muted=True, tts=False)
        ready = await receive('ready')
        assert ready['assessment_available'] is True
        print('session', ready['session_id'], flush=True)
        unknown = await teach('星星运算还没有讲过。请回答：你能给出这个运算的完整规则吗？')
        assert not unknown['state']['knowledge'], unknown['state']['knowledge']
        print('before teaching:', unknown['candidate']['text'], flush=True)
        first = await teach(segments[0])
        assert first['state']['knowledge']
        await send(type='assessment', kind='apply')
        a1 = await receive('assessment_result')
        print('assessment 1:', a1['question']['text'], a1['answer']['text'], a1['evaluation']['verdict'], flush=True)
        question = await teach(segments[1])
        assert not any('老师问' in k['text'] or k['id'].endswith('_question') for k in question['state']['knowledge'])
        incomplete = await teach('老师纠正一下：刚才只说两个数直接相加，这个规则不完整。完整规则稍后再讲。请回答：你现在能给出完整规则吗？')
        assert not any('减' in k['text'] and k['status'] in ('tentative','understood') for k in incomplete['state']['knowledge'])
        print('incomplete correction:', incomplete['candidate']['text'], flush=True)
        for text in segments[2:5]:
            current = await teach(text)
        print('new classroom answer:', current['candidate']['text'], flush=True)
        assert '5' in current['candidate']['text'] or '五' in current['candidate']['text']
        final = await teach(segments[5])
        state = final['state']
        assert not any('结束' in k['text'] or '到这里' in k['text'] or k['id'] == 'k_star_lesson_end' for k in state['knowledge'])
        assert any(e['kind'] == 'lesson_end' for e in state['recent_events'])
        assert state['knowledge_history']
        assert any(k['status'] == 'conflict' for k in state['knowledge']) or any('相加' in h['previous']['text'] for h in state['knowledge_history'])
        assert any(('减' in k['text']) and k['status'] in ('tentative','understood') for k in state['knowledge'])
        assert all(k['status'] != 'verified' for k in state['knowledge'])
        await send(type='assessment', kind='apply')
        a2 = await receive('assessment_result')
        print('assessment 2:', a2['question']['text'], a2['answer']['text'], a2['evaluation']['verdict'], flush=True)
        assert a1['evaluation']['verdict'] == a2['evaluation']['verdict'] == 'passed'
        assert a1['evaluation']['coverage'] == a2['evaluation']['coverage'] == 'sufficient'
        await send(type='end')
        finished = await receive('finished')
        assert len(finished['assessments']) == 2
        assert finished['state'] == state  # Assessments never silently promote knowledge.
        log = ROOT / finished['log_file']
        records = [json.loads(line) for line in log.read_text(encoding='utf-8').splitlines()]
        assert sum(r['type'] == 'assessment_result' for r in records) == 2
        for phase in ('assessment_question','assessment_audit','assessment_answer','assessment_evaluation'):
            assert phases.count(phase) >= 2
        print('knowledge:', [(k['id'],k['status'],k['text']) for k in state['knowledge']], flush=True)
        print('events:', state['recent_events'], flush=True)
        print('history revisions:', len(state['knowledge_history']), flush=True)
        print('verified log:', log, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
