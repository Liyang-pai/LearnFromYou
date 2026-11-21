"""真实 API 验收：五科完整学生反应回归、三节长课及结算，隔离数据并记录耗时与重试。

显式 --live 才调用模型；保留逐轮证据和代码指纹，不修改运行中的服务或产品参数。
"""
import argparse
import asyncio
import contextvars
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config, session as session_module
from backend.llm import ModelClient, ModelError
from backend.report_pipeline import compact, encode, chunks, SHORT_LIMIT
from backend.schemas import Lesson
from backend.settings import model_settings
from backend.teaching_report import ReportService, export_markdown

ACTIVE = contextvars.ContextVar('acceptance_case', default='setup')
TURN = contextvars.ContextVar('acceptance_turn', default='setup')


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def regression_cases():
    result = []
    for path in sorted((ROOT / 'docs/学生反应回归测试').glob('0*.md')):
        text = path.read_text(encoding='utf-8')
        lesson = Lesson(topic=re.search(r'主题：(.*?)。年级：', text)[1],
            grade=re.search(r'年级：(.*?)；', text)[1], subject=re.search(r'学科：(.*?)；', text)[1],
            points=re.search(r'知识点填写：(.*?)。', text)[1] + '。',
            student_profile=re.search(r'### 固定学生画像[^\n]*\n\n```text\n(.*?)\n```', text, re.S)[1])
        turns = []
        for label, heading, block in re.findall(r'### ([ABC]\d+)([^\n]*)\n\n((?:>[^\n]*\n?)+)', text):
            turns.append({'label': label, 'required_reply': '提问' in heading,
                          'teacher': '\n'.join(line.removeprefix('> ').removeprefix('>')
                                               for line in block.strip().splitlines())})
        assert len(turns) == 25
        for route in ('A', 'BC'):
            result.append({'id': path.name[:2] + '-' + route, 'kind': 'regression',
                           'lesson': lesson.model_dump(),
                           'turns': [t for t in turns if t['label'][0] in route]})
    return result


def long_packet(subject, n):
    """每段含不同数值的完整例解与条件比较，末尾只问一个迁移题。"""
    examples = []
    for j in range(8):
        x, a, b = n + j + 2, j + 2, n + 3
        if subject == '01':
            c = a * x + b
            examples.append(f'例{n}-{j+1}：方程{a}x＋{b}＝{c}，目标是隔离未知数。'
                f'左边加了{b}，所以两边同时减{b}，得到{a}x＝{c-b}。'
                f'两边同时除以非零的{a}，得到x＝{x}。检验是{a}×{x}＋{b}＝{c}，等号两边相等。'
                '减去常数和除以系数是不同操作；不能把系数当加数移过去。每次变形都要保留等式两边相等。')
        elif subject == '03':
            v, rho = x * 10, a
            examples.append(f'例{n}-{j+1}：均匀物块在固定温度和状态下，体积{v}立方厘米，质量{v*rho}克。'
                f'每立方厘米对应{rho}克，所以密度是{rho}克每立方厘米。把它均匀切为两半，'
                f'每块质量{v*rho//2}克、体积{v//2}立方厘米，比值仍为{rho}。'
                '这里变化的是样品大小，比较的是同一种物质且温度状态相同；不能据质量变小就说密度变小。'
                f'写成国际单位时，{rho}克每立方厘米等于{rho*1000}千克每立方米；分子分母要同时换算。')
        else:
            r, i = x, a
            examples.append(f'例{n}-{j+1}：讨论温度保持稳定的定值电阻，R＝{r}欧，电流{ i}安，电压为{r*i}伏。'
                f'保持电阻不变，电压提高到{2*r*i}伏，电流变为{2*i}安。'
                f'再另作比较，保持{r*i}伏不变，电阻增为{2*r}欧，电流变成{i/2:g}安。'
                '这两个比较固定的量不同，不能把固定电阻的正比例结论搬到电阻同时变化的情境。'
                'R＝U/I是测定电阻的计算式，并不意味着这只定值电阻由外加电压决定。')
    if subject == '01':
        intro = ('继续用不同数字检查等式操作。每个例题的共同线索是同一个操作同时作用于两边，'
                 '并在原式检验。下面不是新的移项口令，而是把符号简写与依据相连。')
        question = f'请独立解{n+2}x－{n+1}＝{(n+2)*3-(n+1)}，说明每步依据，并代回检验。'
    elif subject == '03':
        intro = ('继续比较同种物质的样品。密度表示单位体积对应的质量；在物质均匀、温度状态相同的前提下，'
                 '切分或合并改变总质量和总体积，不改变比值。若状态改变，则不能直接套用这项条件。')
        question = f'请判断同种均匀物质的一块样品质量{(n+2)*20}克、体积20立方厘米，切掉一半后密度是多少，为什么？'
    else:
        intro = ('继续辨析欧姆定律的控制条件。必须先辨认同一段导体以及比较时固定的量，再判断正反比例。'
                 '没有电阻或电压变化的信息，就不能只凭电流变化猜测唯一原因。灯丝温度显著变化时也要谨慎使用定值电阻假设。')
        question = f'请计算电压{(n+2)*2}伏、电阻{n+2}欧的电流；若电压和电阻都加倍，电流如何变化？解释条件。'
    return intro + '\n' + '\n'.join(examples) + '\n' + question


