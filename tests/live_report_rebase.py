"""有限验收变基后的课堂轮次、统一文本、五维评分和长课统计，并保存逐请求台账。"""
import argparse
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from dotenv import dotenv_values
from backend import config
from backend.llm import ModelClient, ModelError
from backend.policy import apply_result, mark_delivered, student_result_validator
from backend.schemas import StudentResult
from backend.settings import model_settings
from backend.student_prompt import compact_learning_context
from backend.teaching_report import ReportService, export_markdown, read_classroom
from live_earth_report_repair import CLASSROOM, SID, read_events, request_case, system_prompt


def write_classroom(root, sid, topic, turns):
    events = [{'type': 'ready', 'data': {'session_id': sid, 'lesson': {'topic': topic}}}]
    for turn in turns:
        if turn.get('role') == 'ai':
            events.append({'type': 'reply', 'data': {'kind': 'answer', 'text': turn['text']}})
        else:
            events.append({'type': 'transcript', 'data': turn})
            if turn.get('reviewed_text'):
                events.append({'type': 'review_completed', 'data': {'segments': [
                    {'id': turn['id'], 'text': turn['reviewed_text'], 'review_status': 'reviewed'}]}})
    events.append({'type': 'finished', 'data': {'session_id': sid, 'lesson': {'topic': topic}, 'state': {}}})
    (root/'logs'/f'{sid}.jsonl').write_text('\n'.join(json.dumps(row, ensure_ascii=False) for row in events))


