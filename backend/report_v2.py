"""报告 V2 后台生成、阶段缓存、证据校验与 Markdown 导出，不修改课堂。"""
import asyncio
from copy import deepcopy
import json
import os
from pathlib import Path
import re
import tempfile

from . import config, report_prompts
from .llm import ModelClient, ModelError
from .report_models import RecallPlan, Answers, Verification, Diagnosis
from .report_data import refresh_question_settlement
from .report_reader import reader_view

LIMITATION = '模拟验证只提供课堂内解释或应用的证据，不能证明真实掌握或知识客观正确。'
PHASES = ('recall', 'answers', 'verification', 'diagnosis')


def validate_citations(items, snapshot, *, learning_only=False):
    allowed = snapshot['evidence']
    learning_ids = {s['event_id'] for s in snapshot['learning_sources']}
    for citation in items:
        eid, quote = citation['event_id'], citation['quote']
        if not eid.startswith(snapshot['session_id'] + ':') or eid not in allowed:
            raise ValueError('报告引用了不存在或其他课堂的事件')
        if not quote.strip() or quote not in allowed[eid]['text']:
            raise ValueError('报告引用不是对应事件的实际原文')
        if learning_only and eid not in learning_ids:
            raise ValueError('学生不能把未处理内容或自己的发言当作学到的知识')


def validate_teaching_citations(items, snapshot):
    """Teacher analysis may observe delivered replies, without treating them as learning sources."""
    validate_citations(items, snapshot)
    allowed = {s['event_id'] for s in snapshot['learning_sources']}
    allowed.update(eid for eid, e in snapshot['evidence'].items() if e['kind'] == 'reply')
    for citation in items:
        if citation['event_id'] not in allowed:
            raise ValueError('教学评价只能引用已处理讲授、前置知识或实际课堂学生发言，不能引用未处理内容或状态记录：' + citation['event_id'])


def naturalize_recall(plan, snapshot):
    # Recover a missing link only from one exact, non-empty question text.
    # Never infer a paraphrase or resolve ambiguous duplicate questions.
    for item in plan['doubts'] + plan['uncertain']:
        if not item['question_ids']:
            matches = [q['id'] for q in snapshot['questions']
                       if q['text'].strip() and q['text'] in item['text']]
            if len(matches) == 1:
                item['question_ids'] = matches
    expressed = {q['id'] for q in snapshot['settlement']['questions'] if q.get('expressed')}
    for item in list(plan['doubts']):
        if not set(item['question_ids']).intersection(expressed):
            plan['doubts'].remove(item)
            item['text'] = '我还不能确认这个理解：' + item['text']
            plan['uncertain'].append(item)
    starts = ('我的理解是', '我记住的是', '我现在知道')
    for i, item in enumerate(plan['explained']):
        item['text'] = re.sub(r'^我能解释', starts[i % len(starts)], item['text'])
    for group in ('explained', 'doubts', 'uncertain'):
        for item in plan[group]:
            for internal, label in {'tentative':'暂定理解', 'understood':'当前理解', 'unclear':'信息还不完整', 'conflict':'理解有冲突'}.items():
                item['text'] = re.sub(r'\b' + internal + r'\b', label, item['text'])


