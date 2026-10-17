"""真实 DeepSeek 画像回归：保存设置、画像、实际前置摘录、用量和课堂逐轮证据。

只使用合成教学数据；显式 --live 后调用产品真实服务，所有证据写入新建 logs 目录。
使用 --select 只测必要样本；classroom 从同版本已生成画像启动真实 Session。
所有批次通过 --budget-file 共用80次请求上限（包括结构修复），不自动整批重跑。
"""
import argparse
import ast
import asyncio
import contextvars
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, prompts, session as session_module, student_profile
from backend.llm import ModelClient, ModelError
from backend.schemas import Lesson, TeachingSettings, profile_preparation

ROOT = Path(__file__).resolve().parents[1]
CASE_ID = contextvars.ContextVar('profile_case', default='run')


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


def sources():
    result = []
    for path in sorted((ROOT / 'docs/学生反应回归测试').glob('0*.md')):
        text = path.read_text()
        result.append((path.name[:2], TeachingSettings(
            topic=re.search(r'主题：(.*?)。年级：', text)[1],
            grade=re.search(r'年级：(.*?)；', text)[1],
            subject=re.search(r'学科：(.*?)；', text)[1],
            points=re.search(r'知识点填写：(.*?)。', text)[1] + '。')))
    return result


def cases(suite, repeats):
    result = []
    if suite in ('matrix', 'pilot'):
        pilot = {('01', 'weak', 'new'), ('02', 'strong', 'new'),
                 ('03', 'normal', 'new'), ('03', 'normal', 'application'),
                 ('04', 'strong', 'new'), ('04', 'normal', 'review'),
                 ('05', 'normal', 'new'), ('05', 'normal', 'application')}
        for repeat in range(1, repeats + 1):
            for number, setting in sources():
                for level in ('weak', 'normal', 'strong'):
                    for lesson in ('new', 'review', 'application'):
                        if suite == 'pilot' and (number, level, lesson) not in pilot:
                            continue
                        data = {**setting.model_dump(), 'student_level': level, 'lesson_type': lesson}
                        result.append((f'{number}-{level}-{lesson}-r{repeat}', TeachingSettings(**data)))
    if suite in ('grades', 'pilot'):
        settings = [TeachingSettings(topic=s.topic) for _, s in sources()]
        settings += [TeachingSettings(topic='20以内的加减法'),
                     TeachingSettings(topic='认识长方形和正方形'),
                     TeachingSettings(topic='导数的概念与几何意义'),
                     TeachingSettings(topic='定积分与微积分基本定理')]
        settings += [TeachingSettings(topic='一次函数', grade='一年级', subject='数学'),
                     TeachingSettings(topic='欧姆定律', grade='一年级', subject='物理'),
                     TeachingSettings(topic='10以内的加减法', grade='一年级', subject='数学'),
                     TeachingSettings(topic='10以内的加减法', grade='高一', subject='数学'),
                     TeachingSettings(topic='一次函数', grade='小学一年级', subject='数学'),
                     TeachingSettings(topic='比值与单位', grade='成人零基础', subject='数学')]
        if suite == 'pilot':
            settings = [settings[i] for i in (0, 5, 9, 10)]
        for repeat in range(1, repeats + 1):
            result += [(f'grade-{i:02d}-r{repeat}', s) for i, s in enumerate(settings, 1)]
    return result


