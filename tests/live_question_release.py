"""Opt-in real-model classroom acceptance; no ASR required, no extra intention classifier."""
import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config
from backend.schemas import Lesson
from backend.session import Session
from backend.runtime import RUNTIME


async def wait_for(predicate, seconds=190):
    async with asyncio.timeout(seconds):
        while not predicate():
            await asyncio.sleep(.1)


async def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    config.ROOT = args.output
    config.DIRECT_PAUSE = config.SPEAK_PAUSE = .05
    events, checks = [], []
    async def receive(event):
        events.append(event)
        if event['type'] == 'llm_request':
            print('request', event['data']['phase'], event['data']['attempt'], flush=True)
        if event['type'] == 'error':
            print('classroom error', event['data']['message'], flush=True)
    session = Session(receive, None)

    async def teach(text):
        n = len([e for e in events if e['type'] == 'state'])
        before = len(events)
        await session.add_transcript(text)
        await session.flush()
        await wait_for(lambda: len([e for e in events if e['type'] == 'state']) > n or session.failed_batch)
        if session.failed_batch:
            raise AssertionError('课堂模型未提交，请检查日志')
        await wait_for(lambda: not session.processing)
        await session.try_speak(10)
        return [e['data'] for e in events[before:] if e['type'] == 'reply']

    def check(name, passed, detail=''):
        checks.append({'case': name, 'passed': bool(passed), 'detail': detail})
        print(name, 'PASS' if passed else 'FAIL', detail, flush=True)
        if not passed:
            raise AssertionError(name + ': ' + detail)

    try:
        await session.start(Lesson(topic='自定义盒子规则', points='盒子里的数如何变换',
            student_profile='已有知识：知道整数和加法。\n知识边界：尚未学习折光操作，不知道其定义。'),
            tts=False, asr_review=False)
        ready = next(e['data'] for e in events if e['type'] == 'ready')
        check('runtime_identity', ready['runtime'] == RUNTIME and bool(ready['student_prompt_fingerprint']))
        first = await teach('盒子规则是：把盒子里的数做一次“折光”操作，再加2。我还没有解释折光的含义。')
        check('first_question', len(first) == 1 and first[0]['kind'] == 'question', json.dumps([{'kind':r['kind'],'text':r['text']} for r in first],ensure_ascii=False))
        qid = first[0]['question_id']
        second = await teach('关于刚才那条规则，我仍未解释折光是什么。你可以再澄清一次最关键的缺口。')
        check('second_question', len(second) == 1 and second[0]['kind'] == 'question' and second[0]['question_id'] == qid, json.dumps([{'kind':r['kind'],'text':r['text']} for r in second],ensure_ascii=False))
        q = next(q for q in session.state.questions if q.id == qid)
        check('deferred_at_two', q.attempts == 2 and q.status == 'deferred')
        normal = await teach('我暂时不补充折光的定义，先继续讲：每个盒子都遵循刚才的流程。')
        check('autonomous_stop', not normal and next(q for q in session.state.questions if q.id == qid).attempts == 2, str(normal))
        invited = await teach('现在我明确问你：还有什么疑问吗？请把仍没弄懂的问题说出来。')
        check('invited_exception', len(invited) == 1 and invited[0]['kind'] == 'question'
              and invited[0]['question_id'] == qid and bool(invited[0]['question_release_request']), json.dumps([{'kind':r['kind'],'text':r['text']} for r in invited],ensure_ascii=False))
        check('count_three_still_deferred', next(q for q in session.state.questions if q.id == qid).attempts == 3
              and next(q for q in session.state.questions if q.id == qid).status == 'deferred')
        audio = await teach('刚才听不到你说话，请说句话确认你在听。')
        check('audio_check_not_question', len(audio) == 1 and audio[0]['kind'] == 'ack', json.dumps([{'kind':r['kind'],'text':r['text']} for r in audio],ensure_ascii=False))
        await session.set_muted(True)
        muted = await teach('还有什么疑问吗？请说出你刚才仍未弄懂的问题。')
        check('muted_invitation_recorded', not muted and any(r.kind == 'invite_questions' and r.status == 'pending' for r in session.state.response_requests))
        before = len([e for e in events if e['type'] == 'reply'])
        await session.set_muted(False)
        await wait_for(lambda: len([e for e in events if e['type'] == 'reply']) > before or session.resume_failed)
        last = [e['data'] for e in events if e['type'] == 'reply'][-1]
        check('unmute_fresh_question', not session.resume_failed and last['kind'] == 'question'
              and bool(last['question_release_request']) and next(q for q in session.state.questions if q.id == qid).attempts == 4, json.dumps({'kind':last['kind'],'text':last['text']},ensure_ascii=False))
        check('no_extra_classifier', all(e['data']['phase'] in ('preparation', 'student', 'student_resume', 'knowledge_review')
                                       for e in events if e['type'] == 'llm_request'))
        # Text and TTS are deliberately reported separately.
        checks.append({'case': 'tts', 'passed': None, 'detail': '此验收关闭系统播报，不代表已验证实际扬声器声音。'})
    finally:
        await session.close()
        result = {'runtime': RUNTIME, 'session_id': session.id, 'checks': checks,
                  'requests': len([e for e in events if e['type'] == 'llm_request']), 'log': f'logs/{session.id}.jsonl'}
        (args.output / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print('RESULT', args.output / 'acceptance.json', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
