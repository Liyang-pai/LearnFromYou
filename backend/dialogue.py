"""实时课堂与历史共用的批次和学生视图投影；不调用模型。"""
import hashlib
import json

from .schemas import StudentState


def student_segments(data):
    """Read only the current student input, including multimodal/repair requests."""
    if data.get('phase') != 'student':
        return []
    for message in reversed(data.get('body', {}).get('messages', [])):
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        parts = [content] if isinstance(content, str) else [
            part.get('text') for part in content or [] if isinstance(part, dict) and part.get('type') == 'text']
        for part in parts:
            try:
                payload = json.loads(part)
            except (ValueError, TypeError):
                continue
            if not isinstance(payload, dict) or 'new_teacher_segments' not in payload:
                continue
            segments = payload['new_teacher_segments']
            if not isinstance(segments, list) or any(
                not isinstance(segment, dict) or not isinstance(segment.get('id'), str)
                or not segment['id'] or not isinstance(segment.get('text'), str) for segment in segments
            ) or len({segment['id'] for segment in segments}) != len(segments):
                raise ValueError('Invalid teacher batch')
            return segments
    return []


class HistoryDialogue:
    """Keep first-request order and exact submitted wording; transcripts are provenance."""
    def __init__(self, session_id=''):
        self.session_id = session_id
        self.rows, self.sources, self.batches = [], {}, {}
        self.submitted = set()
        self.active = None

    def transcript(self, event):
        data = event['data']
        text, source_id = data['text'], data['id']
        if not isinstance(text, str) or not isinstance(source_id, str) or source_id in self.sources:
            raise ValueError('Invalid transcript')
        row = {'role': 'teacher', 'text': text, 'raw_text': text, 'source_ids': [source_id],
               'time': event.get('time'), 'elapsed': event.get('elapsed'),
               'processing_status': 'unconfirmed', 'request_count': 0}
        self.sources[source_id] = row
        self.rows.append(row)

    def request(self, event):
        segments = student_segments(event['data'])
        if not segments:
            return
        signature = tuple((segment['id'], segment['text']) for segment in segments)
        row = self.batches.get(signature)
        if row is None:
            identity = json.dumps([self.session_id, signature], ensure_ascii=False, separators=(',', ':'))
            originals = []
            for segment in segments:
                source = self.sources.get(segment['id'], {})
                originals.append({'source_id': segment['id'], 'text': segment['text'],
                                  'raw_text': source.get('raw_text'), 'time': source.get('time'),
                                  'elapsed': source.get('elapsed', segment.get('at'))})
            row = {'role': 'teacher', 'batch_id': hashlib.sha256(identity.encode()).hexdigest()[:24],
                   'text': '\n'.join(segment['text'] for segment in segments),
                   'source_ids': [segment['id'] for segment in segments], 'segments': originals,
                   'time': event.get('time'), 'elapsed': event.get('elapsed'), 'request_count': 0,
                   'had_failure': False}
            self.batches[signature] = row
            self.rows.append(row)
        row['request_count'] += 1
        row['processing_status'] = 'submitted'
        self.submitted.update(row['source_ids'])
        self.active = row
        return row

    def processed(self, data):
        if self.active is not None and set(self.active['source_ids']) == set(data.get('processed_sources', [])):
            row, self.active = self.active, None
            row['processing_status'] = 'processed'
            return row

    def failed(self, data):
        if data.get('code') != 'model_failure' or self.active is None:
            return
        if set(data.get('sources') or []) != set(self.active['source_ids']):
            return
        self.active.update(processing_status='failed', had_failure=True)
        return self.active

    def finish(self):
        return [row for row in self.rows if row.get('request_count', 0) or row['role'] != 'teacher'
                or row['source_ids'][0] not in self.submitted]


class StudentView:
    """Only validated state and actually posted questions can enter the readable view."""
    def __init__(self):
        self.state = StudentState()
        self.questions = []

    def consume(self, kind, data):
        update = kind in ('ready', 'state', 'finished') or (
            kind == 'knowledge_review' and data.get('status') == 'completed')
        # These events change the authoritative state without adding teacher input.
        update |= kind == 'reply' or kind == 'invitation_expired' or (
            kind == 'resume_response' and data.get('status') in ('generated', 'skipped'))
        changed = False
        if update and isinstance(data.get('state'), dict):
            try:
                self.state = StudentState.model_validate(data['state'])
                changed = True
            except ValueError:
                pass  # Keep the last validated snapshot when a legacy/damaged event appears.
        if kind == 'reply' and data.get('kind') == 'question':
            text = data.get('text')
            if isinstance(text, str) and text.strip() and text not in self.questions:
                self.questions.append(text)
                changed = True
        return self.value() if changed else None

    def value(self):
        context = self.state.learning_context()
        return {'understood': [k['text'] for k in context['knowledge']],
                'tentative': [k['text'] for k in context['tentative_knowledge']],
                'unclear': [k['text'] for k in context['inactive_knowledge'] if k['status'] != 'invalid'],
                'questions': list(self.questions)}