async def run(args):
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=args.resume)
    (output/'logs').mkdir(exist_ok=args.resume)
    original = args.original.resolve()
    env = dotenv_values(original/'.env')
    config.SETTINGS_PATH = original/'settings.local.json'
    config.API_KEY = env.get('DEEPSEEK_API_KEY') or env.get('Deepseek_API') or config.API_KEY
    snapshot = model_settings()
    snapshot.update(base_url='https://api.deepseek.com', model='deepseek-chat')
    if not snapshot['api_key']:
        raise ValueError('未找到现有密钥')
    config.DATA_ROOT = output
    before = original/'logs/earth-report-repair-20261121/before'
    shutil.copy2(before/CLASSROOM, output/'logs'/CLASSROOM)
    rows = read_events(before/CLASSROOM)
    lesson = next(row['data']['lesson'] for row in rows if row['type'] == 'finished')
    summary = {'request_limit': 16, 'requests': 0, 'responses': 0, 'usage': {}, 'cases': [],
               'model': snapshot['model'], 'destination': snapshot['base_url']+'/chat/completions',
               'source_sha256': hashlib.sha256((before/CLASSROOM).read_bytes()).hexdigest()}
    previous = json.loads((output/'summary.json').read_text()) if args.resume else None
    if previous:
        summary = previous
        retried = ('long',) if args.long_only else ('earth', 'short', 'long')
        summary.setdefault('previous_attempts', []).extend(row for row in summary['cases'] if row['case'] in retried)
        summary['cases'] = [row for row in summary['cases'] if row['case'] not in retried]
        summary['status'] = 'running'
    prior_counts = {name: sum(row['requests'] for row in summary.get('previous_attempts', []) if row['case'] == name)
                    for name in ('earth', 'short', 'long')}
    active, case_count, case_limit = '', 0, 0

    def save():
        (output/'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2)+'\n')

    class Tracked(ModelClient):
        def __init__(self, emit):
            async def audit(kind, data):
                nonlocal case_count
                if kind == 'llm_request':
                    if summary['requests'] >= 16 or case_count >= case_limit:
                        raise ModelError('本轮或本案例已达到真实请求上限')
                    summary['requests'] += 1
                    case_count += 1
                if kind == 'llm_output':
                    summary['responses'] += 1
                    for key, value in data.get('usage', {}).items():
                        if type(value) in (int, float):
                            summary['usage'][key] = summary['usage'].get(key, 0) + value
                with (output/'model-events.jsonl').open('a') as stream:
                    stream.write(json.dumps({'case': active, 'type': kind, 'data': data}, ensure_ascii=False)+'\n')
                save()
                await emit(kind, data)
            super().__init__(audit, snapshot=snapshot)

    short_id, long_id = 'abc123000001', 'abc123000002'
    write_classroom(output, short_id, '密度', [
        {'id': 't1', 'mode': 'microphone', 'review_status': 'pending', 'text': '幽灵词，'*12,
         'reviewed_text': '怎么说呢，密度就是质量与体积的比值，符号ρ，单位可用克每立方厘米。'},
        {'id': 't2', 'mode': 'microphone', 'review_status': 'disabled',
         'text': '怎么说呢，同温均匀的同种物质，质量和体积同比例变化，密度相同。'},
        {'id': 't3', 'mode': 'microphone', 'review_status': 'pending',
         'text': '质量六克、体积三立方厘米的样品，密度是多少？请说出计算理由。'},
        {'role': 'ai', 'text': '密度是二克每立方厘米，因为六除以三等于二。'},
        {'id': 't4', 'mode': 'text', 'text': '对，先列质量除以体积，再代入数值，结果带上密度单位。'},
    ])
    long_turns = []
    for section in range(3):
        text = ''
        if section == 0:
            text = '怎么说呢，同种物质的质量越大，密度越大。'
        for n in range(1, 91):
            volume = n + section*90
            text += (f'例{volume}，在相同温度和物质均匀的条件下，取质量为{volume*2}克、体积为{volume}立方厘米的样品，'
                     '质量除以体积等于2克每立方厘米。切去一半后，质量和体积同时减半，比值仍是2，'
                     '所以样品大小改变不能直接推出密度改变。')
        if section == 2:
            text += '怎么说呢，更正开头的说法：在同温且均匀的同种物质中，质量和体积同比例变化，密度相同。样品大小改变不能直接推出密度改变。'
        long_turns.append({'id': f't{section+1}', 'mode': 'microphone', 'review_status': 'disabled', 'text': text})
    write_classroom(output, long_id, '密度与样品大小', long_turns)

    async def report_case(name, sid, budget):
        nonlocal active, case_count, case_limit
        active, case_count, case_limit = name, 0, budget-prior_counts.get(name, 0)
        started = time.monotonic()
        service = ReportService(Tracked)
        try:
            await service.start(sid)
            if sid in service.tasks:
                await service.tasks[sid]
            result = service.get(sid)
            checks = {'ready': result['status'] == 'ready'}
            if checks['ready']:
                checks['five_dimensions'] = len(result.get('scores', {})) == 5
                checks['accuracy_scored'] = result['scores']['accuracy']['score'] is not None
                if name == 'earth':
                    checks['primary_improvement'] = any(f['kind'] == '重点改进' for f in result['report']['findings'])
                else:
                    checks['filler_across_turns'] = {'word': '怎么说呢', 'count': 2} in (result['word_counts'] or [])
                    checks['no_raw_filler'] = all(x['word'] != '幽灵词' for x in result['word_counts'] or [])
                if name == 'long':
                    checks['late_correction'] = any(x.get('corrected') for x in result['scores']['accuracy'].get('checks', []))
                (output/f'{name}.md').write_text(export_markdown(result), encoding='utf-8')
            (output/f'{name}.json').write_text(json.dumps(result, ensure_ascii=False, indent=2)+'\n')
            summary['cases'].append({'case': name, 'status': 'passed' if all(checks.values()) else 'failed',
                'requests': case_count, 'seconds': round(time.monotonic()-started, 3), 'checks': checks, 'error': result['error']})
        finally:
            await service.close()
            save()

    async def student_case(name):
        nonlocal active, case_count, case_limit
        active, case_count, case_limit = name, 0, 2
        case = request_case(rows, 'student', [name])
        payload = deepcopy(case['payload'])
        payload['current_state'] = compact_learning_context(case['state'].learning_context())
        ids, revision = {name}, payload['interaction_context']['input_revision']
        async def emit(kind, data):
            pass
        model, started = Tracked(emit), time.monotonic()
        try:
            result = await model.generate('student', system_prompt(lesson), payload, StudentResult,
                history=case['history'], result_validator=student_result_validator(case['state'], case['sources'], ids, [],
                    input_revision=revision, require_response=True))
            state, candidate = apply_result(case['state'], result, case['sources'], ids, input_revision=revision)
            checks = {'valid': True}
            if name == 't9':
                k6 = next(item for item in state.knowledge if item.id == 'k6')
                checks['dependency_retained'] = bool(k6.dependencies) and k6.status in ('unclear', 'conflict')
            if name == 't11' and candidate.kind == 'question':
                question = next(item for item in state.questions if item.id == candidate.question_id)
                count = question.attempts
                mark_delivered(state, candidate)
                checks['delivery_increments_once'] = question.attempts == count+1
            (output/f'{name}.json').write_text(json.dumps({'state': state.model_dump(), 'candidate': candidate.model_dump()}, ensure_ascii=False, indent=2))
            summary['cases'].append({'case': name, 'status': 'passed' if all(checks.values()) else 'failed', 'checks': checks})
        except (ModelError, ValueError) as error:
            summary['cases'].append({'case': name, 'status': 'failed', 'error': str(error)})
        finally:
            summary['cases'][-1].update(requests=case_count, seconds=round(time.monotonic()-started, 3))
            await model.close()
            save()

    if not args.long_only:
        await report_case('earth', SID, 2)
        if not args.resume:
            await student_case('t9')
            await student_case('t11')
        await report_case('short', short_id, 2)
    await report_case('long', long_id, 10 if args.long_only else 8)
    summary['status'] = 'passed' if all(row['status'] == 'passed' for row in summary['cases']) else 'failed'
    save()
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--resume', action='store_true', help='保留累计请求台账，只重测报告案例')
    parser.add_argument('--long-only', action='store_true', help='经用户授权将剩余两次全局额度调给长课')
    asyncio.run(run(parser.parse_args()))
