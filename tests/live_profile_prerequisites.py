"""显式 API 抽样：画像的跨学科前置覆盖、原文提取和课堂冲突。"""
import argparse
import asyncio
import json
from pathlib import Path
import subprocess
import sys
from datetime import datetime

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, prompts
from backend.llm import ModelClient, ModelError
from backend.schemas import TeachingSettings, Lesson, StudentState, StudentResult, StudentProfileResult, profile_preparation
from backend.cognition import seed_prerequisites
from backend.policy import apply_result, student_result_validator


async def main(resume=None):
    out = config.ROOT / 'logs' / ('profile-prerequisites-' + datetime.now().strftime('%Y%m%d-%H%M%S'))
    if resume:
        out = Path(resume)
    out.mkdir(parents=True, exist_ok=True)
    summary = {'model': config.MODEL, 'requests': 0, 'usage': {}, 'profiles': [], 'classrooms': [], 'errors': []}
    if resume:
        summary = json.loads((out / 'summary.json').read_text())
    lock = asyncio.Semaphore(2)

    def save():
        (out / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    async def emit(kind, data):
        if kind == 'llm_request':
            if summary['requests'] >= 32:
                raise ModelError('本次抽样最多32次请求')
            summary['requests'] += 1
        if kind == 'llm_output':
            for key in ('prompt_tokens', 'completion_tokens', 'total_tokens'):
                summary['usage'][key] = summary['usage'].get(key, 0) + data.get('usage', {}).get(key, 0)
        with (out / 'events.jsonl').open('a') as f:
            f.write(json.dumps({'type': kind, 'data': data}, ensure_ascii=False) + '\n')
        save()

    triangle = dict(topic='数据结构-三角矩阵的压缩', grade='大学一年级', subject='数据结构',
                    points='三角矩阵的压缩存储定义；下三角矩阵与上三角矩阵的压缩存储方式；对称矩阵的压缩存储；压缩存储的地址计算；压缩存储与常规二维数组存储的对比。')
    # 固定修改前版本，避免后续提交改变对照组。
    old_source = subprocess.check_output(['git', 'show', '8e26e9630e6537b73ce4b2c8d1dbbd4f200f382d:backend/prompts.py'], text=True)
    namespace = {}
    exec(compile(old_source, '<baseline-prompts>', 'exec'), namespace)
    cases = [('triangle_before', triangle, namespace['STUDENT_PROFILE']),
             ('triangle_normal_1', triangle, prompts.STUDENT_PROFILE),
             ('triangle_normal_2', triangle, prompts.STUDENT_PROFILE),
             ('triangle_weak', {**triangle, 'student_level': 'weak'}, prompts.STUDENT_PROFILE),
             ('triangle_strong', {**triangle, 'student_level': 'strong'}, prompts.STUDENT_PROFILE),
             ('ohm_normal', dict(topic='欧姆定律', grade='九年级', subject='物理', points='电流与电压、电阻的关系；欧姆定律的公式及简单计算'), prompts.STUDENT_PROFILE),
             ('linear_normal', dict(topic='一次函数的图像与性质', grade='八年级', subject='数学', points='一次函数图像的形状；系数对图像的影响；增减性'), prompts.STUDENT_PROFILE)]

    async def profile_case(name, fields, system):
        async with lock:
            model = ModelClient(emit)
            try:
                settings = TeachingSettings(**fields)
                profile = await model.generate('student_profile', system, settings.model_dump(), StudentProfileResult)
                lesson = Lesson(**{**settings.model_dump(), **profile.model_dump()})
                prep = await model.generate('preparation', prompts.PREPARE, lesson.model_dump(), profile_preparation(lesson.student_profile))
                record = {'name': name, 'lesson': lesson.model_dump(), 'preparation': prep.model_dump()}
                summary['profiles'].append(record)
                save()
                print('PROFILE', name, 'chars', len(profile.student_profile), 'prerequisites', len(prep.initial_prerequisites), 'sum', any('等差' in p or '数列' in p for p in prep.initial_prerequisites), flush=True)
                return record
            except Exception as exc:
                summary['errors'].append({'case': name, 'error': str(exc)})
                save()
                print('ERROR', name, str(exc), flush=True)
            finally:
                await model.close()

    if resume:
        by_name = {r['name']: r for r in summary['profiles']}
        results = [by_name.get(case[0]) for case in cases]
    else:
        results = await asyncio.gather(*(profile_case(*case) for case in cases))

    async def classroom(record, teacher_turns, label=None):
        if record is None:
            return
        label = label or record['name']
        if any(r.get('test', r['profile']) == label for r in summary['classrooms']):
            return
        lesson = Lesson(**record['lesson'])
        state = StudentState()
        sources = {f'p{i}': {'id': f'p{i}', 'text': text, 'kind': 'prerequisite'}
                   for i, text in enumerate(record['preparation']['initial_prerequisites'], 1)}
        seed_prerequisites(state, list(sources.values()))
        system = prompts.STUDENT + '\n原始确认画像（初始记录，知识修订以 current_state 为准）：\n' + json.dumps({'student_profile': lesson.student_profile, 'teaching_scope_only': {'topic': lesson.topic, 'points': lesson.points}}, ensure_ascii=False)
        record_out = {'test': label, 'profile': record['name'], 'initial_state': state.model_dump(), 'turns': []}
        summary['classrooms'].append(record_out)
        history, utterances = [], []
        model = ModelClient(emit)
        try:
            for revision, text in enumerate(teacher_turns, 1):
                sid = f't{revision}'
                source = {'id': sid, 'text': text, 'mode': 'text', 'revision': revision, 'at': revision}
                sources[sid] = source
                context = state.learning_context()
                payload = {'interaction_context': {'muted': False, 'trigger': 'teacher_input', 'input_revision': revision},
                           'lesson_scope': record['preparation']['scope'], 'student_level': lesson.student_level,
                           'explicit_prerequisites': [k for k in context['knowledge'] if k['id'].startswith('p')],
                           'current_state': context, 'new_teacher_segments': [source],
                           'teacher_sources': [v for k, v in sources.items() if k.startswith('t')],
                           'previous_student_utterances_not_knowledge_sources': utterances[-8:]}
                recovery = []
                result = await model.generate('student', system, payload, StudentResult, history=history,
                    result_validator=student_result_validator(state, sources, {sid}, recovery, input_revision=revision, require_response=True))
                state, candidate = apply_result(state, result, sources, {sid}, input_revision=revision)
                history.append({'role': 'user', 'content': json.dumps({'teacher_segments': [source]}, ensure_ascii=False)})
                if candidate.kind != 'silence':
                    history.append({'role': 'assistant', 'content': candidate.text})
                    utterances.append({'text': candidate.text, 'kind': candidate.kind, 'sources': candidate.sources})
                    # 模拟实际发出：使用生产代码登记教师请求与问题生命周期。
                    from backend.policy import mark_delivered
                    mark_delivered(state, candidate)
                record_out['turns'].append({'teacher': text, 'result': result.model_dump(), 'candidate': candidate.model_dump(), 'state': state.model_dump(), 'recovery': recovery})
                save()
                print('CLASSROOM', record['name'], revision, candidate.kind, candidate.text, 'conflicts', len([c for c in state.conflicts if c.status == 'open']), flush=True)
        except Exception as exc:
            summary['errors'].append({'case': record['name'] + '_classroom', 'error': str(exc)})
            save()
            print('ERROR classroom', str(exc), flush=True)
        finally:
            await model.close()

    await classroom(results[1], [
        '先不讲矩阵压缩。请用你原来学过的等差数列求和知识，计算1加2一直加到10，写出你使用的公式。',
        '我还没有讲过三角矩阵的压缩映射规则。请直接给出下三角矩阵按行压缩时元素a[i][j]对应的一维数组下标公式。',
        '这里说的仍是普通等差数列，n是项数，a1是首项，an是末项。我给出一个说法：前n项和等于n乘以首项与末项的和，不需要除以2。你觉得这与你原来学过的知识一致吗？',
        '我刚才把求和公式讲错了，明确撤回不除以2的说法，改成前n项和等于n乘以首项与末项的和再除以2。你现在怎么看刚才的矛盾？'])
    await classroom(results[5], ['老师还没有讲欧姆定律。请直接给出电流、电压和电阻之间的公式，并计算电压6伏、电阻3欧时的电流。'])
    await classroom(results[3], ['还没开始讲压缩存储，请直接回答：n阶对称矩阵只存一半元素时，一维数组要存多少个元素？'], 'weak_scope_leak')
    await classroom(results[4], ['还没开始讲压缩存储，请回答：三角矩阵压缩存储能把空间复杂度从O(n^2)降低到更低的数量级吗？说明理由。'], 'strong_complexity')
    await classroom(results[6], ['我还没讲一次函数的性质。请回答一般的一次函数里，k和b分别怎样影响增减性？'], 'linear_scope_leak')
    await classroom(results[5], ['先不谈电学，请回答你以前学过的数学知识：正比例和反比例关系分别是什么意思？'], 'ohm_math_prerequisite')
    await classroom(results[5], ['先不谈电学，请回答你以前学过的数学知识：正比例和反比例关系分别是什么意思？', '下面仍是普通数学里的正比例关系，没有换定义。我给出一个说法：两个量成正比例时，它们的乘积一定，而不是比值一定。请判断是否与你原来学过的知识一致。'], 'ohm_implicit_conflict')
    save()
    print('RESULT', out, 'requests', summary['requests'], 'usage', summary['usage'], flush=True)
    if summary['errors']:
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', required=True, action='store_true')
    parser.add_argument('--resume', type=Path, help='继续已有抽样并共用32次请求上限')
    args = parser.parse_args()
    asyncio.run(main(args.resume))
