"""显式运行真实模型抽样：画像推断、不同水平、原文摘录、未知规则与教师纠正。

仅使用合成教学数据；最多 24 次请求，结果保存在被 Git 忽略的 logs 下。
无需启动服务或加载 ASR。运行：python tests/live_student_profile.py --live
"""
import argparse
import asyncio
import json
import re
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, prompts, student_profile, session as session_module
from backend.llm import ModelClient, ModelError
from backend.schemas import Lesson, TeachingSettings, profile_preparation


async def run():
    if not config.API_KEY:
        raise RuntimeError('未配置模型密钥，未运行真实模型抽样。')
    root = config.ROOT / 'logs'
    root.mkdir(exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix='student-profile-live-', dir=root))
    summary = {'requests': 0, 'profiles': [], 'classroom': [], 'status': 'running'}

    def save():
        (output / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')

    class TrackedModel(ModelClient):
        def __init__(self, emit):
            async def tracked(kind, data):
                if kind == 'llm_request':
                    if summary['requests'] >= 24:
                        raise ModelError('达到本次验证的 24 次请求上限')
                    summary['requests'] += 1
                await emit(kind, data)
            super().__init__(tracked)

    student_profile.ModelClient = session_module.ModelClient = TrackedModel
    session = None
    try:
        for setting in [TeachingSettings(topic='欧姆定律'),
                        TeachingSettings(topic='欧姆定律', grade='九年级', subject='物理', student_level='weak'),
                        TeachingSettings(topic='欧姆定律', grade='九年级', subject='物理', student_level='strong')]:
            result = await student_profile.generate_profile(setting)
            lesson = Lesson(**{**setting.model_dump(), **result.model_dump()})
            async def emit(*args): pass
            model = TrackedModel(emit)
            try:
                preparation = await model.generate('preparation', prompts.PREPARE, lesson.model_dump(),
                                                   profile_preparation(lesson.student_profile))
            finally:
                await model.close()
            summary['profiles'].append({'settings': setting.model_dump(), 'result': result.model_dump(),
                                        'preparation': preparation.model_dump()})
            save()
            print('PROFILE', setting.student_level, result.grade, result.subject,
                  '前置条目', len(preparation.initial_prerequisites), flush=True)

        # 使用可验证的新造规则，避免将模型预训练知识误当成课堂学习效果。
        config.ROOT = output
        events = []
        async def send(event): events.append(event)
        # 同时检查实际生成的画像，不只检查手写测试画像的边界。
        generated = summary['profiles'][1]
        session = session_module.Session(send, None)
        await session.start(Lesson(**{**generated['settings'], **generated['result']}),
                            muted=True, tts=False, asr_review=False)
        await session.add_transcript('老师还没有开始讲欧姆定律。请回答：你能给出欧姆定律的公式吗？')
        await session.flush()
        async with asyncio.timeout(110):
            while session.state.version == 0:
                if session.failed_batch:
                    raise RuntimeError('生成画像的课堂边界验证调用失败')
                await asyncio.sleep(.05)
        answer = next(e['data'] for e in reversed(events) if e['type'] == 'state')
        summary['generated_profile_boundary'] = answer
        assert not re.search(r'I\s*=\s*U\s*/\s*R|U\s*=\s*I\s*[*×]?\s*R|电压除以电阻', answer['candidate']['text'], re.I), '生成画像泄漏待讲公式'
        await session.finish()
        print('GENERATED_BOUNDARY', answer['candidate']['text'], flush=True)
        events = []
        session = session_module.Session(send, None)
        lesson = Lesson(topic='星星运算', points='星星运算的规则与计算',
            student_profile='已有知识：知道整数加法和减法。\n理解基础：具备日常语言和基本逻辑能力。\n知识边界：尚未学习星星运算，不知道定义或规则。\n可能的理解困难：需要完整规则才能计算。')
        await session.start(lesson, muted=True, tts=False, asr_review=False)
        async def teach(text):
            version = session.state.version
            await session.add_transcript(text); await session.flush()
            async with asyncio.timeout(110):
                while session.state.version == version:
                    if session.failed_batch:
                        error = next(e['data']['message'] for e in reversed(events) if e['type'] == 'error')
                        raise RuntimeError(error)
                    await asyncio.sleep(.05)
            result = next(e['data'] for e in reversed(events) if e['type'] == 'state')
            summary['classroom'].append({'teacher': text, 'result': result})
            save()
            print('CLASSROOM', result['state']['version'], result['candidate']['text'], flush=True)
            return result

        unknown = await teach('我还没讲过星星运算。请回答：你知道它的完整规则吗？')
        assert all(all(s.startswith('p') for s in k['sources']) for k in unknown['state']['knowledge']
                   if k['status'] in ('tentative','understood')), '未知规则被提前写入知识'
        await teach('先暂时规定：两个整数做星星运算，就是将两个数相加。请计算3星星3。')
        incomplete = await teach('纠正刚才的说法：仅相加的规则不完整，完整规则稍后讲。现在你能给出完整规则吗？')
        assert not any('减' in k['text'] and any(s.startswith('t') for s in k['sources'])
                       and k['status'] in ('tentative','understood') for k in incomplete['state']['knowledge']), '提前猜测纠正规则'
        corrected = await teach('完整规则是：两个数相加后再减1。它取代之前只相加的规则。请回答3星星3是多少，并说明理由。')
        assert '5' in corrected['candidate']['text'] or '五' in corrected['candidate']['text'], '未依据完整规则回答'
        assert corrected['state']['knowledge_history'], '纠正未保存知识修订'
        await session.finish()
        summary['status'] = 'passed'
    except BaseException as exc:
        summary['status'] = 'failed'
        summary['error'] = str(exc)
        raise
    finally:
        if session: await session.close()
        save()
        print('RESULT', output / 'summary.json', 'requests', summary['requests'], summary['status'], flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True, help='确认调用配置好的真实模型')
    parser.parse_args()
    asyncio.run(run())
