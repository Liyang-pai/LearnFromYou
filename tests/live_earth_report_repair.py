"""回放地平说故障并有限调用真实模型，保存独立报告、关键轮次与用量证据。"""

import argparse
import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import config, prompts
from backend.llm import ModelClient, ModelError
from backend.policy import apply_result, mark_delivered, student_result_validator
from backend.report_contract import evidence_turns, source_bound
from backend.report_pipeline import ScoredReflectionReport
from backend.schemas import Knowledge, KnowledgeReviewResult, StudentResult, StudentState
from backend.settings import model_settings
from backend.student_prompt import compact_json, compact_learning_context
from backend.teaching_report import ReportService, export_markdown, read_classroom

SID = '73602980bfb3'
CLASSROOM = '2026-11-21_090452_73602980bfb3.jsonl'


def read_events(path):
    return [json.loads(line) for line in path.read_text(encoding='utf-8-sig').splitlines() if line.strip()]


def request_case(rows, phase, source_ids=None):
    """取首次请求及请求前的权威状态，不混入后续课堂内容。"""
    for index, row in enumerate(rows):
        if (row['type'] != 'llm_request' or row['data']['phase'] != phase
                or row['data'].get('attempt', 1) != 1):
            continue
        messages = row['data']['body']['messages']
        payload = json.loads(messages[-1]['content'])
        if source_ids is not None and payload.get('new_teacher_segment_ids') != source_ids:
            continue
        state = next(event['data']['state'] for event in reversed(rows[:index])
                     if event['data'].get('state') and event['type'] in
                     ('state', 'ready', 'reply', 'knowledge_review'))
        sources = {event['data']['id']: deepcopy(event['data']) for event in rows[:index]
                   if event['type'] == 'transcript'}
        parsed = StudentState.model_validate(state)
        sources.update({item.id: {'text': item.text} for item in parsed.knowledge if item.id.startswith('p')})
        outputs = [event['data']['raw'] for event in rows[index + 1:]
                   if event['type'] == 'llm_output' and event['data']['phase'] == phase]
        return {'state': parsed, 'sources': sources, 'payload': payload,
                'history': messages[1:-1] if phase == 'student' else [], 'outputs': outputs}
    raise ValueError(f'未找到 {phase} {source_ids} 的请求')


def system_prompt(lesson):
    return prompts.STUDENT + '\n原始确认画像（初始记录，知识修订以 current_state 为准）：\n' + compact_json({
        'student_profile': lesson['student_profile'],
        'teaching_scope_only': {'topic': lesson['topic'], 'points': lesson['points']}})


def arithmetic_review():
    """独立解释正例，撤回前提后用乘法计算重新支持局部结论。"""
    sources = {'t1': {'id': 't1', 'mode': 'text', 'text': '任何数的平方都大于0，所以3的平方大于0。'},
               't2': {'id': 't2', 'mode': 'text', 'text':
                      '撤回刚才任何数的平方都大于0的说法，0的平方等于0。另算3的平方，3乘3等于9，9大于0。'}}
    sources['p1'] = {'text': '能计算自然数乘法，知道3乘3等于9。'}
    state = StudentState(knowledge=[
        Knowledge(id='p1', text=sources['p1']['text'], status='understood', sources=['p1'], reason='明确前置知识'),
        Knowledge(id='ka', text='任何数的平方都大于0。', status='understood', sources=['t1'], reason='教师讲授'),
        Knowledge(id='kb', text='3的平方大于0。', status='understood', sources=['t1'], reason='依赖一般规则',
                  dependencies=[{'id': 'ka', 'revision': 1}])])
    previous = next(item for item in state.knowledge if item.id == 'ka')
    state, _ = apply_result(state, StudentResult(knowledge_updates=[previous.model_copy(update={
        'status': 'invalid', 'sources': ['t1', 't2'], 'correction_sources': ['t2'], 'reason': '教师明确撤回全称规则'})]),
        sources, {'t2'})
    payload = {'interaction_context': {'input_revision': 2, 'trigger': 'teacher_input', 'muted': False},
               'lesson_scope': ['平方运算'], 'student_level': 'normal', 'explicit_prerequisite_ids': ['p1'],
               'current_state': compact_learning_context(state.learning_context()),
               'new_teacher_segment_ids': [], 'teacher_sources': [value for key, value in sources.items() if key.startswith('t')],
               'review_targets': ['kb'], 'previous_student_utterances_not_knowledge_sources': []}
    return {'state': state, 'sources': sources, 'payload': payload, 'history': [],
            'lesson': {'topic': '平方运算', 'points': '平方含义与计算',
                       'student_profile': '能计算自然数乘法，知道3乘3等于9。尚未形成平方的一般规则。'}}


