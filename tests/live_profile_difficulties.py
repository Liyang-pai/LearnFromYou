"""显式API课堂对照：仅改变画像的理解困难段，比较跳步、补讲和迁移表现。

使用用户给出的三角矩阵画像；2个完整课堂、2个独立首次提问复测。
共用同一组前置知识，保存真实Session状态、实际送达、请求与用量；最多12次请求。
运行：.venv/bin/python tests/live_profile_difficulties.py --live --output logs/新目录
定向回归：加 --focused --cached-preparation logs/profile-difficulties-ab-20261017/summary.json
只执行困难组三轮，复用已验证前置，默认且最多6次请求（包含重试）。
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config, prompts, session as session_module
from backend.llm import ModelClient, ModelError
from backend.schemas import Lesson, profile_preparation


KNOWN = [
    '能说明一维数组在内存中按连续单元存放，并会用下标访问元素。',
    '能说明二维数组按行优先顺序存放时，元素位置与行号、列号之间的对应关系。',
    '能对n阶方阵数出元素总数，并区分主对角线及其上下两侧的元素。',
    '能用求和公式计算1+2+…+n这类等差数列的结果。',
    '能根据给定下标判断两个元素在矩阵中的相对位置关系。',
    '能读懂用循环遍历二维数组下标的简单程序段。',
]
BASE = ('已有知识：\n' + '\n'.join(KNOWN) + '\n'
        '理解基础：本课面向大二信息科技类课程，学生处于normal水平，常规的数组下标与行优先存储操作可独立完成，但建立新的存储映射关系时需要示例支持。本课为新课，任务以观察三角矩阵元素分布、比较其与完整二维数组存储的差异、建立压缩存储映射为主，目标内容尚未学习。\n'
        '知识边界：三角矩阵的概念与分类、三角矩阵压缩存储的映射思路、下三角矩阵的行优先地址映射、上三角矩阵的行优先地址映射、三角矩阵压缩存储的地址计算与访问验证均属待学习内容，尚未学习。\n'
        '可能的理解困难：')
DIFFICULTIES = ('由二维下标换算到一维存储位置时，容易漏算或错算对角线及边界元素所占的位置，需要借助具体小阶矩阵逐格核对。\n'
                '区分上三角与下三角两种情形时，容易把两种映射的行列处理方式混用，需要在对比中明确各自适用条件。\n'
                '从映射公式反推下标、验证访问是否正确时，缺少系统的检验步骤，需要示例引导其逐项代入核对。')
SETTINGS = dict(topic='三角矩阵的压缩存储', grade='大二', subject='信息科技',
                student_level='normal', lesson_type='new',
                points='三角矩阵的概念与分类；三角矩阵压缩存储的映射思路；下三角矩阵的行优先地址映射；上三角矩阵的行优先地址映射；三角矩阵压缩存储的地址计算与访问验证')
TURNS = [
    ('jump', '本节先讨论下三角矩阵按行压缩存储，保留主对角线。矩阵行号、列号和一维数组下标全部从1开始。对合法位置(i,j)，先给出映射公式k=i(i-1)/2+j，暂不展开推导。请计算(4,2)的一维下标，并说明公式的两部分分别在数什么；如果解释不清，请说具体卡在哪里。'),
    ('explain', '现在补充解释：下三角矩阵第r行保留列号1到r，共r个元素，包含该行主对角线元素。第i行前面有1到i-1行，元素总数为1+2+…+(i-1)=i(i-1)/2；到当前第j列再数j个，所以k是两者相加。第4行之前共6个元素，第2列对应第8个；第4行对角线对应第10个。请独立算(5,3)和(5,5)的一维下标，并解释对角线有没有被计入。'),
    ('transfer', '现在不再给公式。仍按刚才的规则存储，请计算(6,1)和(6,6)的一维下标；说明两个位置相差几个下标单位、第6行实际保存几个元素，并判断(3,4)是否属于本节定义的保留区域。'),
]


def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat()


async def run(args):
    limit = args.request_limit if args.request_limit is not None else (6 if args.focused else 12)
    if limit < 1 or (args.focused and limit > 6):
        raise ValueError('定向回归请求上限必须为1到6')
    if args.focused and not args.cached_preparation:
        raise ValueError('定向回归必须复用前置，不追加准备请求')
    control_lesson = Lesson(**SETTINGS, student_profile=BASE + '未指定。')
    shared = None
    if args.cached_preparation:
        cached = json.loads((ROOT / args.cached_preparation).read_text())
        shared = profile_preparation(control_lesson.student_profile).model_validate(cached['shared_preparation']).model_dump()
        if Counter(shared['initial_prerequisites']) != Counter(KNOWN):
            raise ValueError('缓存与六条原始已知不一致')
    if not config.API_KEY:
        raise RuntimeError('未配置模型密钥')
    if 'deepseek' not in config.BASE_URL or not config.MODEL.startswith('deepseek'):
        raise RuntimeError('仅使用项目已授权的DeepSeek配置')
    output = ROOT / args.output
    output.mkdir(parents=True, exist_ok=False)
    summary = dict(status='running', started_at=now(), model=config.MODEL, temperature=.25,
                   request_limit=limit, requests=0, responses=0, usage={}, phases={}, cases=[],
                   commit=subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
                   design=('困难组三轮字段兼容定向回归；复用六条前置；不重跑A/B' if args.focused else
                           '仅改变困难段；共享前置；完整对照各3轮；独立首次探测各1轮；不把一次对照视作稳定因果证明'),
                   cached_preparation=args.cached_preparation)
    for name in ('backend/prompts.py', 'backend/session.py', 'backend/schemas.py', 'backend/policy.py',
                 'backend/cognition.py', 'backend/llm.py', 'tests/live_profile_difficulties.py'):
        data = (ROOT / name).read_bytes()
        summary.setdefault('file_sha256', {})[name] = hashlib.sha256(data).hexdigest()
        (output / (name.replace('/', '__') + '.snapshot')).write_bytes(data)
    active_case = 'preparation'

    def save():
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2))

    class TrackedModel(ModelClient):
        def __init__(self, emit):
            async def track(kind, data):
                if kind == 'llm_request':
                    if summary.get('blocking_error'):
                        raise ModelError(summary['blocking_error'])
                    if summary['requests'] >= summary['request_limit']:
                        raise ModelError(f'达到本轮{limit}次请求上限，停止调用')
                    summary['requests'] += 1
                    phase = data['phase']
                    summary['phases'][phase] = summary['phases'].get(phase, 0) + 1
                if kind == 'llm_output':
                    summary['responses'] += 1
                    for key, value in data.get('usage', {}).items():
                        if isinstance(value, (int, float)):
                            summary['usage'][key] = summary['usage'].get(key, 0) + value
                with (output / 'events.jsonl').open('a') as stream:
                    stream.write(json.dumps(dict(case=active_case, at=now(), event=kind, data=data), ensure_ascii=False) + '\n')
                save()
                await emit(kind, data)
            super().__init__(track)

    async def noop(*_):
        pass

    if shared is None:
        model = TrackedModel(noop)
        try:
            prep = await model.generate('preparation', prompts.PREPARE, control_lesson.model_dump(),
                                        profile_preparation(control_lesson.student_profile))
            summary['api_preparation'] = prep.model_dump()
            if Counter(prep.initial_prerequisites) != Counter(KNOWN):
                raise RuntimeError('前置提取与六条原始已知不一致，停止以免产生条件混淆')
            shared = prep.model_dump()
        finally:
            await model.close()
    summary['shared_preparation'] = shared
    save()

    class SharedPreparationModel(TrackedModel):
        async def generate(self, phase, system, payload, output_type, **kwargs):
            if phase == 'preparation':
                return output_type.model_validate(shared)
            return await super().generate(phase, system, payload, output_type, **kwargs)

    session_module.ModelClient = SharedPreparationModel
    config.ROOT = output

    async def classroom(condition, repetition, full):
        nonlocal active_case
        active_case = f'{condition}-r{repetition}'
        events = []

        async def send(event):
            events.append(event)

        item = dict(id=active_case, condition=condition, repetition=repetition, status='running', turns=[])
        summary['cases'].append(item)
        session = session_module.Session(send, None)
        item['session_id'] = session.id
        lesson = Lesson(**SETTINGS, student_profile=BASE + (DIFFICULTIES if condition == 'difficulty' else '未指定。'))
        item['lesson'] = lesson.model_dump()
        try:
            await session.start(lesson, muted=False, tts=False, asr_review=False)
            item['initial_state'] = session.state.model_dump()
            item['initial_sources'] = dict(session.sources)
            assert [p['text'] for p in session.prerequisites] == shared['initial_prerequisites']
            for label, teacher in (TURNS if full else TURNS[:1]):
                before = len(session.history)
                event_start, request_start = len(events), summary['requests']
                version = session.state.version
                await session.add_transcript(teacher)
                await session.flush()
                async with asyncio.timeout(110):
                    while session.state.version == version:
                        if session.failed_batch:
                            raise ModelError(str(next(e['data'] for e in reversed(events) if e['type'] == 'error')))
                        await asyncio.sleep(.05)
                    while not session.teaching_queue.empty() or session.processing:
                        if session.failed_batch:
                            raise ModelError('课堂处理失败')
                        await asyncio.sleep(.05)
                await session.try_speak(10)
                event = next(e['data'] for e in reversed(events) if e['type'] == 'state')
                replies = list(session.history[before:])
                turn = dict(label=label, teacher=teacher, result=event, delivered_replies=replies,
                            state_after_delivery=session.state.model_dump(),
                            requests=summary['requests'] - request_start,
                            validation_errors=[e['data'] for e in events[event_start:] if e['type'] == 'student_validation_error'],
                            recovery=[e['data'] for e in events[event_start:] if e['type'] == 'student_recovery'],
                            delivery_events=[e['data'] for e in events[event_start:] if e['type'] == 'reply'])
                item['turns'].append(turn)
                save()
                print(active_case, label, 'DELIVERED', json.dumps(replies, ensure_ascii=False), flush=True)
                if not replies:
                    item.setdefault('delivery_issues', []).append(label)
            await session.finish()
            item['final_state'] = session.state.model_dump()
            item['status'] = 'completed'
        except BaseException as exc:
            item['status'] = 'failed'
            item['error'] = str(exc)
            if re.search(r'HTTP (401|402|403)\b', str(exc)):
                summary['blocking_error'] = str(exc)
            raise
        finally:
            await session.close()
            save()

    try:
        # 顺序反转的独立首次探测，避免全部难度组总处于同一执行顺序。
        cases = ([('difficulty', 1, True)] if args.focused else
                 [('control', 1, True), ('difficulty', 1, True), ('difficulty', 2, False), ('control', 2, False)])
        for condition, repetition, full in cases:
            await classroom(condition, repetition, full)
        summary['status'] = 'completed_semantic_review_required'
    except BaseException as exc:
        summary['status'] = 'incomplete'
        summary['error'] = str(exc)
        raise
    finally:
        summary['finished_at'] = now()
        save()
        lines = ['# 理解困难画像真实课堂对照', '', '以下为实际送达，不把候选或模型自评冒充表现。', '']
        for case in summary['cases']:
            lines.extend(['## ' + case['id'], ''])
            for turn in case['turns']:
                lines.extend(['### ' + turn['label'], '', '> ' + turn['teacher'], ''])
                lines.extend(r['text'] for r in turn['delivered_replies'])
                lines.extend(['', '知识状态：' + json.dumps(
                    [dict(id=k['id'], text=k['text'], status=k['status']) for k in turn['state_after_delivery']['knowledge']], ensure_ascii=False), ''])
        (output / 'replies.md').write_text('\n'.join(lines))
        print('RESULT', output, summary['status'], summary['requests'], summary['usage'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', required=True)
    parser.add_argument('--focused', action='store_true')
    parser.add_argument('--cached-preparation')
    parser.add_argument('--request-limit', type=int)
    asyncio.run(run(parser.parse_args()))
