"""显式使用真实模型验收八组五状态课堂；共享 48 次请求预算，结果只写入 logs。"""
import argparse
import asyncio
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, session as session_module
from backend.llm import ModelClient, ModelError
from backend.schemas import Lesson

ARITHMETIC = '已有知识：知道整数加法和减法。知道整数乘法和除法。理解表格中数字的对应。\n理解基础：能依据已知规则逐步计算。\n知识边界：本课新规则尚未学习。'
BEGINNER = '已有知识：知道整数加法和减法。理解表格中数字的对应。\n理解困难：没有学过字母表示数、变量、坐标、函数和图像。'
BOX = '已有知识：当前红盒规则中，输入一个整数，输出是输入数加1。知道整数加法和减法。\n知识边界：尚未学习其他盒子规则。'
CASES = {
    'advanced': (BEGINNER, ['一次函数写成y=kx+b，k是斜率，图像与y轴的交点是(0,b)，下面直接用斜率判断图像增减。']),
    'complete': (ARITHMETIC, ['朵运算是把两个整数相加。例如2朵3，就是2加3，结果5。规则只有这一步。']),
    'skipped': (ARITHMETIC, ['现在定义花数为一个整数的3倍再加2。求花数为14时的原数，我略去中间变形和计算，直接得到4。']),
    'profile_conflict': (BOX, ['我们仍讨论当前红盒，同样输入整数，输出应该是输入数加2。']),
    'conditions': (BOX, ['我们仍讨论当前红盒，同样输入整数，输出应该是输入数加2。',
        '把适用范围说明清楚：刚才加2讲的是蓝盒，红盒仍然加1。两种规则分别用于不同颜色的盒子。']),
    'correction': (ARITHMETIC, ['朵运算是将两个整数相加，3朵3就是6。',
        '刚才朵运算只相加的规则讲错了，我明确撤回它。正确规则下一段再讲。',
        '正确的朵运算是两数相加后再减1，先相加，再减去1。这取代原来的只相加规则。请回答3朵3是多少。']),
    'multiple_routes': (BEGINNER, ['一次函数y=2x+1的图像是一条直线，通过斜率和截距就能读图。',
        '先不谈字母和图像。看数字对应表：输入0输出1，输入1输出3，输入2输出5。规则是把输入数乘2再加1，也可每当输入增加1，输出增加2。看相邻两行：1到3增加2，3到5增加2。这里只学这个表格的变化关系，不学如何读图。']),
    'dependencies': (ARITHMETIC, ['盒规则是把输入数乘3再加2。要使输出为14，我跳过中间计算，暂时推得输入是4。',
        '沿用刚才尚待确认的输入推断，把这个输入数再加1，结果就是5；这个结论只根据前面的输入推断得到。',
        '刚才盒规则乘3再加2讲错了，明确撤回它，以及由这条规则推出的输入为4。正确规则暂时不讲；由那个输入推断出的后续结果需要重新考虑。'])
}


def learned(state):
    return [k for k in state.knowledge if not k.id.startswith('p')]