async def run(args):
    if not config.API_KEY:
        raise RuntimeError('未配置 DeepSeek 密钥')
    if 'deepseek' not in config.BASE_URL or not config.MODEL.startswith('deepseek'):
        raise RuntimeError('此回归仅针对已授权的 DeepSeek 配置')
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=False)
    summary = {'status': 'running', 'started_at': now(), 'model': config.MODEL,
               'base_url': config.BASE_URL, 'temperature': .25, 'suite': args.suite,
               'stage': args.stage, 'repeats': args.repeats, 'requests': 0,
               'responses': 0, 'usage': {}, 'phases': {}, 'cases': [],
               'commit': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
               'prompts_sha256': hashlib.sha256((ROOT / 'backend/prompts.py').read_bytes()).hexdigest()}
    budget_path = ROOT / args.budget_file
    budget_path.parent.mkdir(parents=True, exist_ok=True)
    budget = json.loads(budget_path.read_text()) if budget_path.exists() else {'requests': 0, 'limit': 80, 'runs': []}
    budget['runs'].append(str(output.relative_to(ROOT)))

    def save_budget():
        budget_path.write_text(json.dumps(budget, ensure_ascii=False, indent=2))

    save_budget()
    (output / 'prompts.py.snapshot').write_bytes((ROOT / 'backend/prompts.py').read_bytes())
    summary['implementation_sha256'] = {}
    for name in ('backend/prompts.py', 'backend/student_profile.py', 'backend/llm.py', 'backend/schemas.py',
                 'tests/live_profile_regression.py'):
        content = (ROOT / name).read_bytes()
        summary['implementation_sha256'][name] = hashlib.sha256(content).hexdigest()
        (output / (name.replace('/', '__') + '.snapshot')).write_bytes(content)

    def save():
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    class TrackedModel(ModelClient):
        def __init__(self, emit):
            async def track(kind, data):
                if kind == 'llm_request':
                    if budget.get('blocking_error'):
                        raise ModelError(budget['blocking_error'])
                    if budget['requests'] >= budget['limit']:
                        raise ModelError(f"达到本次修复共享的{budget['limit']}次请求上限（含修复重试）")
                    if summary.get('blocking_error'):
                        raise ModelError(summary['blocking_error'])
                    if summary['requests'] >= args.request_limit:
                        raise ModelError('达到本次真实回归请求上限')
                    summary['requests'] += 1
                    budget['requests'] += 1
                    save_budget()
                    phase = data['phase']
                    summary['phases'][phase] = summary['phases'].get(phase, 0) + 1
                if kind == 'llm_output':
                    summary['responses'] += 1
                    for key, value in data.get('usage', {}).items():
                        if isinstance(value, (int, float)):
                            summary['usage'][key] = summary['usage'].get(key, 0) + value
                with (output / 'events.jsonl').open('a') as stream:
                    stream.write(json.dumps({'case': CASE_ID.get(), 'at': now(),
                                             'event': kind, 'data': data}, ensure_ascii=False) + '\n')
                save()
                await emit(kind, data)
            super().__init__(track)

    student_profile.ModelClient = session_module.ModelClient = TrackedModel
    semaphore = asyncio.Semaphore(args.concurrency)
    previous = {}
    if args.stage == 'profiles' and args.source:
        prior_path = ROOT / args.source
        prior = json.loads((prior_path / 'summary.json').read_text())
        hashes = dict(prior['implementation_sha256'])
        extra = prior_path / 'schema-provenance.json'
        if extra.exists():
            hashes.update(json.loads(extra.read_text()))
        for name in ('backend/prompts.py', 'backend/student_profile.py', 'backend/llm.py', 'backend/schemas.py'):
            if hashes.get(name) != summary['implementation_sha256'][name]:
                raise RuntimeError('续测前产品版本已变化，请新建完整批次：' + name)
        if (prior['model'], prior['base_url']) != (config.MODEL, config.BASE_URL):
            raise RuntimeError('续测前模型配置已变化')
        previous = {c['id']: c for c in prior['cases']}
        summary['resume_source'] = str(prior_path)

    def stop_on_account_error(exc):
        if re.search(r'HTTP (401|402|403)\b', str(exc)):
            summary['blocking_error'] = str(exc)
            budget['blocking_error'] = str(exc)
            save_budget()

    async def profile(key, settings):
        async with semaphore:
            CASE_ID.set(key)
            item = {'id': key, 'settings': settings.model_dump(), 'status': 'running'}
            summary['cases'].append(item)
            save()
            if summary.get('blocking_error'):
                item['status'] = 'not_run'
                item['error'] = summary['blocking_error']
                save()
                return
            try:
                old = previous.get(key, {})
                if old.get('profile'):
                    from backend.schemas import StudentProfileResult
                    result = StudentProfileResult.model_validate(old['profile'])
                    item['profile_reused_from'] = args.source
                else:
                    result = await student_profile.generate_profile(settings)
                item['profile'] = result.model_dump()
                if args.no_preparation:
                    item['status'] = 'completed'
                    print(key, 'completed', result.grade, 'profile only', flush=True)
                    save()
                    return
                lesson = Lesson(**{**settings.model_dump(), **result.model_dump()})
                async def emit(*_): pass
                model = TrackedModel(emit)
                try:
                    prep = await model.generate('preparation', prompts.PREPARE, lesson.model_dump(),
                                                profile_preparation(lesson.student_profile))
                finally:
                    await model.close()
                item['preparation'] = prep.model_dump()
                item['status'] = 'completed'
                print(key, 'completed', result.grade, 'prerequisites', len(prep.initial_prerequisites), flush=True)
            except Exception as exc:
                stop_on_account_error(exc)
                item['status'] = 'failed'
                item['error'] = str(exc)
                print(key, 'FAILED', str(exc), flush=True)
            save()

    async def classroom(source):
        async with semaphore:
            CASE_ID.set(source['id'])
            item = {'id': source['id'], 'settings': source['settings'], 'profile': source['profile'],
                    'status': 'running', 'turns': []}
            summary['cases'].append(item)
            events = []
            async def send(event): events.append(event)
            session = session_module.Session(send, None)
            if args.reuse_preparation:
                # 已验证同版本的画像生成/提取规则；只复测学生Prompt时不重复付费提取。
                class PreparedModel(TrackedModel):
                    async def generate(self, phase, system, payload, output_type, **kwargs):
                        if phase == 'preparation':
                            item['preparation_reused_from'] = args.source
                            return output_type.model_validate(source['preparation'])
                        return await super().generate(phase, system, payload, output_type, **kwargs)
                await session.llm.close()
                session.llm = PreparedModel(session.emit)
            item['session_id'] = session.id
            try:
                await session.start(Lesson(**{**source['settings'], **source['profile']}),
                                    muted=False, tts=False, asr_review=False)
                item['preparation'] = session.preparation.model_dump()
                number = source['id'][:2]
                material = next((ROOT / 'docs/学生反应回归测试').glob(number + '-*.md'))
                text = material.read_text()
                headings = dict(re.findall(r'### ([ABC]\d+)([^\n]*)', text))
                # 每科 A1 为旧知/边界探测；B 整条跳步路径与 C 补讲保持同一个真实课堂。
                blocks = re.findall(r'### ([ABC]\d+)[^\n]*\n\n((?:>[^\n]*\n?)+)', text)
                chosen = [(label, block) for label, block in blocks
                          if label.startswith(('B', 'C'))] if args.path == 'recovery' else blocks[:1]
                if not chosen:
                    raise RuntimeError('未找到测试话术')
                for label, block in chosen:
                    delivered_before = len(session.history)
                    teacher = '\n'.join(line.removeprefix('> ').removeprefix('>') for line in block.splitlines())
                    version = session.state.version
                    await session.add_transcript(teacher)
                    await session.flush()
                    async with asyncio.timeout(115):
                        while session.state.version == version:
                            if session.failed_batch:
                                error = next(e['data'] for e in reversed(events) if e['type'] == 'error')
                                raise RuntimeError(str(error))
                            await asyncio.sleep(.05)
                    state = next(e['data'] for e in reversed(events) if e['type'] == 'state')
                    # 等处理队列结算，再显式触发发言，确保历史包含真实送达的学生回应。
                    await session.teaching_queue.join()
                    await session.try_speak(10)
                    replies = list(session.history[delivered_before:])
                    required_reply = '提问' in headings.get(label, '')
                    if required_reply and not replies:
                        item.setdefault('acceptance_issues', []).append(label + ': 明确提问未送达回复')
                    item['turns'].append({'label': label, 'teacher': teacher, 'result': state,
                                          'required_reply': required_reply, 'delivered_replies': replies,
                                          'delivered_history': list(session.history)})
                    save()
                    print(source['id'], label, state['candidate']['text'], flush=True)
                await session.finish()
                item['status'] = 'completed'
                item['acceptance'] = 'failed' if item.get('acceptance_issues') else 'passed_delivery'
            except Exception as exc:
                stop_on_account_error(exc)
                item['status'] = 'failed'
                item['error'] = str(exc)
                print(source['id'], 'CLASSROOM FAILED', str(exc), flush=True)
            finally:
                await session.close()
                save()

    try:
        if args.stage == 'profiles':
            available = cases(args.suite, args.repeats)
            if args.select != 'all':
                unknown = set(args.select.split(',')) - {key for key, _ in available}
                if unknown:
                    raise ValueError('不存在的样本ID：' + ','.join(sorted(unknown)))
            planned = [(key, setting) for key, setting in available
                       if args.select == 'all' or key in args.select.split(',')]
            summary['planned_cases'] = len(planned)
            summary['skipped_completed'] = [key for key, _ in planned
                                             if previous.get(key, {}).get('status') == 'completed']
            summary['cases'].extend({**previous[key], 'reused_from': args.source}
                                    for key in summary['skipped_completed'])
            await asyncio.gather(*(profile(key, setting) for key, setting in planned
                                   if key not in summary['skipped_completed']))
        else:
            source_data = json.loads((ROOT / args.source / 'summary.json').read_text())
            for name in ('backend/prompts.py', 'backend/student_profile.py', 'backend/llm.py', 'backend/schemas.py'):
                if source_data['implementation_sha256'].get(name) != summary['implementation_sha256'][name]:
                    if name == 'backend/prompts.py':
                        tree = ast.parse((ROOT / args.source / 'prompts.py.snapshot').read_text())
                        prior = {node.targets[0].id: ast.literal_eval(node.value) for node in tree.body
                                 if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)
                                 and isinstance(node.value, ast.Constant)}
                        if all(prior.get(key) == getattr(prompts, key) for key in ('STUDENT_PROFILE', 'PREPARE')):
                            summary['student_prompt_changed_since_profile_generation'] = True
                            continue
                    raise RuntimeError('课堂复测必须使用当前产品版本生成的画像：' + name)
            selected = [c for c in source_data['cases'] if c['status'] == 'completed'
                        and (args.select == 'all' or c['id'] in args.select.split(','))]
            if not selected:
                raise RuntimeError('没有匹配的已生成画像')
            config.ROOT = output
            await asyncio.gather(*(classroom(c) for c in selected))
        summary['status'] = 'completed' if all(c['status'] == 'completed' for c in summary['cases']) else 'incomplete'
    finally:
        summary['finished_at'] = now()
        summary['prompts_sha256_after'] = hashlib.sha256((ROOT / 'backend/prompts.py').read_bytes()).hexdigest()
        save()
        lines = ['# 真实画像回归原始结果', '']
        for item in sorted(summary['cases'], key=lambda c: c['id']):
            lines += ['## ' + item['id'], '', json.dumps(item['settings'], ensure_ascii=False), '',
                      item.get('profile', {}).get('student_profile', item.get('error', '')), '', '### 实际初始知识', '']
            lines += ['- ' + p for p in item.get('preparation', {}).get('initial_prerequisites', [])]
            for turn in item.get('turns', []):
                lines += ['', '### ' + turn['label'], '', '> ' + turn['teacher'], '',
                          turn['result']['candidate']['text']]
            lines += ['']
        (output / 'profiles.md').write_text('\n'.join(lines))
        print('RESULT', output, summary['status'], summary['requests'], summary['usage'], flush=True)
    if summary['status'] != 'completed':
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--stage', choices=('profiles', 'classroom'), default='profiles')
    parser.add_argument('--suite', choices=('pilot', 'matrix', 'grades'), default='pilot')
    parser.add_argument('--repeats', type=int, default=1)
    parser.add_argument('--concurrency', type=int, choices=(1, 2), default=2)
    parser.add_argument('--request-limit', type=int, default=100)
    parser.add_argument('--no-preparation', action='store_true', help='年级/回填专项仅生成画像，省去无关的提取调用')
    parser.add_argument('--reuse-preparation', action='store_true', help='只复测学生Prompt时复用同画像、同提取规则的结果')
    parser.add_argument('--budget-file', required=True, help='本次修复全部批次共享的80次硬上限账本；续测必须复用')
    parser.add_argument('--source', help='课堂用的画像批次；profiles 阶段则从同版本批次续测，仅补未完成项')
    parser.add_argument('--select', default='all')
    parser.add_argument('--path', choices=('boundary', 'recovery'), default='boundary')
    asyncio.run(run(parser.parse_args()))