def validate_recall(plan, snapshot):
    naturalize_recall(plan, snapshot)
    known = {k['id']: k for k in snapshot['settlement']['knowledge_points']}
    source_events = {s['id']: s['event_id'] for s in snapshot['learning_sources']}
    questions = {q['id']: q for q in snapshot['questions']}
    preserved = set()
    for group in ('explained', 'doubts', 'uncertain'):
        for index, item in enumerate(plan[group]):
            if '我' not in item['text']:
                raise ValueError('知识复述必须使用学生第一人称')
            if set(item['knowledge_ids']) - known.keys() or set(item['question_ids']) - questions.keys():
                raise ValueError('复述引用了不存在的知识或疑问')
            # 学生原话可证明疑问或为复述提供上下文；每条知识仍须在下方绑定实际讲授来源。
            validate_citations(item['citations'], snapshot)
            cited_ids = {c['event_id'] for c in item['citations']}
            unmatched = [k for k in item['knowledge_ids']
                         if not cited_ids.intersection(source_events.get(s) for s in known[k]['sources'])]
            if unmatched:
                required = {k: [source_events[s] for s in known[k]['sources'] if s in source_events] for k in unmatched}
                raise ValueError(f'复述证据与所引用的知识来源不对应：{group}[{index}]的knowledge_ids {unmatched}；对应可用event_id：{required}。请引用实际来源中的原文，或去掉本条未表达的重复知识编号，不编造引用。')
            if group == 'explained':
                if not item['knowledge_ids'] or any(known[k]['status'] in ('unclear', 'conflict') for k in item['knowledge_ids']):
                    raise ValueError('不能把缺失或冲突知识写成已经能解释')
                if not any(s['kind'] == 'teacher' and s['event_id'] in {c['event_id'] for c in item['citations']}
                           for s in snapshot['learning_sources']):
                    raise ValueError('本次学到的知识必须有实际教师讲授证据')
            if group in ('doubts', 'uncertain'):
                preserved.update(item['question_ids'])
            if any(item['text'].strip() == c['quote'].strip() and snapshot['evidence'][c['event_id']]['kind'] == 'transcript'
                   for c in item['citations']):
                raise ValueError('学生复述不能直接复制教师原句')
    unresolved = {q['id'] for q in questions.values() if q['status'] != 'resolved'}
    if not unresolved <= preserved:
        raise ValueError('学生复述遗漏了课堂仍未解决的疑问：' + '、'.join(sorted(unresolved - preserved)) + '；须在doubts或uncertain的question_ids填写对应编号。')
    ids, targets = set(), set()
    for probe in plan['probes']:
        if probe['id'] in ids or probe['knowledge_id'] in targets or probe['knowledge_id'] not in known:
            raise ValueError('验证题编号或知识点无效、重复')
        if known[probe['knowledge_id']]['status'] in ('unclear', 'conflict'):
            raise ValueError('缺失或冲突知识不能作为可靠验证考点')
        validate_citations(probe['citations'], snapshot, learning_only=True)
        if not {c['event_id'] for c in probe['citations']}.intersection(source_events.get(s) for s in known[probe['knowledge_id']]['sources']):
            raise ValueError('验证题证据与考点来源不对应')
        if any(c['quote'].strip() in probe['question'] for c in probe['citations']):
            raise ValueError('验证题直接带入了课堂答案')
        ids.add(probe['id']); targets.add(probe['knowledge_id'])


def validate_answers(answers, plan, snapshot):
    ids = [a['id'] for a in answers['answers']]
    if len(ids) != len(set(ids)) or set(ids) != {p['id'] for p in plan['probes']}:
        raise ValueError('验证作答与题目不对应')
    for answer in answers['answers']:
        validate_citations(answer['citations'], snapshot, learning_only=True)


def validate_verification(result, plan, answers, snapshot):
    ids = [v['id'] for v in result['results']]
    if len(ids) != len(set(ids)) or set(ids) != {p['id'] for p in plan['probes']}:
        raise ValueError('验证结论与题目不对应')
    answered = {a['id']: a for a in answers['answers']}
    probes = {p['id']: p for p in plan['probes']}
    def normalized(text):
        # Only canonicalize identifiers/numbers, never infer understanding from a similarity score.
        text = re.sub(r'[A-Za-z_][A-Za-z_0-9]*|\d+(?:\.\d+)?', '@', text)
        return re.sub(r'\W', '', text)
    for verdict in result['results']:
        validate_citations(verdict['citations'], snapshot, learning_only=True)
        answer = answered[verdict['id']]
        probe = probes[verdict['id']]
        checks = verdict.get('task_checks') or []
        reasoning = verdict.get('reasoning_check') or {}
        required = probe.get('required_tasks') or []
        quote = reasoning.get('answer_quote', '')
        repetition = any(re.sub(r'\W', '', answer['text']) == re.sub(r'\W', '', s['text'])
                         for s in snapshot['learning_sources'])
        canonical_copy = bool(quote.strip()) and any(
            normalized(quote) and normalized(quote) in normalized(s['text'])
            for s in snapshot['learning_sources'] if s['kind'] == 'teacher')
        supported = bool(required and checks and reasoning and quote.strip() and quote in answer['text'])
        supported = supported and set(required) == {c['task'] for c in checks} and len(checks) == len(required)
        supported = supported and all(t in probe['question'] for t in required)
        for check in checks + ([reasoning] if reasoning else []):
            validate_citations(check.get('citations', []), snapshot, learning_only=True)
            if not check.get('citations') or not check.get('answer_quote', '').strip() or check['answer_quote'] not in answer['text']:
                supported = False
        outcomes = {c['outcome'] for c in checks}
        comparison = reasoning.get('comparison') or {}
        if comparison:
            validate_citations(comparison.get('citations', []), snapshot, learning_only=True)
        compared = bool(comparison.get('difference') and comparison.get('citations')
                        and comparison.get('answer_quote', '').strip()
                        and comparison['answer_quote'] in answer['text'])
        teacher_steps = comparison.get('teacher_steps') or []
        task_steps = comparison.get('task_steps') or []
        task_quote = comparison.get('task_quote', '')
        compared = compared and bool(teacher_steps and task_steps and task_quote.strip() and task_quote in probe['question'])
        if verdict['status'] == '有理解证据':
            if verdict['reasoning'] != '解释或应用' or repetition or canonical_copy or reasoning.get('kind') == '仅换名换数或复述':
                verdict.update(status='尚未验证', explanation='回答仅有复述证据，尚不能确认解释或应用。')
            elif not supported or not verdict['citations'] or not answer['citations']:
                verdict.update(status='证据不足', explanation='缺少逐项任务检查、实际作答原文或课堂依据，不能确认独立推理。')
            elif '理由错误' in outcomes:
                verdict.update(status='存在误解', explanation='结论即使正确，理由仍与课堂依据冲突：' + '；'.join(c['explanation'] for c in checks if c['outcome'] == '理由错误'))
            elif outcomes.intersection({'未回答', '部分完成'}):
                verdict.update(status='尚未验证', explanation='未完成题目要求的全部解释或应用：' + '；'.join(c['explanation'] for c in checks if c['outcome'] != '有依据地完成'))
            elif outcomes != {'有依据地完成'} or reasoning.get('kind') != '新情境推理' or probe.get('task_kind', '未标注') == '未标注':
                verdict.update(status='证据不足', explanation='无法可靠确认新情境中的推理，不能仅因答案正确而判定理解。')
            elif comparison.get('kind') == '仅表面替换' or (teacher_steps and task_steps and teacher_steps == task_steps):
                verdict.update(status='尚未验证', explanation='题目只替换已演示例子的表面信息，回答正确仍不足以确认独立迁移。')
            elif not compared or comparison.get('kind') != '实质任务变化':
                verdict.update(status='证据不足', explanation='没有对照课堂已演示任务核实实质变化；仅凭正确答案不能确认独立迁移。')
        elif verdict['status'] == '存在误解' and not verdict['citations']:
            verdict.update(status='证据不足', explanation='没有课堂依据，无法可靠判断是否存在误解。')


