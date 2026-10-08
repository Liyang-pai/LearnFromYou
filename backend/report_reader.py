"""页面与导出共用的教师阅读层，不修改原始数据，不发起模型请求。"""
from copy import deepcopy


def reader_view(view):
    snapshot = view['snapshot']
    stage = view.get('stages', {}).get('diagnosis', {})
    diagnosis = view.get('diagnosis')
    failed = stage.get('status') == 'failed'
    if diagnosis:
        summary = (diagnosis.get('summary') or {}).get('text') or '旧版报告未生成总体评价；下列逐项评价可查阅课堂依据。'
    elif failed:
        summary = '教学分析生成失败，尚未得到教学评价。这不表示没有教学问题。请点击“重试未完成分析”；已完成内容会复用。'
    else:
        summary = '教学分析尚未完成，暂不能评价本节课的教学表现。'
    metrics = deepcopy(snapshot['settlement']['metrics'])
    replies = metrics.get('student_replies', {}).get('value')
    if 'duration_seconds' in metrics:
        metrics['duration_seconds']['label'] = '会话经过时间（秒）'
    if 'teacher_segments' in metrics:
        metrics['teacher_segments']['label'] = '教师文本片段数'
    for key, item in metrics.items():
        value = item.get('value')
        if 'value' not in item:
            item.update(value=None, state='missing', display='记录缺失')
        elif key == 'verified_understanding':
            item.update(state='not_measured', display='未进行课堂独立验证')
        elif value is None:
            item['state'] = 'unknown'
            item['display'] = '无法统计'
        elif value == 0 and replies == 0 and key not in ('teacher_segments', 'duration_seconds', 'teacher_questions'):
            item['state'] = 'not_occurred'
            item['display'] = '未发生课堂互动'
        else:
            item['state'] = 'observed'
            item['display'] = str(value)
    if metrics.get('teacher_questions', {}).get('value') is None:
        metrics['teacher_questions']['display'] = '未标注，无法可靠统计'
    def finding_refs(f):
        item=deepcopy(f)
        for c in f.get('followup_citations', []):
            if c not in item['citations']: item['citations'].append(c)
        return item
    issues = [finding_refs(f) for f in (diagnosis or {}).get('weaknesses', [])]
    suggestions = {s['finding_id']:s for s in (diagnosis or {}).get('suggestions', [])}
    issues.sort(key=lambda f: 0 if suggestions.get(f['id'], {}).get('priority') == '优先' else 1)
    student = ('没有课堂学生发言，真实学生的理解证据不足。教师自问自答不能代替学生作答。'
               if replies == 0 else ('课堂互动记录缺失，不能评价真实学生理解。' if replies is None else f'课堂有 {replies} 次 AI 学生发言；这是模拟互动，没有真实学生作答，不能推断真实学习效果。'))
    student += ' 课后复述与作答属于 AI 模拟，单题正确不代表整个知识点掌握。'
    counts = {}
    for result in (view.get('verification') or {}).get('results', []):
        counts[result['status']] = counts.get(result['status'], 0) + 1
    return {'summary':summary, 'summary_citations':(diagnosis or {}).get('summary', {}).get('citations', []) if (diagnosis or {}).get('summary') else [],
            'diagnosis_failed':failed, 'error':stage.get('error', ''),
            'strengths':[finding_refs(f) for f in (diagnosis or {}).get('strengths', [])[:3]],
            'issues':[{**f, 'suggestion':suggestions.get(f['id'])} for f in issues[:2]],
            'student':student, 'verification_counts':counts, 'metrics':metrics}
