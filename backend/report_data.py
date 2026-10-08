"""从已结束课堂事件构建不可变报告快照、确定性结算和 ASR 证据链。"""
from copy import deepcopy
import hashlib
import json
import re

REPORT_EVENTS = {'ready', 'transcript', 'review_completed', 'state', 'reply', 'error', 'finished'}


def metric(value, label, method):
    return {'value': value, 'label': label, 'method': method}


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
            evidence[eid] = {**base, 'text': data['text'], 'question_id': data.get('question_id')}
        elif kind in ('state', 'finished'):
            evidence[eid] = {**base, 'text': json.dumps(data.get('state', {}), ensure_ascii=False)}
        elif kind == 'error':
            evidence[eid] = {**base, 'text': data.get('message', '处理异常'), 'code': data.get('code')}
    teachers = [s for s in sources.values() if s['kind'] == 'teacher']
    learning = [s for s in sources.values() if s['processed']]
    asked = {r['question_id'] for r in replies if r.get('kind') == 'question' and r.get('question_id')}
    questions = {q['id']: deepcopy(q) for q in state.get('questions', [])}
    responded = {qid for qid in asked if any(s in sources and sources[s]['kind'] == 'teacher' and sources[s]['processed']
                 for s in questions.get(qid, {}).get('resolution_sources', []))}
    resolved = {qid for qid in asked if questions.get(qid, {}).get('status') == 'resolved'}
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
        'student_questions': metric(len(asked), '学生提出的疑问数', '实际交付 question 的唯一 question_id；同疑问重复追问仅计一个。'),
        'responded_questions': metric(len(responded), '有教师回应证据的疑问', '已提出疑问的 resolution_sources 指向有效教师来源；这是系统可观察的回应证据，不证明理解。'),
        'unanswered_questions': metric(len(asked - responded), '未记录明确回应的疑问', '已提出且没有 resolution_sources 的疑问；不等于确定教师从未回应。'),
        'resolved_questions': metric(len(resolved), '系统标记已解决的疑问', '已实际提出疑问中，最终状态为 resolved 的数量；不是独立验证。'),
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
    snapshot['source_hash'] = hashlib.sha256(json.dumps(snapshot, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    return snapshot