def validate_diagnosis(diagnosis, snapshot, *, require_current=False):
    for withdrawal in diagnosis.get('withdrawals', []):
        if not withdrawal['reason'].strip():
            raise ValueError('撤销评价必须说明原因')
        validate_teaching_citations(withdrawal['citations'], snapshot)
    if require_current and not diagnosis.get('summary'):
        raise ValueError('教学分析没有返回总体评价，不能把空对象当作分析成功；可重试。')
    if diagnosis.get('summary'):
        validate_teaching_citations(diagnosis['summary']['citations'], snapshot)
    for point in diagnosis.get('key_points', []):
        validate_citations(point['citations'], snapshot, learning_only=True)
        if not any(snapshot['evidence'][c['event_id']]['kind']=='transcript' for c in point['citations']):
            raise ValueError('讲授要点必须引用实际教师讲授，不能只引用前置知识')
    dimensions = diagnosis.get('dimensions', [])
    names = [item['name'] for item in dimensions]
    if len(names) != len(set(names)):
        raise ValueError('评价维度重复')
    if require_current and set(names) != {'内容准确性','结构衔接','解释与例子','互动检查','表达节奏'}:
        raise ValueError('新报告必须覆盖五个教学维度；没有依据的维度请明确填写缺少依据')
    if require_current and diagnosis['weaknesses'] and not diagnosis.get('practice'):
        raise ValueError('有主要不足的报告必须给出下次练习和完成标准')
    for item in dimensions:
        validate_teaching_citations(item['citations'], snapshot)
        if item['status'] != '缺少依据' and not item['citations']:
            raise ValueError('评价维度必须有课堂依据，不能把无证据写成已评价')
    findings = diagnosis['strengths'] + diagnosis['weaknesses']
    ids = [f['id'] for f in findings]
    if len(ids) != len(set(ids)):
        raise ValueError('教学诊断编号重复')
    for finding in findings:
        validate_teaching_citations(finding['citations'], snapshot)
        validate_citations(finding.get('followup_citations', []), snapshot, learning_only=True)
        if not any(snapshot['evidence'][c['event_id']]['kind'] in ('transcript', 'reply', 'review_completed')
                   for c in finding['citations']):
            raise ValueError('教学诊断必须引用实际教师或学生发言')
        # State and teacher statements cannot stand in for delivered student answers.
        text = finding['observation']
        if re.search(r'学生(?:回答|表示|说|仍|已经|没有理解|未理解|误解|困惑|掌握|理解了)', text):
            if not any(snapshot['evidence'][c['event_id']]['kind'] == 'reply' for c in finding['citations']):
                replies = [eid for eid, e in snapshot['evidence'].items() if e['kind'] == 'reply']
                available = '、'.join(replies[:6]) or '无实际学生发言'
                raise ValueError(f"{finding['id']}的observation“{text}”：学生表现判断缺少实际课堂学生发言。请在该项citations引用支持此事实的reply，不能以教师讲授或课后模拟代替。本课堂reply：{available}。若只评价教师行为，请去掉无依据的学生表现断言；不能机械添加无关引用。")
    weaknesses = {f['id'] for f in diagnosis['weaknesses']}
    linked = [s['finding_id'] for s in diagnosis['suggestions']]
    if set(linked) - weaknesses or len(linked) != len(set(linked)):
        raise ValueError('建议必须对应本报告的具体不足，不能重复或虚构问题')
    # A fleeting question followed by an explanation is not evidence of teaching failure.
    supported = []
    for finding in diagnosis['weaknesses']:
        cited = {c['event_id'] for c in finding['citations']}
        if finding.get('basis') == '教学内容或检查机会':
            if not any(snapshot['evidence'][eid]['kind'] == 'transcript' for eid in cited):
                raise ValueError('教学改进必须有实际教师讲授依据')
            supported.append(finding)
            continue
        for q in snapshot['settlement']['questions']:
            mention_ids = {m['event_id'] for m in snapshot['settlement'].get('question_mentions', []) if m['question_id'] == q['id']}
            explanation_orders = [e['order'] for e in snapshot['evidence'].values()
                                  if e.get('source_id') in q.get('explanation_sources', [])]
            continued = {eid for eid in mention_ids if explanation_orders and snapshot['evidence'][eid]['order'] > max(explanation_orders)
                         and re.search(r'不[清确]楚|不理解|没[有弄]*明白|困惑|疑惑|[？?]', snapshot['evidence'][eid]['text'])}
            if not q.get('expressed') or (q['status'] == 'resolved' and not cited.intersection(continued)): continue
            topic = q.get('topic', q['text'])
            description = finding['observation'] + finding['interpretation']
            relevant = any(topic[i:i+3] in description for i in range(max(0, len(topic)-2))) or topic[-2:] in description
            teacher_evidence = any(snapshot['evidence'][eid]['kind'] == 'transcript' for eid in cited)
            if cited.intersection(mention_ids) and relevant and teacher_evidence:
                supported.append(finding); break
    kept = {f['id'] for f in supported}
    if require_current and kept != weaknesses:
        raise ValueError('不足的依据类型与引用不匹配：学生疑问必须引用实际学生发言；教师例子或讲法问题应归为教学内容或检查机会。请核对后续解释，不得静默删除问题和对应建议。')
    if require_current and set(linked) != weaknesses:
        raise ValueError('每个主要不足必须有对应的具体改进动作')
    diagnosis['weaknesses'] = supported
    diagnosis['suggestions'] = [s for s in diagnosis['suggestions'] if s['finding_id'] in kept]


