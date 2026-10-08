"""从已结束课堂事件构建不可变报告快照、确定性结算和 ASR 证据链。"""
from copy import deepcopy
import hashlib
import json
import re

REPORT_EVENTS = {'ready', 'transcript', 'review_completed', 'state', 'reply', 'error', 'finished'}


def metric(value, label, method):
    return {'value': value, 'label': label, 'method': method}


def refresh_question_settlement(snapshot):
    """Recompute observable question states, including legacy caches, without changing cognition."""
    questions = {q['id']: q for q in snapshot['questions']}
    evidence = snapshot['evidence']
    sources = {s['id']: s for s in snapshot['sources']}
    mentions, unmatched = [], []
    explicit = re.compile(r'没[有弄]*明白|不[清确]楚|困惑|疑惑|不理解|[？?]|^(为什么|怎么|如何|哪[里个])')
    for e in evidence.values():
        if e['kind'] != 'reply': continue
        qid = e.get('question_id')
        # A delivered question ID is reliable; other reply kinds still contain real doubts.
        if qid in questions and (e.get('reply_kind') == 'question' or explicit.search(e['text'])):
            mentions.append({'event_id': e['event_id'], 'question_id': qid, 'text': e['text']})
            continue
        spans = [p for p in re.split(r'(?<=[。！？?])', e['text']) if explicit.search(p)
                 and not re.search(r'没有疑问|没什么疑问|没有不明白|没有困惑', p)]
        matched = set()
        for span in spans:
            # Require a shared concrete topic phrase, never merge solely because both are questions.
            candidates = []
            for q in questions.values():
                topic = q.get('topic', q['text'])
                phrases = {topic[i:i+3] for i in range(max(0, len(topic)-2))}
                if any(p in span for p in phrases): candidates.append(q['id'])
            if len(candidates) == 1:
                if candidates[0] not in matched:
                    mentions.append({'event_id': e['event_id'], 'question_id': candidates[0], 'text': span})
                    matched.add(candidates[0])
            else: unmatched.append({'event_id': e['event_id'], 'text': span})
    asked = {m['question_id'] for m in mentions}
    audits = []
    for q in questions.values():
        first = min((evidence[m['event_id']]['order'] for m in mentions if m['question_id'] == q['id']), default=float('inf'))
        responses, explanations = [], []
        for sid in dict.fromkeys(q.get('resolution_sources', []) + q.get('sources', [])):
            s = sources.get(sid)
            if not s or s['kind'] != 'teacher' or not s['processed'] or evidence[s['event_id']]['order'] <= first: continue
            deferred = bool(re.search(r'下[节次回]|以后|先记|暂缓|先不展开', s['text']))
            if sid in q.get('resolution_sources', []) or deferred:
                responses.append(sid)
                if sid in q.get('resolution_sources', []) and not deferred: explanations.append(sid)
        audits.append({**deepcopy(q), 'expressed': q['id'] in asked, 'response_sources': responses,
                       'explanation_sources': explanations})
    responded = {q['id'] for q in audits if q['expressed'] and q['response_sources']}
    explained = {q['id'] for q in audits if q['expressed'] and q['explanation_sources']}
    resolved = {q['id'] for q in audits if q['expressed'] and q['status'] == 'resolved'}
    metrics = snapshot['settlement']['metrics']
    metrics.update({
        'student_questions': metric(None if unmatched else len(asked), '学生独立疑问数量', '检查全部学生发言，复用唯一疑问对象；无法可靠关联的显式疑问不强行合并，数量显示无法统计。'),
        'question_occurrences': metric(len(mentions)+len(unmatched), '学生疑问出现次数', '按发言中的疑问计数，同一发言关联同一疑问仅计一次；重复追问可多次出现，含未能关联的疑问片段。'),
        'responded_questions': metric(len(responded), '已记录教师回应的疑问', '疑问提出之后的解释来源或同疑问暂缓回应；回应不等于解释、解决或理解。'),
        'explained_questions': metric(len(explained), '已记录内容解释的疑问', '提出后有效 resolution_sources，排除仅暂缓处理的来源；解释仍不证明理解。'),
        'unanswered_questions': metric(len(asked-responded), '已关联疑问中未记录回应', '仅统计已关联对象；未回应为零不表示全部解决，未关联疑问另列限制。'),
        'resolved_questions': metric(len(resolved), '系统标记已解决的疑问', '最终 resolved 状态，仅为系统标记，不是独立验证。'),
        'unresolved_questions': metric(len(asked-resolved), '系统尚未标记解决的疑问', '包括已回应但暂缓或仍未解决的疑问，与未记录回应分开统计。'),
    })
    snapshot['settlement'].update(questions=audits, question_mentions=mentions, unmatched_questions=unmatched)
    note = '疑问状态分别记录提出、回应、内容解释、系统解决；课堂理解未独立验证。未回应为零不表示全部解决。'
    if note not in snapshot['settlement']['limitations']: snapshot['settlement']['limitations'].append(note)
    if unmatched:
        note = f'有 {len(unmatched)} 个显式疑问片段无法可靠关联独立疑问对象；已关联独立疑问至少 {len(asked)} 个，未回应统计不覆盖这些片段。'
        if note not in snapshot['settlement']['limitations']: snapshot['settlement']['limitations'].append(note)