def review_validator(case):
    targets = set(case['payload']['review_targets'])
    evidence = {source for review in case['state'].pending_reviews for source in review.trigger_sources}

    def validate(result, final_attempt):
        if {item.id for item in result.knowledge_updates} != targets or any(item.supersedes for item in result.knowledge_updates):
            raise ValueError('复核只允许更新指定目标，不能替代其他知识')
        updated, _ = apply_result(case['state'], StudentResult(knowledge_updates=result.knowledge_updates),
                                  case['sources'], evidence)
        if targets & {item.knowledge_id for item in updated.pending_reviews}:
            raise ValueError('复核需使用当前依赖并明确理解障碍')
        return result
    return validate


async def offline(rows, report_events, lesson, directory):
    checks = {}
    t9 = request_case(rows, 'student', ['t9'])
    current = set(t9['payload']['new_teacher_segment_ids'])
    revision = t9['payload']['interaction_context']['input_revision']
    before = t9['state'].model_dump()
    failures = []
    for raw in t9['outputs'][:2]:
        try:
            apply_result(t9['state'], StudentResult.model_validate_json(raw), t9['sources'], current,
                         input_revision=revision)
        except ValueError as error:
            failures.append(str(error))
    checks['t9_original_revision_and_dependency_errors'] = len(failures) == 2
    fixed = json.loads(t9['outputs'][0])
    for item in fixed['knowledge_updates']:
        if item['id'] == 'k6':
            item.update(revision=1, status='unclear', reason='原观察前提的冲突仍未解释，保留推理障碍')
    updated, _ = apply_result(t9['state'], StudentResult.model_validate(fixed), t9['sources'], current,
                              input_revision=revision)
    k6 = next(item for item in updated.knowledge if item.id == 'k6')
    checks['t9_gap_retains_dependency'] = k6.status == 'unclear' and bool(k6.dependencies)
    checks['t9_failed_output_does_not_mutate_state'] = t9['state'].model_dump() == before

    arithmetic = arithmetic_review()
    checks['independent_fixture_withdraws_correct_premise'] = (
        next(item for item in arithmetic['state'].knowledge if item.id == 'ka').status == 'invalid'
        and {item.knowledge_id for item in arithmetic['state'].pending_reviews} == {'kb'})
    dependant = next(item for item in arithmetic['state'].knowledge if item.id == 'kb')
    independently_supported = KnowledgeReviewResult(knowledge_updates=[dependant.model_copy(update={
        'status': 'understood', 'sources': ['t2', 'p1'], 'dependencies': [], 'independent_sources': ['t2'],
        'reason': '教师用3乘3等于9且9大于0建立独立依据'})])
    review_validator(arithmetic)(independently_supported, False)
    checks['genuine_independent_explanation_can_remove_dependency'] = True

    t11 = request_case(rows, 'student', ['t11'])
    raw = t11['outputs'][0]
    events, calls = [], []

    async def emit(kind, data):
        events.append({'type': kind, 'data': deepcopy(data)})

    def respond(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': raw}}]})

    model = ModelClient(emit, snapshot={'api_key': 'offline-key', 'model': 'offline',
                                     'image_model': 'offline', 'base_url': 'https://offline.invalid'})
    await model.http.aclose()
    model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    try:
        result = await model.generate('student', system_prompt(lesson), t11['payload'], StudentResult,
            history=t11['history'], result_validator=student_result_validator(t11['state'], t11['sources'], {'t11'}, [],
                input_revision=t11['payload']['interaction_context']['input_revision'], require_response=True))
    finally:
        await model.close()
    checks['t11_one_request'] = len(calls) == 1
    checks['t11_raw_preserved'] = next(event['data']['raw'] for event in events if event['type'] == 'llm_output') == raw
    checks['t11_ignored_fields_logged'] = any(event['type'] == 'student_recovery' and
        event['data'].get('code') == 'readonly_fields_ignored' for event in events)
    submitted, candidate = apply_result(t11['state'], result, t11['sources'], {'t11'},
        input_revision=t11['payload']['interaction_context']['input_revision'])
    question = next(item for item in submitted.questions if item.id == candidate.question_id)
    checks['candidate_does_not_spend_attempt'] = question.attempts == 0
    mark_delivered(submitted, candidate)
    checks['delivery_spends_one_attempt'] = question.attempts == 1
    (directory/'t11-replay-events.json').write_text(json.dumps(events, ensure_ascii=False, indent=2)+'\n')

    classroom = read_classroom(config.data_root(), SID)
    bound = source_bound(ScoredReflectionReport, {item['id'] for item in classroom['teacher_segments']},
                         evidence_turns(classroom))
    first_report = next(event['data']['raw'] for event in report_events if event['type'] == 'llm_output')
    report = bound.model_validate_json(first_report)
    checks['original_report_accepted_with_primary_findings'] = any(item.kind == '重点改进' for item in report.findings)
    checks['empty_audio_keeps_capture_complete'] = classroom['presentation']['capture_status'] == 'complete'

    class RecordedReport:
        async def generate(self, phase, prompt, payload, contract):
            return contract.model_validate_json(first_report)

        async def close(self):
            pass

    service = ReportService(lambda emit: RecordedReport())
    try:
        await service.start(SID)
        await service.tasks[SID]
        public = service.get(SID)
        checks['recorded_report_ready'] = public['status'] == 'ready'
        checks['recorded_report_has_evidence_scores'] = any(row['score'] is not None for row in public['scores'].values())
        (directory/'report-replayed.json').write_text(json.dumps(public, ensure_ascii=False, indent=2)+'\n')
        (directory/'report-replayed.md').write_text(export_markdown(public), encoding='utf-8')
    finally:
        await service.close()
    return {'checks': checks, 't9_errors': failures, 'status': 'passed' if all(checks.values()) else 'failed'}


async def live(rows, lesson, directory, snapshot, limit, previous=None):
    summary = {'status': 'running', 'request_limit': limit, 'requests': 0, 'responses': 0,
               'model': snapshot['model'], 'base_url': snapshot['base_url'], 'usage': {}, 'cases': []}
    retry_names = None
    if previous:
        retry_names = {case['case'] for case in previous['cases'] if case['status'] not in ('passed', 'ready')}
        if not retry_names:
            raise ValueError('上一轮没有需要重测的失败案例')
        summary.update(requests=previous['requests'], responses=previous['responses'], usage=deepcopy(previous['usage']),
                       cases=[case for case in previous['cases'] if case['case'] not in retry_names],
                       previous_run=previous)
    active = 'report'

    def save():
        text = json.dumps(summary, ensure_ascii=False, indent=2).replace(snapshot['api_key'], '[REDACTED]')
        (directory/'summary.json').write_text(text+'\n')

    class TrackedModel(ModelClient):
        def __init__(self, emit):
            case = active

            async def audit(kind, data):
                if kind == 'llm_request':
                    if summary['requests'] >= limit:
                        raise ModelError('本轮定向验收到达请求上限')
                    summary['requests'] += 1
                if kind == 'llm_output':
                    summary['responses'] += 1
                    for key, value in data.get('usage', {}).items():
                        if type(value) in (int, float):
                            summary['usage'][key] = summary['usage'].get(key, 0) + value
                with (directory/'model-events.jsonl').open('a') as stream:
                    stream.write(json.dumps({'case': case, 'type': kind, 'data': data}, ensure_ascii=False)+'\n')
                save()
                await emit(kind, data)
            super().__init__(audit, snapshot=snapshot)

    async def observe(kind, data):
        pass

    if retry_names is None or 'report' in retry_names:
        service = ReportService(TrackedModel)
        started = time.monotonic()
        # 独立实时目录不复制旧报告缓存，确保真正走新提示词生成路径。
        try:
            await service.start(SID)
            await service.tasks[SID]
            public = service.get(SID)
            findings = (public.get('report') or {}).get('findings', [])
            report_checks = {'has_primary_issue': any(item['kind'] == '重点改进' and
                any(evidence['actor'] == 'teacher' and evidence['source_id'] in ('t6', 't9', 't14')
                    for evidence in item['evidence']) for item in findings),
                'has_evidence_scores': any(item['score'] is not None for item in public.get('scores', {}).values())}
            report_status = public['status']
            if report_status == 'ready' and not all(report_checks.values()):
                report_status = 'semantic_failure'
            summary['cases'].append({'case': active, 'status': report_status, 'error': public['error'],
                                     'seconds': round(time.monotonic()-started, 3),
                                     'checks': report_checks,
                                     'primary_findings': [item['title'] for item in findings if item['kind'] == '重点改进']})
            (directory/'report.json').write_text(json.dumps(public, ensure_ascii=False, indent=2)+'\n')
            if public['status'] == 'ready':
                (directory/'report.md').write_text(export_markdown(public), encoding='utf-8')
            elif any(term in public['error'] for term in ('连接失败', 'HTTP 401', 'HTTP 402', 'HTTP 403', 'HTTP 429')):
                summary.update(status='blocked', blocking_error=public['error'])
                save()
                return summary
        finally:
            await service.close()

    cases = [('t9', request_case(rows, 'student', ['t9']), 'student'),
             ('t11', request_case(rows, 'student', ['t11']), 'student'),
             ('repeated_conclusion', request_case(rows, 'knowledge_review'), 'knowledge_review'),
             ('independent_explanation', arithmetic_review(), 'knowledge_review')]
    for active, case, phase in cases:
        if retry_names is not None and active not in retry_names:
            continue
        model = TrackedModel(observe)
        started = time.monotonic()
        payload = deepcopy(case['payload'])
        payload['current_state'] = compact_learning_context(case['state'].learning_context())
        try:
            if phase == 'student':
                ids = set(payload['new_teacher_segment_ids'])
                result = await model.generate(phase, system_prompt(lesson), payload, StudentResult, history=case['history'],
                    result_validator=student_result_validator(case['state'], case['sources'], ids, [],
                        input_revision=payload['interaction_context']['input_revision'], require_response=True))
                state, candidate = apply_result(case['state'], result, case['sources'], ids,
                    input_revision=payload['interaction_context']['input_revision'])
                checks = {'valid_result': True}
                if active == 't9':
                    k6 = next(item for item in state.knowledge if item.id == 'k6')
                    checks['gap_preserved'] = bool(k6.dependencies) and k6.status in ('unclear', 'conflict')
                detail = {'state': state.model_dump(), 'candidate': candidate.model_dump(), 'checks': checks}
            else:
                result = await model.generate(phase, system_prompt(case.get('lesson', lesson)) + '\n' + prompts.KNOWLEDGE_REVIEW,
                    payload, KnowledgeReviewResult, result_validator=review_validator(case))
                item = result.knowledge_updates[0]
                checks = {'semantic_expectation': (item.status == 'unclear' and bool(item.dependencies))
                          if active == 'repeated_conclusion' else
                          (item.status == 'understood' and not item.dependencies and bool(item.independent_sources))}
                detail = {'result': result.model_dump(), 'checks': checks}
            summary['cases'].append({'case': active, 'status': 'passed' if all(checks.values()) else 'semantic_failure',
                                     'seconds': round(time.monotonic()-started, 3), **detail})
        except (ModelError, ValueError) as error:
            summary['cases'].append({'case': active, 'status': 'failed', 'error': str(error),
                                     'seconds': round(time.monotonic()-started, 3)})
            if any(term in str(error) for term in ('连接失败', 'HTTP 401', 'HTTP 402', 'HTTP 403', 'HTTP 429')):
                summary.update(status='blocked', blocking_error=str(error))
                break
        finally:
            await model.close()
            save()
    if summary['status'] != 'blocked':
        summary['status'] = 'passed' if len(summary['cases']) == 5 and all(
            case['status'] in ('passed', 'ready') for case in summary['cases']) else 'failed'
    save()
    return summary


async def run(args):
    snapshot = model_settings() if args.live else None
    source = args.source.resolve()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    logs = output/'logs'
    logs.mkdir()
    original = source/CLASSROOM
    digest = hashlib.sha256(original.read_bytes()).hexdigest()
    shutil.copy2(original, logs/CLASSROOM)
    config.DATA_ROOT = output
    starting_hashes = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted((ROOT/'backend').glob('*.py'))}
    rows = read_events(original)
    lesson = next(row['data']['lesson'] for row in rows if row['type'] == 'finished')
    if args.live:
        if not snapshot['api_key']:
            raise ValueError('没有可用的模型密钥')
        previous = json.loads(args.retry_failed_from.read_text()) if args.retry_failed_from else None
        result = await live(rows, lesson, output, snapshot, args.max_requests, previous)
    else:
        report_events = read_events(source/f'{SID}.teaching-report-reflection.events.jsonl')
        result = await offline(rows, report_events, lesson, output)
    result.update({'original_sha256': digest,
                   'original_unchanged': hashlib.sha256(original.read_bytes()).hexdigest() == digest,
                   'at': datetime.now(timezone.utc).isoformat(),
                   'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                   'backend_sha256': {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                                      for path in sorted((ROOT/'backend').glob('*.py'))}})
    result['backend_unchanged_during_run'] = result['backend_sha256'] == starting_hashes
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if snapshot and snapshot['api_key']:
        text = text.replace(snapshot['api_key'], '[REDACTED]')
    (output/'summary.json').write_text(text+'\n')
    print(json.dumps({'status': result['status'], 'requests': result.get('requests', 0),
                      'responses': result.get('responses', 0), 'output': str(output)}, ensure_ascii=False))
    if result['status'] != 'passed':
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='显式调用真实模型，可能产生费用')
    parser.add_argument('--source', type=Path, default=ROOT/'logs/earth-report-repair-20261121/before')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-requests', type=int, default=12, choices=range(1, 13))
    parser.add_argument('--retry-failed-from', type=Path,
                        help='仅重测上一份汇总的失败案例，累计计入同一批请求上限')
    asyncio.run(run(parser.parse_args()))
