"""Ordered long-class analysis with durable, input-addressed stage results."""
import hashlib
import json
import re
from pathlib import Path

from .llm import ModelError
from .report_contract import AnalysisNotes, TeachingReport, source_bound
from .report_prompt import ANALYZE_CHUNK, MERGE_NOTES, TEACHING_REPORT

SHORT_LIMIT = 24000
CHUNK_LIMIT = 12000


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def compact(classroom):
    return {'lesson': classroom['lesson'], 'turns': [
        {key: turn[key] for key in ('id', 'role', 'text', 'mode', 'kind') if key in turn}
        for turn in classroom['turns']]}


def split_text(text, limit=2000):
    """Prefer sentence boundaries; hard-split a huge sentence without losing characters."""
    while len(text) > limit:
        boundaries = list(re.finditer(r'[。！？!?\n]', text[:limit]))
        end = boundaries[-1].end() if boundaries else limit
        yield text[:end]
        text = text[end:]
    if text:
        yield text


def chunks(payload):
    lesson = payload['lesson']
    if len(encode(lesson)) > CHUNK_LIMIT // 2:
        raise ModelError('教学设置过长，无法在分析窗口中完整保留；请缩短设置后重试')
    current, previous_teacher = [], None
    for turn in payload['turns']:
        for text in split_text(turn['text']):
            part = {**turn, 'text': text}
            candidate = {'lesson': lesson, 'turns': [*current, part]}
            if current and len(encode(candidate)) > CHUNK_LIMIT:
                yield {'lesson': lesson, 'turns': current}
                current = []
                if part['role'] == 'ai' and previous_teacher:
                    # Context was already analyzed in full. Repeating it makes an
                    # AI-only continuation interpretable without inventing a source.
                    current.append({**previous_teacher, 'context_only': True})
            current.append(part)
            if part['role'] == 'teacher':
                previous_teacher = part
    if current:
        yield {'lesson': lesson, 'turns': current}


def note_sources(payload):
    return {sid for group in payload['groups'] for note in group['notes'] for sid in note['source_ids']}


def batches(groups):
    current = []
    for group in groups:
        if current and len(encode({'groups': [*current, group]})) > CHUNK_LIMIT:
            yield current
            current = []
        current.append(group)
    if current:
        yield current


class ReportPipeline:
    def __init__(self, model, root: Path, sid, digest, progress):
        self.model, self.progress = model, progress
        # Keep paths short on Windows; full hashes in each file still guard reuse.
        self.directory = root / 'logs' / f'{sid}.report-v2-stages' / digest[:16]
        self.directory.mkdir(parents=True, exist_ok=True)

    async def stage(self, phase, prompt, payload, schema, allowed):
        bound = source_bound(schema, allowed)
        key = hashlib.sha256(encode({'phase': phase, 'prompt': prompt, 'payload': payload,
                                    'schema': schema.model_json_schema()}).encode('utf-8')).hexdigest()
        path = self.directory / (key[:24] + '.json')
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved['key'] == key:
                    return bound.model_validate(saved['result'])
            except (ValueError, TypeError, KeyError):
                pass  # A damaged stage is regenerated; other valid stages remain usable.
        result = await self.model.generate(phase, prompt, payload, bound)
        result = bound.model_validate(result.model_dump())
        temporary = path.with_suffix('.tmp')
        temporary.write_text(encode({'key': key, 'result': result.model_dump()}), encoding='utf-8')
        temporary.replace(path)
        return result

    async def generate(self, classroom):
        payload = compact(classroom)
        teachers = {s['id'] for s in classroom['teacher_segments']}
        if len(encode(payload)) <= SHORT_LIMIT:
            self.progress('正在分析讲授内容与改进建议……')
            return await self.stage('teaching_report', TEACHING_REPORT, payload, TeachingReport, teachers)
        parts = list(chunks(payload))
        groups = []
        for i, part in enumerate(parts):
            self.progress(f'正在分析长课内容 {i + 1}/{len(parts)}……')
            allowed = {t['id'] for t in part['turns'] if t['role'] == 'teacher'}
            notes = await self.stage('teaching_report_chunk', ANALYZE_CHUNK, part, AnalysisNotes, allowed)
            groups.append({'range': [i, i], **notes.model_dump()})
        # Coverage is tracked by the program, rather than trusting a model's claim.
        level = 0
        while len(encode({'lesson': payload['lesson'], 'groups': groups})) > SHORT_LIMIT:
            level += 1
            merged = []
            for i, batch in enumerate(batches(groups)):
                self.progress(f'正在整理全课前后关系（第{level}轮）……')
                merge_payload = {'groups': batch}
                notes = await self.stage('teaching_report_merge', MERGE_NOTES, merge_payload,
                                         AnalysisNotes, note_sources(merge_payload))
                merged.append({'range': [batch[0]['range'][0], batch[-1]['range'][1]], **notes.model_dump()})
            if len(encode(merged)) >= len(encode(groups)):
                raise ModelError('长课笔记未能有效压缩；成功阶段已保留，可重试继续，未截断课堂')
            groups = merged
        assert groups[0]['range'][0] == 0 and groups[-1]['range'][1] == len(parts) - 1
        self.progress('正在核对全课解释、更正与优先改进……')
        final_payload = {'lesson': payload['lesson'], 'groups': groups}
        return await self.stage('teaching_report', TEACHING_REPORT, final_payload, TeachingReport,
                                note_sources(final_payload))