def check(case, snapshots, replies):
    end = snapshots[-1]
    for state in snapshots:
        assert all(k.reason and k.sources for k in state.knowledge), '知识缺少依据或来源'
        assert all(k.status in {'understood','tentative','unclear','conflict','invalid'} for k in state.knowledge)
    if case == 'advanced':
        assert any(k.status == 'unclear' for k in learned(end)), '超前内容没有记录理解障碍'
    elif case == 'complete':
        assert any(k.status == 'understood' and '相加' in k.text for k in learned(end)), '完整定义没有形成理解'
    elif case == 'skipped':
        assert any(k.status == 'tentative' and '4' in k.text for k in learned(end)), '可补跳步没有待确认推断'
    elif case == 'profile_conflict':
        assert any(c.status == 'open' and (c.left_id.startswith('p') or c.right_id.startswith('p')) for c in end.conflicts), '未关联画像矛盾'
        assert not end.learning_context()['knowledge'] or all('红盒' not in k['text'] for k in end.learning_context()['knowledge']), '冲突旧规则仍作事实'
    elif case == 'conditions':
        assert any(c.status == 'open' for c in snapshots[0].conflicts)
        assert end.conflicts and all(c.status == 'resolved' for c in end.conflicts), '条件澄清后冲突未解除'
        assert any(k.status == 'understood' and '红盒' in k.text for k in end.knowledge), '未保留红盒规则'
        assert any(k.status == 'understood' and ('蓝盒' in k.text or '蓝盒' in k.conditions) for k in end.knowledge), '未理解蓝盒规则'
    elif case == 'correction':
        assert any(k.status == 'invalid' for k in snapshots[1].knowledge), '教师撤回未产生 invalid'
        assert not any(k.status in ('understood','tentative') and '减1' in k.text for k in learned(snapshots[1])), '提前猜补正确规则'
        assert any(k.status == 'understood' and ('减1' in k.text or '减去1' in k.text) for k in learned(end)), '未理解更正规则'
        assert any('5' in text or '五' in text for text in replies), '纠正后答案未按新规则计算'
    elif case == 'multiple_routes':
        assert any(k.status == 'unclear' for k in learned(snapshots[0]))
        assert any(k.status == 'understood' and ('增加2' in k.text or '增加 2' in k.text) for k in learned(end)), '表格路径未建立局部理解'
        assert any(k.status == 'unclear' and ('图' in k.text or '图' in k.reason) for k in learned(end)), '把表格理解当成会读图'
    elif case == 'dependencies':
        assert any(k.status == 'tentative' and k.dependencies for k in learned(snapshots[1])), '没有记录并传递待确认依赖'
        assert any(k.status == 'invalid' for k in learned(end)), '撤回前提未失效'
        assert not end.pending_reviews, '自动依赖复核未完成'
        assert any(k.status == 'unclear' and k.dependencies for k in learned(end)), '前提失效后未保留推导障碍'


async def run(args):
    if not config.API_KEY:
        raise RuntimeError('未配置模型密钥，未执行真实验收')
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    summary_path = output / 'summary.json'
    summary = json.loads(summary_path.read_text()) if summary_path.exists() else {'requests': 0, 'attempts': []}
    def save(): summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    class Tracked(ModelClient):
        def __init__(self, emit):
            async def tracked(kind, data):
                if kind == 'llm_request':
                    if summary['requests'] >= 48: raise ModelError('达到共享 48 次请求上限')
                    summary['requests'] += 1; save()
                await emit(kind, data)
            super().__init__(tracked)
    session_module.ModelClient = Tracked
    config.ROOT = output
    for name in args.cases or CASES:
        if summary['requests'] >= 48: break
        entry = {'case': name, 'status': 'running', 'snapshots': []}
        summary['attempts'].append(entry); save()
        events = []
        async def send(event): events.append(event)
        session = session_module.Session(send, None)
        try:
            profile, turns = CASES[name]
            await session.start(Lesson(topic='数学规则与关系', student_profile=profile), muted=True, tts=False, asr_review=False)
            states, replies = [], []
            for text in turns:
                previous = len([e for e in events if e['type'] == 'state'])
                await session.add_transcript(text); await session.flush()
                async with asyncio.timeout(240):
                    while True:
                        if session.failed_batch:
                            raise RuntimeError(next(e['data']['message'] for e in reversed(events) if e['type'] == 'error'))
                        if any(r.status == 'failed' for r in session.state.pending_reviews):
                            raise RuntimeError('复核失败：' + session.state.pending_reviews[0].error)
                        states_events = [e for e in events if e['type'] == 'state']
                        if len(states_events) > previous and not session.processing and not session.knowledge_review_busy and not session.state.pending_reviews:
                            break
                        await asyncio.sleep(.05)
                states.append(session.state.model_copy(deep=True))
                replies.append(states_events[-1]['data']['candidate']['text'])
                entry['snapshots'].append({'teacher': text, 'state': session.state.model_dump(), 'candidate': states_events[-1]['data']['candidate']})
                save()
            check(name, states, replies)
            entry['status'] = 'passed'
        except Exception as exc:
            entry['status'], entry['error'] = 'failed', str(exc)
        finally:
            await session.close()
            entry['session_id'] = session.id
            save()
        print(name,entry['status'],entry.get('error',''), 'requests',summary['requests'],flush=True)
    print('SUMMARY',summary_path,flush=True)
    return 1 if any(next(a for a in reversed(summary['attempts']) if a['case'] == name)['status'] != 'passed'
                    for name in {a['case'] for a in summary['attempts']}) else 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--output', default='logs/knowledge-machine-live-20261014')
    parser.add_argument('--cases', nargs='+', choices=list(CASES))
    sys.exit(asyncio.run(run(parser.parse_args())))
