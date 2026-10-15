"""Opt-in synthetic multi-turn check of prompt-only proactive questioning changes."""
import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config
from backend.runtime import RUNTIME
from backend.schemas import Lesson
from backend.session import Session
from live_question_release import wait_for


async def run(args):
    args.output.mkdir(parents=True, exist_ok=True)
    config.ROOT = args.output
    config.DIRECT_PAUSE = config.SPEAK_PAUSE = .05
    events, checks, snapshots = [], [], []
    async def receive(event):
        events.append(event)
        if event['type'] == 'llm_request':
            print('request', event['data']['phase'], event['data']['attempt'], flush=True)
    session = Session(receive, None)

    def check(name, passed, detail=''):
        checks.append({'case': name, 'passed': bool(passed), 'detail': detail})
        print(name, 'PASS' if passed else 'FAIL', detail, flush=True)
        if not passed:
            raise AssertionError(name + ': ' + detail)

    async def teach(text):
        before = len(events)
        await session.add_transcript(text)
        await session.flush()
        await wait_for(lambda: any(e['type'] == 'state' for e in events[before:]) or session.failed_batch)
        if session.failed_batch:
            raise AssertionError('模型未提交；详见原始日志')
        await wait_for(lambda: not session.processing)
        await session.try_speak(10)
        result = [e['data'] for e in events[before:] if e['type'] == 'reply']
        outputs = [e['data']['raw'] for e in events[before:] if e['type'] == 'llm_output' and e['data']['phase'] == 'student']
        raw = json.loads(outputs[-1]) if outputs else {}
        snapshots.append({'teacher': text, 'state': session.state.model_dump(),
                          'raw_candidate': raw.get('candidate'), 'raw_note': raw.get('note'),
                          'candidate': next(e['data']['candidate'] for e in events[before:] if e['type'] == 'state'),
                          'replies': [{'kind': r['kind'], 'text': r['text'], 'question_id': r['question_id']} for r in result]})
        return result

    def one_question(replies):
        return len(replies) == 1 and replies[0]['kind'] == 'question'

    try:
        await session.start(Lesson(topic='虚构卡片与读数规则', points='一号盒、三号盒、红卡读卡器、四号盒、五号盒',
            student_profile='已有知识：知道整数加减法。知道红卡经过读卡器时读数增加2。\n知识边界：未学习折光、星环或映辉操作的定义，也未学过各个盒子的规则。'),
            tts=False, asr_review=False)
        initial = await teach('一号盒把读数加1，二号盒把读数减1。先过一号再过二号，加1再减1，读数就回到原值。')
        check('complete_explanation_stays_quiet', not initial, str(initial))
        gap = await teach('接下来讲三号盒：先把读数进行折光，再把结果加4，两步做完就是三号盒的输出。')
        check('new_unclear_asks_without_invitation', one_question(gap), str([r['text'] for r in gap]))
        gap_id = gap[0]['question_id']
        check('unclear_kept_and_no_fake_invitation', any(k.status == 'unclear' for k in session.state.knowledge)
              and not session.state.response_requests and not gap[0]['question_release_request'])
        resolved = await teach('折光在本课中的定义就是把原读数加3。三号盒先加3再加4，相当于总共加7。')
        check('resolved_gap_does_not_repeat', not resolved and next(q for q in session.state.questions if q.id == gap_id).status == 'resolved', str(resolved))
        conflict = await teach('现在讲红卡读卡器：红卡每次经过读卡器时，读数增加5。后面的红卡计算都按这条规则进行。')
        check('new_conflict_asks_without_invitation', one_question(conflict), str([r['text'] for r in conflict]))
        conflict_id = conflict[0]['question_id']
        check('conflict_recorded_not_used_as_premise', any(k.status == 'conflict' for k in session.state.knowledge)
              and not conflict[0]['knowledge_ids'])
        waiting = await teach('再补充一个标签约定：红卡是红色标签，蓝卡是蓝色标签，颜色只用于区分卡片。')
        check('awaiting_answer_does_not_repeat', not waiting and next(q for q in session.state.questions if q.id == conflict_id).attempts == 1, str(waiting))
        followup = await teach('针对刚才红卡增加2还是增加5的疑问，我给你的说明是：两种说法都适用于同一个读卡器、同一种模式，没有其他隐藏条件。')
        check('incomplete_explanation_gets_one_followup', one_question(followup) and followup[0]['question_id'] == conflict_id, str([r['text'] for r in followup]))
        check('two_deliveries_still_defer', next(q for q in session.state.questions if q.id == conflict_id).attempts == 2
              and next(q for q in session.state.questions if q.id == conflict_id).status == 'deferred')
        stopped = await teach('红卡的这两种说法暂时不再解释，先接着讲其他内容。')
        check('deferred_not_repeated', not stopped, str(stopped))
        independent = await teach('四号盒的规则是先把读数进行星环操作，再把结果加1，就得到输出。')
        check('old_deferred_does_not_hide_independent_gap', one_question(independent) and independent[0]['question_id'] not in (gap_id, conflict_id), str([r['text'] for r in independent]))
        postponed = await teach('星环的含义我稍后再解释，现在先停在这一步。')
        check('explicit_postponement_waits', not postponed, str(postponed))
        await session.set_muted(True)
        muted = await teach('五号盒使用另一个独立操作：把读数进行映辉，再减1，得到输出。')
        check('muted_gap_registered_without_reply', not muted and any(q.status == 'pending' and q.attempts == 0 for q in session.state.questions))
        before = len([e for e in events if e['type'] == 'reply'])
        await session.set_muted(False)
        await wait_for(lambda: len([e for e in events if e['type'] == 'reply']) > before or session.resume_failed)
        latest = [e['data'] for e in events if e['type'] == 'reply'][-1]
        check('resume_proactively_asks_pending_independent_gap', not session.resume_failed and latest['kind'] == 'question'
              and latest['question_id'] not in (gap_id, conflict_id, independent[0]['question_id']), latest['text'])
    finally:
        await session.close()
        result = {'runtime': RUNTIME, 'session_id': session.id, 'checks': checks, 'snapshots': snapshots,
                  'requests': len([e for e in events if e['type'] == 'llm_request']), 'log': f'logs/{session.id}.jsonl'}
        (args.output / 'acceptance.json').write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print('RESULT', args.output / 'acceptance.json', flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', type=Path, required=True)
    asyncio.run(run(parser.parse_args()))