def validate_diagnosis_repair(previous, corrected, snapshot):
    """A correction must preserve findings or explicitly retract them with evidence."""
    before = {f['id'] for f in previous['strengths'] + previous['weaknesses']}
    after = {f['id'] for f in corrected['strengths'] + corrected['weaknesses']}
    withdrawals = corrected.get('withdrawals', [])
    ids = [w['finding_id'] for w in withdrawals]
    if len(ids) != len(set(ids)) or set(ids) != before - after:
        raise ValueError('修正报告不能静默删除评价；保留原编号，或在withdrawals逐项注明撤销原因和课堂依据。')
    validate_diagnosis(corrected, snapshot, require_current=True)


def _detailed_markdown(view):
    snapshot = view['snapshot']; summary = snapshot['settlement']
    lines = ['# Learn From You · 课后反馈报告 V2', '', f"课堂：{snapshot['session_id']}", '',
             f"主题：{snapshot['lesson'].get('topic', '未提供')}", '', LIMITATION, '', '## 本次试讲结算', '']
    for item in reader_view(view)['metrics'].values():
        value = '无法统计' if item['value'] is None else str(item['value'])
        description = f"（{item['display']}）" if item['display'] != value else ''
        lines += [f"- {item['label']}：{value}{description}。口径：{item['method']}"]
    knowledge_labels = {'understood':'当前理解', 'tentative':'暂定理解', 'conflict':'存在冲突', 'unclear':'信息缺失'}
    lines += ['', '主要知识点：'] + [f"- {k['text']}（{knowledge_labels.get(k['status'], k['status'])}）" for k in summary['knowledge_points']]
    if not summary['knowledge_points']: lines += ['- 无可靠已处理知识记录。']
    qlabels = {'pending':'系统待提问', 'asked':'系统已提问', 'resolved':'系统标记已解决', 'deferred':'暂缓处理，尚未解决'}
    lines += ['', '疑问状态：'] + [f"- {q['id']}：{q['text']} · {'学生已提出' if q.get('expressed') else '仅系统记录，未确认学生提出'} · {qlabels.get(q['status'], q['status'])} · 回应来源：{'、'.join(q.get('response_sources', [])) or '未记录'} · 内容解释来源：{'、'.join(q.get('explanation_sources', [])) or '未记录'} · 独立理解：尚未验证" for q in summary['questions']]
    lines += [''] + ['- ' + note for note in summary.get('limitations', [])]
    lines += ['', '未处理来源：' + ('、'.join(summary['unprocessed_sources']) or '无'),
              '处理异常记录：'] + [f"- {e['event_id']}：{e['text']}" for e in summary['processing_errors']]
    if summary['incomplete']: lines += ['', '**反馈可能不完整：存在未处理讲授或录音识别缺口，不能算作学生已经学习；请核对是否已重讲。**']
    lines += ['', '## 学生说，我学到了什么', '']
    def cites(items):
        grouped = {}
        for c in items:
            grouped.setdefault(c['event_id'], [])
            if c['quote'] not in grouped[c['event_id']]: grouped[c['event_id']].append(c['quote'])
        return [f"  - 证据 `{eid}`：" + ' / '.join(quotes) for eid, quotes in grouped.items()]
    recall = view.get('recall')
    if recall:
        for key, title in [('explained', '我的理解'), ('doubts', '我实际表达过的疑问'), ('uncertain', '尚未确认的理解（模拟推断）')]:
            lines += ['### ' + title, '']
            for item in recall[key]: lines += ['- ' + item['text']] + cites(item['citations'])
            if not recall[key]: lines += ['暂无有依据的内容。']
    else: lines += ['尚未生成或生成失败；不使用虚构复述补位。']
    lines += ['', '## 学生真的理解了吗', '', LIMITATION]
    answers = {a['id']: a for a in (view.get('answers') or {}).get('answers', [])}
    results = {v['id']: v for v in (view.get('verification') or {}).get('results', [])}
    for probe in (recall or {}).get('probes', []):
        answer = answers.get(probe['id']); result = results.get(probe['id'])
        lines += ['', f"### {probe['id']} · {probe['knowledge_id']}", probe['question'],
                  '学生回答：' + (answer['text'] if answer else '尚未验证'),
                  '结论：' + (result['status'] if result else '尚未验证'),
                  '依据说明：' + (result['explanation'] if result else '验证未完成或失败。')]
        lines += cites(probe['citations'] + (answer['citations'] if answer else []) + (result['citations'] if result else []))
    if not (recall or {}).get('probes'): lines += ['', '没有可靠验证题，尚未验证。']
    diagnosis = view.get('diagnosis') or {'strengths': [], 'weaknesses': [], 'suggestions': []}
    for key, title in [('strengths', '本次试讲的优点'), ('weaknesses', '本次试讲的不足')]:
        lines += ['', '## ' + title, '']
        for finding in diagnosis[key]:
            lines += [f"- {finding['id']} · 观察事实：{finding['observation']}", '  AI 推断：' + finding['interpretation']] + cites(finding['citations'])
        if not diagnosis[key]: lines += ['没有足够证据作出判断，或分析尚未完成。']
    lines += ['', '## 下次应该怎么改', '']
    for suggestion in diagnosis['suggestions']:
        lines += [f"- {suggestion['priority']} · 针对 {suggestion['finding_id']} · 下次尝试：{suggestion['action']}"]
    if not diagnosis['suggestions']: lines += ['暂无有依据的建议。']
    lines += ['', '## 课堂证据', '']
    for evidence in snapshot['evidence'].values():
        at = f"{evidence['at']} 秒" if evidence['at'] is not None else f"第 {evidence['order']} 个记录"
        anchor = 'report-evidence-' + re.sub(r'[^a-zA-Z0-9_-]', '-', evidence['event_id'])
        text=evidence['text']
        if evidence['kind'] in ('state','finished','ready'):
            text='课堂开始记录。' if evidence['kind']=='ready' else '模拟认知状态记录，不能作为真实学生理解证据。完整原始状态保留在本地课堂记录及页面详细依据中。'
        lines += [f'<a id="{anchor}"></a>', f"### {evidence['event_id']} · {at}", text]
        if 'raw_text' in evidence:
            lines += ['原始转写：' + evidence['raw_text'], '实际学习文本：' + evidence['text']]
            if evidence.get('review_event_id'): lines += ['审核事件：' + evidence['review_event_id']]
    lines += ['', '## 生成状态与限制', '', '状态：' + view['status']]
    for withdrawal in diagnosis.get('withdrawals', []):
        lines += [f"- 修正时撤销 {withdrawal['finding_id']}：{withdrawal['reason']} · 依据：" +
                  '、'.join(c['event_id'] for c in withdrawal['citations'])]
    phase_labels = {'recall':'学生知识复述', 'answers':'学生独立作答', 'verification':'理解验证', 'diagnosis':'教学反馈'}
    stage_labels = {'pending':'尚未生成', 'generating':'生成中', 'success':'已生成', 'skipped':'无可靠题目，尚未验证', 'failed':'未完成'}
    for phase, stage in view['stages'].items(): lines += [f"- {phase_labels[phase]}：{stage_labels[stage['status']]}" + ((' · ' + stage['error']) if stage.get('error') else '')]
    lines += ['', '引用校验不能完全证明语义支持。模拟模型样例不是付费真实模型验收。']
    return '\n'.join(lines) + '\n'


