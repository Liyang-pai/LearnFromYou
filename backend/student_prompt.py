"""整理发给学生模型的 JSON、来源和发言历史，保持课堂原始状态完整。"""

import json


STUDENT_PHASES = ('student', 'student_resume', 'knowledge_review')


def compact_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def remove_schema_titles(schema):
    """仅处理 Schema 节点，保留属性名和 default 等数据里的 title。"""
    if not isinstance(schema, dict):
        return
    schema.pop('title', None)
    for keyword in ('$defs', 'properties', 'patternProperties', 'dependentSchemas'):
        for child in schema.get(keyword, {}).values():
            remove_schema_titles(child)
    for keyword in ('allOf', 'anyOf', 'oneOf', 'prefixItems'):
        for child in schema.get(keyword, []):
            remove_schema_titles(child)
    for keyword in ('items', 'additionalProperties', 'unevaluatedProperties',
                    'unevaluatedItems', 'propertyNames', 'contains', 'not', 'if', 'then', 'else'):
        remove_schema_titles(schema.get(keyword))


def student_teacher_source(source):
    return {key: source[key] for key in ('id', 'text', 'mode', 'image_id') if key in source}


def compact_learning_context(context):
    """仅修改 learning_context() 返回的独立快照。"""
    records = (context['knowledge'] + context['tentative_knowledge'] + context['inactive_knowledge']
               + [entry['previous'] for entry in context['knowledge_history']])
    for record in records:
        if record.get('conditions') == '':
            record.pop('conditions')
        for key in ('dependencies', 'independent_sources', 'correction_sources', 'supersedes'):
            if record.get(key) == []:
                record.pop(key)
    return context


def student_history(conversation, utterances):
    """补回窗口之外的近期发言，以共享记录的身份区分相同措辞的两次发言。"""
    recent = utterances[-8:]
    recent_ids = {id(reply) for reply in recent}
    visible_ids = {id(turn['content']) for turn in conversation if turn['role'] == 'assistant'}
    result = [{'role': 'assistant', 'content': compact_json(reply)}
              for reply in recent if id(reply) not in visible_ids]
    for turn in conversation:
        if turn['role'] == 'assistant':
            reply = turn['content']
            content = compact_json(reply) if id(reply) in recent_ids else reply['text']
            result.append({'role': 'assistant', 'content': content})
        else:
            result.append(turn)
    return result
