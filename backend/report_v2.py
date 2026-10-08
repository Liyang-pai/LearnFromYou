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


def naturalize_recall(plan, snapshot):
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
        for item in plan[group]:
            if '我' not in item['text']:
                raise ValueError('知识复述必须使用学生第一人称')
            if set(item['knowledge_ids']) - known.keys() or set(item['question_ids']) - questions.keys():
                raise ValueError('复述引用了不存在的知识或疑问')
            # 学生原话可证明疑问或为复述提供上下文；每条知识仍须在下方绑定实际讲授来源。
            validate_citations(item['citations'], snapshot)
            cited_ids = {c['event_id'] for c in item['citations']}
            if any(not cited_ids.intersection(source_events.get(s) for s in known[k]['sources']) for k in item['knowledge_ids']):
                raise ValueError('复述证据与所引用的知识来源不对应')
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
        raise ValueError('学生复述遗漏了课堂仍未解决的疑问')
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
        elif verdict['status'] == '存在误解' and not verdict['citations']:
            verdict.update(status='证据不足', explanation='没有课堂依据，无法可靠判断是否存在误解。')


def validate_diagnosis(diagnosis, snapshot):
    findings = diagnosis['strengths'] + diagnosis['weaknesses']
    ids = [f['id'] for f in findings]
    if len(ids) != len(set(ids)):
        raise ValueError('教学诊断编号重复')
    for finding in findings:
        validate_citations(finding['citations'], snapshot)
        if not any(snapshot['evidence'][c['event_id']]['kind'] in ('transcript', 'reply', 'review_completed')
                   for c in finding['citations']):
            raise ValueError('教学诊断必须引用实际教师或学生发言')
    weaknesses = {f['id'] for f in diagnosis['weaknesses']}
    linked = [s['finding_id'] for s in diagnosis['suggestions']]
    if set(linked) - weaknesses or len(linked) != len(set(linked)):
        raise ValueError('建议必须对应本报告的具体不足，不能重复或虚构问题')
    # A fleeting question followed by an explanation is not evidence of teaching failure.
    supported = []
    for finding in diagnosis['weaknesses']:
        cited = {c['event_id'] for c in finding['citations']}
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
    diagnosis['weaknesses'] = supported
    diagnosis['suggestions'] = [s for s in diagnosis['suggestions'] if s['finding_id'] in kept]


def markdown(view):
    snapshot = view['snapshot']; summary = snapshot['settlement']
    lines = ['# Learn From You · 课后反馈报告 V2', '', f"课堂：{snapshot['session_id']}", '',
             f"主题：{snapshot['lesson'].get('topic', '未提供')}", '', LIMITATION, '', '## 本次试讲结算', '']
    for item in summary['metrics'].values():
        value = '无法统计' if item['value'] is None else str(item['value'])
        lines += [f"- {item['label']}：{value}。口径：{item['method']}"]
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
        lines += [f"### {evidence['event_id']} · {at}", evidence['text']]
        if 'raw_text' in evidence:
            lines += ['原始转写：' + evidence['raw_text'], '实际学习文本：' + evidence['text']]
            if evidence.get('review_event_id'): lines += ['审核事件：' + evidence['review_event_id']]
    lines += ['', '## 生成状态与限制', '', '状态：' + view['status']]
    phase_labels = {'recall':'学生知识复述', 'answers':'学生独立作答', 'verification':'理解验证', 'diagnosis':'教学反馈'}
    stage_labels = {'pending':'尚未生成', 'generating':'生成中', 'success':'已生成', 'skipped':'无可靠题目，尚未验证', 'failed':'未完成'}
    for phase, stage in view['stages'].items(): lines += [f"- {phase_labels[phase]}：{stage_labels[stage['status']]}" + ((' · ' + stage['error']) if stage.get('error') else '')]
    lines += ['', '引用校验不能完全证明语义支持。模拟模型样例不是付费真实模型验收。']
    return '\n'.join(lines) + '\n'


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
        if view.get('diagnosis'): validate_diagnosis(view['diagnosis'], view['snapshot'])
        return view

    def start(self, sid):
        view = self.get(sid)
        if view['status'] == 'ready' or (sid in self.tasks and not self.tasks[sid].done()): return view
        self.views[sid]['status'] = 'generating'
        self.tasks[sid] = asyncio.create_task(self._run(sid))
        return self.get(sid)

    async def pause(self):
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks: task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _run(self, sid):
        view = self.views[sid]; snapshot = view['snapshot']
        async def quiet_emit(kind, data): pass
        client = None
        try:
            async with self.lock:
                client = self.client_factory(quiet_emit)
                classroom = {'lesson': snapshot['lesson'], 'state': snapshot['state'],
                    'sources': [{key: s[key] for key in ('id', 'text', 'kind', 'event_id')} for s in snapshot['learning_sources']],
                    'evidence': {eid: {key: value for key, value in e.items() if key not in ('raw_text', 'review_event_id')}
                                 for eid, e in snapshot['evidence'].items() if e['kind'] != 'error'},
                    'questions': snapshot['questions']}
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
                            prompt, payload, schema = report_prompts.DIAGNOSE, {**classroom, 'verification': view['verification'],
                                'settlement': snapshot['settlement'], 'limitation': LIMITATION}, Diagnosis
                        async with asyncio.timeout(60):
                            result = await client.generate('report_v2_' + phase, prompt, payload, schema)
                        result = schema.model_validate(result).model_dump()
                        if phase == 'recall': validate_recall(result, snapshot)
                        elif phase == 'answers': validate_answers(result, view['recall'], snapshot)
                        elif phase == 'verification': validate_verification(result, view['recall'], view['answers'], snapshot)
                        else: validate_diagnosis(result, snapshot)
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