def markdown(view, *, include_details=False):
    reader = reader_view(view)
    def cites(items):
        if not include_details:
            return []
        ids = list(dict.fromkeys(c['event_id'] for c in items))
        refs=[]
        for eid in ids:
            evidence=view['snapshot']['evidence'][eid]
            speaker='AI 学生' if evidence['kind']=='reply' else '教师'
            at=f"{evidence['at']} 秒" if evidence.get('at') is not None else f"记录 {evidence['order']}"
            anchor='report-evidence-'+re.sub(r'[^a-zA-Z0-9_-]','-',eid)
            refs.append(f'[{speaker} · {at}](#{anchor})')
        return ['课堂依据：'+'、'.join(refs)] if refs else []
    lines = ['# 课后教学反馈', '', '主题：' + view['snapshot']['lesson'].get('topic', '未提供'), '',
             '## 本节讲授要点', '']
    lines += ['- ' + item['text'] for item in reader['key_points']] or [reader['points_empty_message']]
    lines += ['', '## 课堂总体评价', '', reader['summary']]
    lines += cites(reader['summary_citations'])
    if reader['diagnosis_failed']:
        # Show a meaningful cause up front; exact schema diagnostics are in folded details.
        error = reader['error']
        cause = '模型输出不符合报告结构要求' if '结构' in error else error
        lines += ['', '失败原因：' + cause, '重试方式：在报告页面点击“重试未完成分析”。']
    if view.get('diagnosis'):
        if reader['dimensions']:
            lines += ['', '## 教学维度简评', '']
            lines += [f"- {item['name']}（{item['status']}）：{item['text']}" for item in reader['dimensions']]
        lines += ['', '## 本次试讲的优点', '']
        for f in reader['strengths']:
            lines += ['- ' + f['observation'], '  教学作用（分析）：' + f['interpretation']] + cites(f['citations'])
        if not reader['strengths']: lines += ['现有依据不足以提炼明确优点。']
        lines += ['', '## 优先改进的问题（本次试讲的不足）', '']
        for i, f in enumerate(reader['issues'], 1):
            lines += [f"### {i}. {f['observation']}", f['interpretation'],
                      '下次应该怎么改：' + (f['suggestion']['action'] if f['suggestion'] else '尚未生成有依据的具体动作，不能用套话补位。')]
            lines += cites(f['citations'])
        if not reader['issues']: lines += ['未发现证据充分的主要改进问题；这不代表所有教学维度均已验证。']
        if reader['practice']:
            lines += ['', '## 下次练习', '', reader['practice']['action'], '完成标准：' + reader['practice']['check']]
    lines += ['', '## 学生理解情况', '', reader['student']]
    if reader['verification_counts']:
        lines += ['课后 AI 模拟检查：' + '；'.join(f'{k} {v} 题' for k,v in reader['verification_counts'].items()) + '。']
    else: lines += ['课后 AI 模拟检查尚未完成或没有可靠题目。']
    if not include_details:
        return '\n'.join(lines) + '\n'
    lines += ['', LIMITATION, '', '<details>', '<summary>详细依据：课堂原文、复述与作答、统计及生成状态（展开核查）</summary>', '']
    details = _detailed_markdown(view)
    details = details[details.index('## 本次试讲结算'):]
    # Feedback is already presented above; retain additional legacy findings only as audit data.
    start = details.index('## 本次试讲的优点')
    end = details.index('## 课堂证据')
    details = details[:start] + details[end:]
    lines += [details.rstrip(), '', '</details>', '']
    return '\n'.join(lines)