def build_snapshot(session_id, events, lesson=None):
    if not re.fullmatch(r'[a-f0-9]{12}', session_id):
        raise ValueError('课堂编号无效')
    unique = {}
    for index, original in enumerate(events):
        event = deepcopy(original)
        event_id = event.setdefault('id', f'{session_id}:e{index+1:06d}')
        if not event_id.startswith(session_id + ':'):
            raise ValueError('证据不属于本次课堂')
        if event_id in unique and unique[event_id] != event:
            raise ValueError('同一事件编号出现不同内容')
        unique[event_id] = event
    records = list(unique.values())
    finished_event = next((e for e in reversed(records) if e['type'] == 'finished'), None)
    if not finished_event or finished_event['data'].get('session_id') != session_id:
        raise ValueError('课堂尚未结束或编号不一致')
    finished = finished_event['data']
    state = deepcopy(finished['state'])
    ready = next((e for e in records if e['type'] == 'ready'), None)
    unprocessed = set(finished.get('unprocessed_sources', []))
    sources, evidence, replies = {}, {}, []
    for order, event in enumerate(records, 1):
        kind, data, eid = event['type'], event['data'], event['id']
        base = {'event_id': eid, 'kind': kind, 'order': order, 'at': event.get('elapsed'), 'time': event.get('time')}
        if kind == 'ready':
            for p in data.get('prerequisites', []):
                sources[p['id']] = {**deepcopy(p), 'event_id': eid, 'processed': True, 'kind': 'prerequisite'}
            evidence[eid] = {**base, 'text': '\n'.join(p['text'] for p in data.get('prerequisites', []))}
        elif kind == 'transcript':
            source = {**deepcopy(data), 'event_id': eid, 'kind': 'teacher',
                      'processed': data['id'] not in unprocessed}
            source.setdefault('raw_text', data['text'])
            sources[data['id']] = source
            evidence[eid] = {**base, 'text': source['text'], 'raw_text': source['raw_text'], 'source_id': data['id']}
        elif kind == 'review_completed':
            for reviewed in data.get('segments', []):
                if reviewed['id'] not in sources:
                    raise ValueError('审核记录缺少原始转写')
                source = sources[reviewed['id']]
                source.update(deepcopy(reviewed), review_event_id=eid)
                evidence[source['event_id']].update(text=source['text'], raw_text=source['raw_text'],
                    review_event_id=eid, review_status=source.get('review_status'))
            evidence[eid] = {**base, 'text': '\n'.join(s['text'] for s in data.get('segments', []))}
        elif kind == 'reply':
            replies.append({**deepcopy(data), 'event_id': eid})
            evidence[eid] = {**base, 'text': data['text'], 'question_id': data.get('question_id'), 'reply_kind': data.get('kind')}
        elif kind in ('state', 'finished'):
            evidence[eid] = {**base, 'text': json.dumps(data.get('state', {}), ensure_ascii=False)}
        elif kind == 'error':
            evidence[eid] = {**base, 'text': data.get('message', '处理异常'), 'code': data.get('code')}
    teachers = [s for s in sources.values() if s['kind'] == 'teacher']
    learning = [s for s in sources.values() if s['processed']]
    questions = {q['id']: deepcopy(q) for q in state.get('questions', [])}
    counts = [s.get('question_count') for s in teachers]
    teacher_questions = sum(counts) if all(type(n) is int and 0 <= n <= 50 for n in counts) else None
    duration = None
    if ready and isinstance(ready.get('elapsed'), (int, float)) and isinstance(finished_event.get('elapsed'), (int, float)):
        duration = round(max(0, finished_event['elapsed'] - ready['elapsed']), 2)
    metrics = {
        'duration_seconds': metric(duration, '试讲总时长（秒）', 'ready 到 finished 的经过时间，包含停顿与结束等待，不是纯讲授时长。'),
        'teacher_segments': metric(len(teachers), '教师有效发言片段', '按唯一 t 来源计数，含未处理文本；VAD 短段不等于自然发言轮次。'),
        'teacher_questions': metric(teacher_questions, '教师提问次数', '仅累加教师明确标注的提问次数；任一片段未标注则无法统计，不通过关键词猜测。'),
        'student_replies': metric(len(replies), 'AI 学生发言次数', '只计实际 reply 事件，候选、重试、模型输出不计。'),
        'verified_understanding': metric(None, '课堂理解已验证数量', '普通课堂未独立验证；课后模拟验证另列，不更改学生认知。'),
    }
    points = [deepcopy(k) for k in state.get('knowledge', []) if any(s in sources and sources[s]['kind'] == 'teacher'
              and sources[s]['processed'] for s in k.get('sources', []))]
    failures = [e for e in evidence.values() if e['kind'] == 'error']
    audio_gaps = [e['event_id'] for e in failures if e.get('code') in ('asr_failure', 'audio_failure', 'audio_backlog', 'no_audio_frames')]
    snapshot = {'version': 2, 'session_id': session_id, 'lesson': deepcopy(lesson or {}), 'state': state,
        'sources': list(sources.values()), 'learning_sources': learning, 'evidence': evidence,
        'questions': list(questions.values()), 'settlement': {'metrics': metrics, 'knowledge_points': points,
            'questions': list(questions.values()), 'unprocessed_sources': sorted(unprocessed),
            'processing_errors': failures, 'audio_gap_events': audio_gaps, 'incomplete': bool(unprocessed or audio_gaps),
            'limitations': ['当前理解是课堂推断；沉默不是理解证据。', '错误记录包括已恢复的历史错误，不全部等同于最终失败。']}}
    refresh_question_settlement(snapshot)
    snapshot['source_hash'] = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return snapshot