def plan():
    base = regression_cases()
    result = list(base)
    for number in ('01', '03', '04'):
        initial = next(c for c in base if c['id'] == number + '-A')
        turns = list(initial['turns']) + [
            {'label': f'L{n:02d}', 'required_reply': True, 'teacher': long_packet(number, n)}
            for n in range(1, 23)]
        assert all(len(t['teacher']) <= 5000 for t in turns)
        assert sum(len(t['teacher']) for t in turns) > SHORT_LIMIT
        result.append({'id': number + '-long', 'kind': 'long_stress', 'lesson': initial['lesson'], 'turns': turns})
    return result


async def run(args):
    planned = plan()
    if args.select:
        planned = [case for case in planned if case['id'] in args.select]
        missing = set(args.select) - {case['id'] for case in planned}
        if missing:
            raise ValueError('未知课堂编号：' + ', '.join(sorted(missing)))
    if not args.live:
        print(json.dumps([{'id': c['id'], 'turns': len(c['turns']),
                           'teacher_chars': sum(len(t['teacher']) for t in c['turns'])}
                          for c in planned], ensure_ascii=False, indent=2))
        return
    snapshot = model_settings()
    if not snapshot['api_key']:
        raise RuntimeError('没有可用的 API 密钥')
    output = (ROOT / args.output).resolve()
    output.mkdir(parents=True, exist_ok=False)
    # 只改变本测试进程的数据目录；模型配置已在隔离前冻结。
    config.DATA_ROOT = output
    summary = {'status': 'running', 'started_at': now(), 'model': snapshot['model'],
               'base_url': snapshot['base_url'], 'request_limit': args.max_requests,
               'concurrency': args.concurrency, 'requests': 0, 'responses': 0,
               'phases': {}, 'usage': {}, 'cases': [], 'anomalies': [],
               'scope': '真实Session及ReportService；文字逐轮；静音关闭；TTS及ASR关闭；正常时钟发言',
               'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'source_hashes': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                 for p in (ROOT/'backend').glob('*.py')}}
    (output/'source-hashes.json').write_text(json.dumps(summary['source_hashes'], indent=2))
    source_dir = output/'source-snapshot'
    source_dir.mkdir()
    for path in (ROOT/'backend').glob('*.py'):
        (source_dir/path.name).write_bytes(path.read_bytes())
    (source_dir/'live_classroom_acceptance.py').write_bytes(Path(__file__).read_bytes())
    active_turns = {}
    def save():
        temporary = output/'summary.tmp'
        temporary.write_text(json.dumps(summary, ensure_ascii=False, indent=2).replace(snapshot['api_key'], '[REDACTED]'))
        temporary.replace(output/'summary.json')

    class Tracked(ModelClient):
        def __init__(self, emit, **kwargs):
            async def track(kind, data):
                if kind == 'llm_request':
                    if summary.get('blocking_error') or summary['requests'] >= args.max_requests:
                        raise ModelError(summary.get('blocking_error', '达到验收请求上限'))
                    summary['requests'] += 1
                    phase = data['phase']
                    summary['phases'][phase] = summary['phases'].get(phase, 0) + 1
                if kind == 'llm_output':
                    summary['responses'] += 1
                    for k, v in data.get('usage', {}).items():
                        if isinstance(v, (int, float)):
                            summary['usage'][k] = summary['usage'].get(k, 0) + v
                if ('error' in kind or kind == 'student_recovery'
                        or kind == 'llm_request' and data.get('attempt', 1) > 1):
                    summary['anomalies'].append({'case': ACTIVE.get(), 'turn': active_turns.get(ACTIVE.get(), TURN.get()),
                                                'at': now(), 'event': kind, 'data': data})
                # 请求体原文保存在每节课的产品日志中，汇总日志避免重复保存长上下文。
                small = {k: v for k, v in data.items() if k != 'body'}
                event = {'case': ACTIVE.get(), 'turn': active_turns.get(ACTIVE.get(), TURN.get()), 'at': now(), 'event': kind, 'data': small}
                with (output/'model-events.jsonl').open('a') as stream:
                    stream.write(json.dumps(event, ensure_ascii=False).replace(snapshot['api_key'], '[REDACTED]')+'\n')
                save()
                await emit(kind, data)
            super().__init__(track, snapshot=snapshot)
        async def generate(self, *a, **kw):
            try:
                return await super().generate(*a, **kw)
            except ModelError as exc:
                if re.search(r'HTTP (401|402|403)\b', str(exc)):
                    summary['blocking_error'] = str(exc)
                raise

    session_module.ModelClient = Tracked
    reports = ReportService(Tracked)
    semaphore = asyncio.Semaphore(args.concurrency)

    async def one(case):
        async with semaphore:
            ACTIVE.set(case['id'])
            events = []
            entry = {**case, 'turns': [], 'planned_turns': len(case['turns']), 'status': 'running',
                     'started_at': now(), 'issues': [], 'manual_retries': []}
            summary['cases'].append(entry)
            if summary.get('blocking_error'):
                entry.update(status='not_run', error=summary['blocking_error'], finished_at=now())
                save()
                return
            async def send(event):
                events.append(event)
            session = session_module.Session(send, None)
            entry['session_id'] = session.id
            started = time.monotonic()
            save()
            try:
                await session.start(Lesson(**case['lesson']), muted=False, tts=False, asr_review=False)
                entry['preparation_seconds'] = round(time.monotonic()-started, 3)
                entry['preparation'] = session.preparation.model_dump()
                entry['log_file'] = session.log_file
                for turn in case['turns']:
                    # Session 后台任务继承启动时的上下文；逐轮标号以产品来源和本轮事件为准。
                    TURN.set(turn['label'])
                    active_turns[case['id']] = turn['label']
                    record = {**turn, 'status': 'running'}
                    entry['turns'].append(record)
                    before = len(events)
                    began = time.monotonic()
                    await session.add_transcript(turn['teacher'])
                    await session.flush()
                    retried = False
                    async with asyncio.timeout(240):
                        while True:
                            if session.failed_batch and session.retry_event.is_set() is False:
                                if retried:
                                    raise ModelError('本轮显式重试后仍失败')
                                failure = next(e['data'] for e in reversed(events) if e['type'] == 'error')
                                entry['manual_retries'].append({'turn': turn['label'], 'error': failure, 'at': now()})
                                retried = True
                                session.retry_event.set()
                            local = events[before:]
                            ready = any(e['type'] == 'state' for e in local) and not session.processing and session.teaching_queue.empty()
                            reviews = session.knowledge_review_busy or any(r.status == 'pending' for r in session.state.pending_reviews)
                            if ready and not reviews:
                                # 不虚构60秒静默：由生产clock_loop按实际时间发言。
                                if not session.candidate or any(e['type'] == 'reply' for e in local):
                                    break
                            await asyncio.sleep(.05)
                    local = events[before:]
                    states = [e for e in local if e['type'] == 'state']
                    replies = [e for e in local if e['type'] == 'reply']
                    transcript = next(e for e in local if e['type'] == 'transcript')
                    record.update(status='completed', seconds=round(time.monotonic()-began, 3),
                        processing_seconds=round(states[-1]['elapsed']-transcript['elapsed'], 3),
                        reply_seconds=round(replies[0]['elapsed']-transcript['elapsed'], 3) if replies else None,
                        replies=[e['data']['text'] for e in replies],
                        candidate=states[-1]['data']['candidate'], state=session.state.model_dump(),
                        source_ids=[e['data']['id'] for e in local if e['type'] == 'transcript'],
                        diagnostics=[e for e in local if e['type'] in ('error', 'student_recovery', 'student_validation_error', 'knowledge_review', 'suppressed')])
                    if turn['required_reply'] and not replies:
                        entry['issues'].append({'turn': turn['label'], 'issue': '明确提问未实际发言'})
                    if any(r.status == 'failed' for r in session.state.pending_reviews):
                        entry['issues'].append({'turn': turn['label'], 'issue': '知识复核失败'})
                    print(json.dumps({'case': case['id'], 'turn': turn['label'], 'seconds': record['seconds'],
                                      'reply': bool(replies), 'requests': summary['requests']}, ensure_ascii=False), flush=True)
                    save()
                await session.finish()
                final = next(e['data'] for e in reversed(events) if e['type'] == 'finished')
                entry['finished'] = final
                if final['unprocessed_sources'] or final['unreviewed_knowledge']:
                    entry['issues'].append({'issue': '结算前存在未完成内容', 'data': final})
                classroom, _ = reports.material(session.id)
                payload = compact(classroom)
                entry['report_input_chars'] = len(encode(payload))
                entry['report_expected_chunks'] = len(list(chunks(payload))) if len(encode(payload)) > SHORT_LIMIT else 0
                if case['kind'] == 'long_stress' and not entry['report_expected_chunks']:
                    entry['issues'].append({'issue': '长课没有触发报告分段'})
                TURN.set('report')
                active_turns[case['id']] = 'report'
                report_started = time.monotonic()
                await reports.start(session.id)
                await reports.tasks[session.id]
                result = reports.get(session.id)
                entry['report_first_status'] = result['status']
                if result['status'] == 'failed' and not summary.get('blocking_error'):
                    entry['manual_retries'].append({'turn': 'report', 'error': result['error'], 'at': now()})
                    await reports.start(session.id)
                    await reports.tasks[session.id]
                    result = reports.get(session.id)
                entry['report_seconds'] = round(time.monotonic()-report_started, 3)
                entry['report_result'] = result
                if result['status'] == 'ready':
                    (output/(case['id']+'-report.md')).write_text(export_markdown(result), encoding='utf-8')
                    requests_before = summary['requests']
                    await reports.start(session.id)
                    fresh = ReportService().get(session.id)
                    entry['cache_checks'] = {'no_extra_request': summary['requests'] == requests_before,
                                             'fresh_service_same_report': fresh['report'] == result['report']}
                else:
                    entry['issues'].append({'issue': '报告生成失败', 'error': result['error']})
                entry['status'] = 'completed'
            except Exception as exc:
                entry.update(status='failed', error=f'{type(exc).__name__}: {exc}')
                summary['anomalies'].append({'case': case['id'], 'turn': active_turns.get(case['id'], 'preparation'),
                                            'at': now(), 'event': 'case_error', 'data': entry['error']})
                if not entry['turns'] and isinstance(exc, ModelError) and '连接失败' in str(exc):
                    summary['blocking_error'] = '初始化时模型连接失败，停止后续课堂；请恢复网络后另开新批次'
                print(json.dumps({'case': case['id'], 'failed': entry['error']}, ensure_ascii=False), flush=True)
            finally:
                await session.close()
                entry['seconds'] = round(time.monotonic()-started, 3)
                entry['finished_at'] = now()
                save()

    save()
    try:
        await asyncio.gather(*(one(c) for c in planned))
    finally:
        await reports.close()
        summary['status'] = 'completed' if len(summary['cases']) == len(planned) and all(c['status'] == 'completed' for c in summary['cases']) else 'incomplete'
        summary['finished_at'] = now()
        summary['source_hashes_after'] = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                                         for p in (ROOT/'backend').glob('*.py')}
        summary['source_changed_during_run'] = summary['source_hashes_after'] != summary['source_hashes']
        save()
    print('RESULT', summary['status'], summary['requests'], str(output), flush=True)
    # 测试失败必须传回非零退出码，避免启动器把未完成批次显示为成功。
    return int(summary['status'] != 'completed' or any(
        c.get('issues') or not all(c.get('cache_checks', {}).values()) for c in summary['cases']))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--output', default='logs/classroom-acceptance-20261121')
    parser.add_argument('--concurrency', type=int, default=2)
    parser.add_argument('--max-requests', type=int, default=450)
    parser.add_argument('--select', nargs='+', help='仅运行给定课堂编号，例如 01-A 01-BC 01-long')
    sys.exit(asyncio.run(run(parser.parse_args())))
