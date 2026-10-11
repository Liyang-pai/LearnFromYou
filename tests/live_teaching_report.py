"""Bounded real-model acceptance. Requires --live, private output, and an env file.

Never writes classroom production logs or prints credentials. Existing results
are reused. The request cap counts structural correction requests as well.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import dotenv_values
from backend import config
from backend.llm import ModelClient, ModelError
from backend.teaching_report import ReportService, export_markdown

CASES = [
    ('正确讲解', ['列表和元组都可以按顺序存储多个元素。列表用方括号，元组用圆括号。',
                 '列表可以增删或替换元素；元组创建后不能改变元素引用。',
                 '例如a=[1,2]可以令a[0]=3；b=(1,2)不能令b[0]=3。判断能否赋值时，先看容器类型。']),
    ('明确概念错误', ['今天介绍列表与元组。列表用方括号，元组用圆括号。',
                    '元组和列表一样，可以直接替换任意元素。比如b=(1,2)，执行b[0]=3就能得到(3,2)。']),
    ('合理延期', ['今天只讲列表与元组的基本差别：两者都有顺序，列表可修改，元组的元素引用不可改。',
                 'a=[1,2]可以令a[0]=3；b=(1,2)不能这样赋值。元组内包含列表的进阶情形留到下节，今天不展开。']),
    ('先错后纠正', ['元组创建后也能直接替换元素。',
                   '纠正刚才那句话：元组创建后不能直接替换元素引用。b=(1,2)，b[0]=3会报错；列表才允许这样赋值。',
                   '总结：判断能否替换元素先区分容器类型，列表允许，元组不允许。']),
    ('教师自问自答', ['为什么元组不支持b[0]=3？因为创建后元素引用不能改变。',
                     '那列表呢？列表支持替换元素，a=[1,2]可以把第一个元素改成3。刚才都是老师自己设问并回答。']),
    ('缺少互动材料', ['列表和元组都按顺序存储元素，可以用下标读取。',
                    '列表能修改元素引用，元组不能直接替换元素引用。a=[1,2]可令a[0]=3；b=(1,2)不能这样赋值。']),
    ('明确疑惑未回应', [
        ('teacher', '列表可以修改元素，元组不能直接替换元素。两者都有顺序，用下标读取。'),
        ('ai', '元组不能修改，是不是也不能读取？读取和修改有什么区别？'),
        ('teacher', '继续看写法：列表用方括号，元组用圆括号。今天讲到这里。')]),
    ('模拟误解已澄清', [
        ('teacher', '列表的元素引用可修改，元组的元素引用不可修改。b=(1,2)，b[0]=3能运行吗？说明理由。'),
        ('ai', '可以，元组和列表一样能替换元素。'),
        ('teacher', '纠正：元组不能直接替换元素引用，这个赋值会报错。列表a=[1,2]可以令a[0]=3。'),
        ('teacher', '换个例子：要反复更新待办事项清单，你会选择列表还是元组？为什么？'),
        ('ai', '选列表，因为要增删和替换任务，列表支持这些操作。'),
        ('teacher', '理由符合修改清单的需求。总结：列表可修改元素引用，元组不允许直接替换元素引用。')]),
    ('模拟误解未处理', [
        ('teacher', '列表可修改元素引用，元组不允许直接替换。a=[1,2]可以令a[0]=3，b=(1,2)不能这样赋值。'),
        ('teacher', '现在b[0]=3会怎么样？请说明理由。'),
        ('ai', '会把元组变成(3,2)，元组也能替换元素。'),
        ('teacher', '继续看写法：列表用方括号，元组用圆括号。今天讲到这里。')]),
    ('材料不足', ['同学们好，今天开始讲课。']),
    ('疑似转写错误', ['我们算二加二，转写成2加2等于5。请先看这个加法例子。']),
    ('长课跨段纠正', ['先暂说尾节点还指向下一节点，后面会纠正。',
                      '链表遍历沿next逐个访问节点，访问后移动到next，判断当前指针是否为空。' * 900,
                      '纠正开头说法：尾节点指向空，不存在可访问的下一节点。先检查指针为空则结束，否则访问后移动。',
                      '练习用两个节点画出访问顺序。复杂优化合理安排到下一课，不在本次目标内。']),
]


def write_case(root, sid, name, texts):
    events = [{'type': 'ready', 'data': {'session_id': sid}}]
    teacher = 0
    for turn in texts:
        role, text = ('teacher', turn) if isinstance(turn, str) else turn
        if role == 'teacher':
            teacher += 1
            events.append({'type': 'transcript', 'data': {'id': f't{teacher}', 'text': text,
                           'mode': 'microphone' if name == '疑似转写错误' else 'text'}})
        else:
            events.append({'type': 'reply', 'data': {'kind': 'answer', 'text': text}})
    events.append({'type': 'finished', 'data': {'session_id': sid, 'lesson': {'topic': name},
                   'unprocessed_sources': [], 'state': {'open_questions': []}}})
    path = root / 'logs' / f'{sid}.jsonl'
    content = '\n'.join(json.dumps(e, ensure_ascii=False) for e in events)
    if path.exists() and path.read_text(encoding='utf-8') != content:
        raise ValueError('已有验收输入不同；请使用独立输出目录')
    path.write_text(content, encoding='utf-8')


def content_checks(name, report):
    finding = report['finding']
    checks = {}
    if name in ('正确讲解', '合理延期', '先错后纠正'):
        checks['未把已解释更正或合理延期凑成首要问题'] = finding['kind'] == '可选提升'
        checks['有具体保留做法'] = report['keep'] is not None
    if name == '明确概念错误':
        checks['准确性指出可改进'] = report['dimensions']['accuracy']['label'] == '可改进'
        checks['首要问题指向错误而非泛泛互动'] = finding['kind'] == '首要问题' and '元组' in finding['text']
    if name == '材料不足':
        checks['没有虚构优点或缺点'] = report['keep'] is None and finding['kind'] == '材料不足'
        checks['四维诚实说明不能判断'] = all(d['label'] == '暂不能判断' for d in report['dimensions'].values())
    if name == '疑似转写错误':
        checks['先建议核对录音'] = '核对' in str(report) and '录音' in str(report)
    if name == '教师自问自答':
        checks['肯定针对概念的设问设计'] = report['dimensions']['checking']['label'] == '做得好'
        checks['不把未呈现学生回答凑成首要问题'] = finding['kind'] == '可选提升'
        checks['没有编造学生已经理解'] = '学生已经理解' not in str(report)
    if name == '缺少互动材料':
        checks['互动材料不足诚实说明'] = report['dimensions']['checking']['label'] == '暂不能判断'
        checks['不因无回应制造教学问题'] = finding['kind'] == '可选提升'
    if name == '明确疑惑未回应':
        checks['指出忽略目标内的明确疑惑'] = report['dimensions']['checking']['label'] == '可改进'
        checks['改法澄清读取与修改'] = finding['kind'] == '首要问题' and all(
            s in report['action']['text'] for s in ('读取', '修改'))
    if name == '模拟误解已澄清':
        checks['认可澄清后换例说明理由的有效检查'] = report['dimensions']['checking']['label'] == '做得好'
        checks['未把已解决误解凑为首要问题'] = finding['kind'] == '可选提升'
        checks['未强加未讲授的进阶检查'] = not any(s in report['action']['text'] + report['practice']['task']
            for s in ('append', '嵌套', '内部对象', '元组内', '元组里', '底层'))
    if name == '模拟误解未处理':
        checks['检查指出未处理的具体误解'] = report['dimensions']['checking']['label'] == '可改进'
        checks['优先改进对准核心误解'] = finding['kind'] == '首要问题' and '元组' in finding['text']
    if name == '长课跨段纠正':
        checks['后段更正后准确性成立'] = report['dimensions']['accuracy']['label'] == '做得好'
        checks['问题没有误指已纠正知识或合理延期'] = all(s not in finding['text'] for s in ('尾节点指向下一', '复杂优化', '漏讲'))
    checks['练习检查教师动作'] = report['practice']['criterion'].startswith('教师能') and all(
        s not in report['practice']['criterion'] for s in ('AI答对', '学生掌握', '学生能', '学生答对'))
    return checks


async def run(args):
    if not args.live:
        raise SystemExit('只有显式--live才调用收费模型')
    settings = dotenv_values(args.env_file)
    config.API_KEY = settings.get('DEEPSEEK_API_KEY') or settings.get('Deepseek_API') or config.API_KEY
    config.BASE_URL = settings.get('LLM_BASE_URL', config.BASE_URL).rstrip('/')
    config.MODEL = settings.get('LLM_MODEL', config.MODEL)
    root = args.output.resolve()
    (root / 'logs').mkdir(parents=True, exist_ok=True)
    config.ROOT = root
    calls, outputs, results = [], [], []
    class BoundedModel(ModelClient):
        def __init__(self, emit):
            async def audit(kind, data):
                if kind == 'llm_request':
                    if len(calls) >= args.max_requests:
                        raise ModelError('本轮真实验收已到请求上限，停止调用')
                    calls.append(data['phase'])
                    print(json.dumps({'request': len(calls), 'phase': data['phase']}), flush=True)
                if kind == 'llm_output':
                    outputs.append({'seconds': data['seconds'], 'usage': data.get('usage')})
                await emit(kind, data)
            super().__init__(audit)
    service = ReportService(BoundedModel)
    selected = [case for case in CASES if not args.case or case[0] in args.case]
    for name, texts in selected:
        i = next(i for i, case in enumerate(CASES, 1) if case[0] == name)
        sid = f'{i:012x}'
        write_case(root, sid, name, texts)
        started = time.monotonic()
        await service.start(sid)
        task = service.tasks.get(sid)
        if task: await task
        result = service.get(sid)
        checks = content_checks(name, result['report']) if result['status'] == 'ready' else {}
        entry = {'case': name, 'status': result['status'], 'error': result['error'], 'checks': checks,
                 'seconds': round(time.monotonic() - started, 2)}
        results.append(entry)
        if result['status'] == 'ready':
            (root / f'{i:02d}-{name}.md').write_text(export_markdown(result), encoding='utf-8')
            assert ReportService().get(sid)['report'] == result['report']
        print(json.dumps(entry, ensure_ascii=False), flush=True)
        (root / 'validation.json').write_text(json.dumps({'cases': results, 'calls': calls, 'outputs': outputs},
            ensure_ascii=False, indent=2), encoding='utf-8')
        if len(calls) >= args.max_requests: break
    if args.classroom and len(results) == len(selected) and len(calls) < args.max_requests:
        original = args.classroom.read_bytes()
        digest = hashlib.sha256(original).hexdigest()
        events = [json.loads(line) for line in original.decode('utf-8-sig').splitlines() if line.strip()]
        sid = next(e['data']['session_id'] for e in reversed(events) if e['type'] == 'finished')
        target = root / 'logs' / f'{sid}.jsonl'
        target.write_bytes(original)
        await service.start(sid)
        task = service.tasks.get(sid)
        if task: await task
        result = service.get(sid)
        entry = {'case': '用户实际课堂', 'status': result['status'], 'error': result['error'],
                 'checks': {'原始导出没有变化': hashlib.sha256(args.classroom.read_bytes()).hexdigest() == digest}}
        results.append(entry)
        if result['status'] == 'ready':
            (root / '09-真实试讲.md').write_text(export_markdown(result), encoding='utf-8')
        (root / 'validation.json').write_text(json.dumps({'cases': results, 'calls': calls, 'outputs': outputs},
            ensure_ascii=False, indent=2), encoding='utf-8')
        print(json.dumps(entry, ensure_ascii=False), flush=True)
    await service.close()
    if len(results) != len(selected) + bool(args.classroom) or any(r['status'] != 'ready' or not all(r['checks'].values()) for r in results):
        raise SystemExit(1)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--live', action='store_true')
    parser.add_argument('--env-file', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--max-requests', type=int, default=14)
    parser.add_argument('--classroom', type=Path)
    parser.add_argument('--case', action='append', choices=[name for name, _ in CASES],
                        help='仅验收选定场景，可重复指定；未指定则运行全部场景')
    asyncio.run(run(parser.parse_args()))