class ReportService:
    def __init__(self, client_factory=ModelClient):
        self.client_factory = client_factory
        self.views = {}
        self.tasks = {}
        self.lock = asyncio.Lock()

    def _path(self, sid):
        if not re.fullmatch(r'[a-f0-9]{12}', sid): raise ValueError('课堂编号无效')
        return config.ROOT / 'logs' / f'{sid}.report-v2.json'

    def _save(self, sid):
        path = self._path(sid)
        path.parent.mkdir(exist_ok=True)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile('w', encoding='utf-8', dir=path.parent, suffix='.report-v2.tmp', delete=False) as f:
                temporary = f.name
                json.dump(self.views[sid], f, ensure_ascii=False)
            os.replace(temporary, path)
        finally:
            if temporary and os.path.exists(temporary): os.unlink(temporary)

    def register(self, snapshot):
        sid = snapshot['session_id']
        if sid in self.views:
            if self.views[sid]['snapshot']['source_hash'] != snapshot['source_hash']:
                raise ValueError('已经结束的课堂快照发生变化')
            return
        self.views[sid] = {'snapshot': deepcopy(snapshot), 'status': 'idle',
            'stages': {phase: {'status': 'pending', 'error': ''} for phase in PHASES},
            'recall': None, 'answers': None, 'verification': None, 'diagnosis': None}
        self._save(sid)

    def get(self, sid):
        path = self._path(sid)
        if sid not in self.views:
            loaded = json.loads(path.read_text(encoding='utf-8'))
            if loaded['snapshot']['session_id'] != sid: raise ValueError('课堂缓存编号不一致')
            if loaded['status'] == 'generating':
                loaded['status'] = 'failed'
                for stage in loaded['stages'].values():
                    if stage['status'] == 'generating': stage.update(status='failed', error='服务重启中断了生成，可以重试。')
            self.views[sid] = loaded
        view = deepcopy(self.views[sid])
        # Read-only compatibility projection: preserve original cached/model outputs and event hash.
        refresh_question_settlement(view['snapshot'])
        if view.get('recall'): naturalize_recall(view['recall'], view['snapshot'])
        if view.get('verification') and view.get('answers') and view.get('recall'):
            validate_verification(view['verification'], view['recall'], view['answers'], view['snapshot'])
        if view.get('diagnosis'):
            before = {f['id'] for f in view['diagnosis']['weaknesses']}
            validate_diagnosis(view['diagnosis'], view['snapshot'])
            if before != {f['id'] for f in view['diagnosis']['weaknesses']}:
                view['status'] = 'partial'
                view['stages']['diagnosis'] = {'status':'failed', 'error':'旧报告存在依据不匹配的问题，教学分析不完整；可重试修正。'}
        view['reader'] = reader_view(view)
        return view

    def start(self, sid):
        view = self.get(sid)
        if view['status'] == 'ready' or (sid in self.tasks and not self.tasks[sid].done()): return view
        if view['stages']['diagnosis']['status'] == 'failed':
            self.views[sid]['stages']['diagnosis'] = deepcopy(view['stages']['diagnosis'])
            self.views[sid]['diagnosis'] = None
        self.views[sid]['status'] = 'generating'
        self.tasks[sid] = asyncio.create_task(self._run(sid))
        return self.get(sid)

    async def pause(self):
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, sid):
        view = self.views[sid]; snapshot = view['snapshot']
        async def audit_emit(kind, data):
            # Local-only diagnosis trace: no authentication headers or repeated input dumps.
            record = {**data}
            if kind == 'llm_request':
                record = {key:data[key] for key in ('phase', 'attempt')}
                record['model'] = data['body']['model']
                view.setdefault('model_usage', []).append({'phase':record['phase'], 'attempt':record['attempt']})
            elif kind == 'llm_output' and view.get('model_usage'):
                view['model_usage'][-1].update(usage=data.get('usage', {}), seconds=data.get('seconds'), model=data.get('model'))
            text = json.dumps({'type':kind, 'data':record}, ensure_ascii=False)
            if config.API_KEY: text = text.replace(config.API_KEY, '[REDACTED]')
            with self._path(sid).with_suffix('.events.jsonl').open('a', encoding='utf-8') as f:
                f.write(text + '\n')
        client = None
        try:
            async with self.lock:
                client = self.client_factory(audit_emit)
                classroom = {'lesson': snapshot['lesson'], 'state': snapshot['state'],
                    'sources': [{key: s[key] for key in ('id', 'text', 'kind', 'event_id')} for s in snapshot['learning_sources']],
                    'evidence': {eid: {key: value for key, value in e.items() if key not in ('raw_text', 'review_event_id')}
                                 for eid, e in snapshot['evidence'].items() if e['kind'] != 'error'},
                    'questions': snapshot['questions']}
                # Full teacher/reply text stays available, redundant state JSON does not.
                classroom['evidence'] = {eid:e for eid,e in classroom['evidence'].items()
                                         if e['kind'] in ('transcript', 'reply')}
                # Unprocessed transcripts remain visible for audit, never in model learning context.
                unprocessed_ids = {s['event_id'] for s in snapshot['sources'] if not s['processed']}
                classroom['evidence'] = {eid: e for eid, e in classroom['evidence'].items() if eid not in unprocessed_ids and e['kind'] != 'review_completed'}
                if not any(s['kind'] == 'teacher' for s in snapshot['learning_sources']):
                    raise ValueError('没有已处理的教师讲授，无法生成可靠复述或验证；基础结算仍可用。')
                if len(json.dumps(classroom, ensure_ascii=False)) > 60_000:
                    raise ValueError('课堂上下文超过当前报告容量；结算仍可用，未截断证据。')
                for phase in PHASES:
                    if view['stages'][phase]['status'] in ('success', 'skipped'): continue
                    view['stages'][phase].update(status='generating', error='')
                    try:
                        if phase in ('answers', 'verification') and not (view['recall'] or {}).get('probes'):
                            view[phase] = {'answers': []} if phase == 'answers' else {'results': []}
                            view['stages'][phase]['status'] = 'skipped'; continue
                        if phase == 'verification' and view['stages']['answers']['status'] != 'success':
                            raise ValueError('独立作答失败，不能生成通过结论。')
                        public = [{'id': p['id'], 'question': p['question']} for p in (view['recall'] or {}).get('probes', [])]
                        if phase == 'recall':
                            prompt, payload, schema = report_prompts.RECALL, classroom, RecallPlan
                        elif phase == 'answers':
                            prompt, payload, schema = report_prompts.ANSWER, {**classroom, 'public_questions': public}, Answers
                        elif phase == 'verification':
                            prompt, payload, schema = report_prompts.VERIFY, {**classroom, 'public_questions': public,
                                'verification_tasks': view['recall']['probes'], 'student_answers': view['answers']}, Verification
                        else:
                            prompt, payload, schema = report_prompts.DIAGNOSE, {**classroom, 'verification': self.get(sid)['verification'],
                                'settlement': snapshot['settlement'], 'limitation': LIMITATION,
                                'input_modes': {s['event_id']:s.get('mode', 'unknown') for s in snapshot['learning_sources'] if s['kind']=='teacher'}}, Diagnosis
                        async with asyncio.timeout(60):
                            result = await client.generate('report_v2_' + phase, prompt, payload, schema)
                            if phase in ('recall', 'diagnosis'):
                                try:
                                    if phase == 'recall':
                                        validate_recall(result.model_dump(), snapshot)
                                    else:
                                        validate_diagnosis(result.model_dump(), snapshot, require_current=True)
                                except ValueError as exc:
                                    await audit_emit(phase + '_validation_error', {'error':str(exc)})
                                    # One semantic correction only; transport/timeouts do not retry.
                                    previous = result.model_dump()
                                    instruction = ('修正上一份复述中指出的编号或证据错误，保持课堂学习边界。知识编号须对应本条引用的实际来源；所有未解决疑问在doubts或uncertain中携带question_ids。不能编造引用或用外部知识补齐；返回完整JSON。'
                                        if phase == 'recall' else
                                        '修正上一份报告中校验指出的问题；保留合法的事实、引用和对应建议及原finding编号。已回应的疑问不能写成未解决；如实际问题是缺少理解检查，请基于教师发言归为教学内容或检查机会。确需撤销无依据评价时，在withdrawals逐项填写finding_id、reason和课堂citations，不保留错误断言。返回完整 JSON，不静默删除问题来绕过校验。')
                                    result = await client.generate('report_v2_' + phase, prompt,
                                        {**payload, 'validation_feedback':str(exc),
                                         'previous_output':previous,
                                         'repair_instruction':instruction}, schema)
                                    if phase == 'diagnosis':
                                        validate_diagnosis_repair(previous, result.model_dump(), snapshot)
                        result = schema.model_validate(result).model_dump()
                        if phase == 'recall': validate_recall(result, snapshot)
                        elif phase == 'answers': validate_answers(result, view['recall'], snapshot)
                        elif phase == 'verification': validate_verification(result, view['recall'], view['answers'], snapshot)
                        else: validate_diagnosis(result, snapshot, require_current=True)
                        view[phase] = result
                        view['stages'][phase]['status'] = 'success'
                        # A recovered upstream phase changes dependent inputs. Never
                        # reuse an earlier skipped answer or diagnosis of missing results.
                        for dependent in PHASES[PHASES.index(phase)+1:]:
                            view[dependent] = None
                            view['stages'][dependent].update(status='pending', error='')
                    except (ModelError, ValueError, TimeoutError) as exc:
                        text = str(exc) or '模型请求超时，请重试。'
                        if config.API_KEY: text = text.replace(config.API_KEY, '[REDACTED]')
                        view['stages'][phase].update(status='failed', error=text)
                    self._save(sid)
                view['status'] = 'ready' if all(s['status'] in ('success', 'skipped') for s in view['stages'].values()) else 'partial'
        except asyncio.CancelledError:
            view['status'] = 'failed'
            for stage in view['stages'].values():
                if stage['status'] == 'generating': stage.update(status='failed', error='开始新课堂或服务关闭，报告生成已暂停；可重试。')
            raise
        except Exception as exc:
            view['status'] = 'failed'
            error = str(exc) if isinstance(exc, ValueError) else '报告未完成，请检查服务、容量与本地文件权限。'
            if config.API_KEY: error = error.replace(config.API_KEY, '[REDACTED]')
            for stage in view['stages'].values():
                if stage['status'] not in ('success', 'skipped'):
                    stage.update(status='failed', error=error)
        finally:
            if client: await client.close()
            try: self._save(sid)
            except OSError: pass  # The in-memory settlement survives disk errors.


report_service = ReportService()
