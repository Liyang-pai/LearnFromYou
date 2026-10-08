"""生成三组明确标注的模拟课堂报告，不连接任何真实模型或读取私人日志。"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from backend import config
from backend.report_data import build_snapshot
from backend.report_v2 import ReportService, markdown

CASES = {
    '正确讲解': {'sid': '111111111111', 'topic': '自定义蓝盒规则',
        'teacher': '蓝盒规则是输入数字加一。比如输入三，输出四。',
        'knowledge': '蓝盒把输入数字加一。', 'recall': '我现在会按老师的蓝盒规则，把放进去的数字增加一。',
        'question': '有人说蓝盒先放入五，再把输出放回蓝盒，最后仍是六。判断他说得对不对，请说明理由。',
        'answer': '他说得不对。第一次五加一得到六，第二次输入已经变成六，再加一得到七；不能把第二次也当作输入五。',
        'verdict': '有理解证据', 'reasoning': '解释或应用', 'explanation': '在课堂定义的规则内，对新输入给出了结果和理由；不是全面掌握证明。'},
    '错误讲解': {'sid': '222222222222', 'topic': '乘法讲解错误样例',
        'teacher': '今天我们把二乘三的结果记为七。',
        'knowledge': '二乘三等于七。', 'recall': '我记得老师今天把二乘三讲成七，我当前的理解也仍是这个结果。',
        'question': '请说说今天这个算式的结果和你的依据。',
        'answer': '今天我们把二乘三的结果记为七。',
        'verdict': '有理解证据', 'reasoning': '机械复述', 'explanation': '模拟检查员的过强结论将被程序降级；回答只重复课堂错误说法。'},
    '不完整讲解': {'sid': '333333333333', 'topic': '链表删除讲解不完整',
        'teacher': '链表是节点通过引用连接的结构。现在讲到删除，但具体怎样改变引用稍后再讲。',
        'knowledge': '链表节点通过引用连接。', 'recall': '我能说出链表中的节点相互连接，但老师还没讲删除时怎样改变连接。',
        'doubt': '删除中间节点时，为什么要看前一个节点？',
        'question': None, 'answer': None, 'verdict': None,
        'reasoning': None, 'explanation': None},
}


def fixture(name):
    case = CASES[name]; sid = case['sid']; events = []
    def add(kind, data, seconds):
        events.append({'id': f'{sid}:e{len(events)+1:06d}', 'type': kind, 'elapsed': seconds, 'data': data})
    add('ready', {'prerequisites': []}, 0)
    add('transcript', {'id': 't1', 'text': case['teacher'], 'raw_text': case['teacher'], 'mode': 'text', 'question_count': 0}, 10)
    questions = []
    if case.get('doubt'):
        questions = [{'id': 'q1', 'topic': '链表删除', 'text': case['doubt'], 'status': 'asked', 'sources': ['t1'], 'resolution_sources': [], 'attempts': 1}]
        add('reply', {'kind': 'question', 'question_id': 'q1', 'text': case['doubt'], 'sources': ['t1']}, 20)
    state = {'version': 1, 'knowledge': [{'id': 'k1', 'text': case['knowledge'], 'status': 'understood', 'sources': ['t1']}], 'questions': questions}
    add('state', {'state': state, 'processed_sources': ['t1']}, 25)
    add('finished', {'session_id': sid, 'state': state, 'unprocessed_sources': []}, 40)
    return build_snapshot(sid, events, {'topic': case['topic']}), deepcopy(case)


class ExampleModel:
    def __init__(self, case): self.case = case
    async def close(self): pass
    async def generate(self, phase, system, payload, output_type):
        case = self.case
        source = next(s for s in payload['sources'] if s['kind'] == 'teacher')
        citation = {'event_id': source['event_id'], 'quote': source['text']}
        if phase.endswith('recall'):
            doubts = []
            if case.get('doubt'):
                doubts = [{'text': '我还有这个疑问：' + case['doubt'], 'question_ids': ['q1'], 'citations': [citation]}]
            data = {'explained': [{'text': case['recall'], 'knowledge_ids': ['k1'], 'citations': [citation]}],
                'doubts': doubts, 'uncertain': [], 'probes': [] if not case['question'] else [
                    {'id': 'v1', 'knowledge_id': 'k1', 'question': case['question'], 'task_kind': '发现错误',
                     'required_tasks': [case['question']], 'citations': [citation]}]}
        elif phase.endswith('answers'):
            data = {'answers': [{'id': 'v1', 'text': case['answer'], 'citations': [citation]}]}
        elif phase.endswith('verification'):
            data = {'results': [{'id': 'v1', 'status': case['verdict'], 'reasoning': case['reasoning'],
                'explanation': case['explanation'], 'citations': [citation],
                'task_checks': [{'task': case['question'], 'outcome': '有依据地完成', 'answer_quote': case['answer'],
                                'explanation': case['explanation'], 'citations': [citation]}],
                'reasoning_check': {'kind': '新情境推理' if case['reasoning'] == '解释或应用' else '仅换名换数或复述',
                                    'answer_quote': case['answer'], 'explanation': case['explanation'], 'citations': [citation],
                                    'comparison': {'kind':'实质任务变化' if case['reasoning']=='解释或应用' else '仅表面替换',
                                                   'difference':'题目需要组合两次蓝盒处理，课堂只演示一次处理。' if case['reasoning']=='解释或应用' else '作答只是复述课堂原算式，没有新的判断任务。',
                                                   'answer_quote':case['answer'],'citations':[citation],
                                                   'teacher_steps':['计算一次'] if case['reasoning']=='解释或应用' else ['陈述规则'],
                                                   'task_steps':['计算一次','计算一次'] if case['reasoning']=='解释或应用' else ['陈述规则'],
                                                   'task_quote':case['question']}}}]}
        else:
            if case.get('doubt'):
                data = {'strengths': [], 'weaknesses': [{'id': 'f1', 'observation': '教师表示引用改变稍后再讲，学生随后追问删除操作。',
                    'interpretation': '当前课堂还没有足够规则支持学生解释删除过程。', 'citations': [citation,
                        {'event_id': case['sid']+':e000003', 'quote': case['doubt']}]}],
                    'suggestions': [{'finding_id': 'f1', 'priority': '优先', 'action': '下次用 A → B → C 展示删除 B 前后的连接，让学生先指出哪个引用需要改变，再解释理由。这是未来例子，不是本次已讲内容。'}]}
            elif case['sid'].startswith('1'):
                data = {'strengths': [{'id': 'f1', 'observation': '教师给出了蓝盒定义，并举出输入三输出四的例子。',
                    'interpretation': '定义与例子为模拟学生尝试新输入提供了课堂依据。', 'citations': [citation]}], 'weaknesses': [], 'suggestions': []}
            else:
                data = {'strengths': [], 'weaknesses': [], 'suggestions': []}
            # A classroom-relative evaluator cannot independently prove a factual error.
            summaries={'111111111111':'教师给出蓝盒加一规则和一次处理例子，课后模拟作答处理了两次计算；只支持这道任务，不能证明真实掌握。',
                       '222222222222':'教师给出了二乘三等于七的讲法，模拟回答照搬原句；不能以复述证明理解或知识正确。',
                       '333333333333':'教师说明链表通过引用连接，删除的引用改变明确暂缓；学生实际提出的删除疑问尚未解决。'}
            data['summary']={'text':summaries[case['sid']],'citations':[citation]}
        return output_type.model_validate(data)


async def generate_examples(output_dir):
    output_dir.mkdir(parents=True, exist_ok=True)
    results = {}
    for name in CASES:
        snapshot, case = fixture(name)
        service = ReportService(lambda emit: ExampleModel(case))
        service.register(snapshot); service.start(case['sid']); await service.tasks[case['sid']]
        view = service.get(case['sid'])
        view['data_origin'] = '固定模拟课堂及模拟模型输出，不是真实课堂或真实 API 验收'
        results[name] = view
        (output_dir / (name + '.md')).write_text('> **模拟数据：课堂、学生输出和模型结果均由固定测试夹具生成，不是真实课堂或真实 API 测试。**\n\n' + markdown(view), encoding='utf-8')
        (output_dir / (name + '.json')).write_text(json.dumps(view, ensure_ascii=False, indent=2), encoding='utf-8')
    return results


if __name__ == '__main__':
    # Cache only these public fake examples under this worktree's ignored logs.
    output = ROOT / 'docs' / 'report-v2-examples'
    results = asyncio.run(generate_examples(output))
    print(json.dumps({name: {'status': v['status'], 'verification': v['verification']} for name, v in results.items()}, ensure_ascii=False))
