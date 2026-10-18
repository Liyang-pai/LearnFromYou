"""Read-only classroom archives. Raw model requests never enter history responses."""
import asyncio
from collections import OrderedDict
import json
from pathlib import Path
import re
import shutil
import tempfile
import threading

from . import config

VERSION = 2
PAGE_SIZE = 50


def classroom_path(session_id):
    if not re.fullmatch(r'[0-9a-f]{12}', session_id):
        raise ValueError('课堂编号无效')
    path = config.ROOT / 'logs' / f'{session_id}.jsonl'
    # Never follow a log symlink outside the classroom directory.
    if path.resolve().parent != (config.ROOT / 'logs').resolve():
        raise ValueError('课堂记录路径无效')
    if not path.is_file():
        raise FileNotFoundError('未找到这次课堂记录')
    return path


def source_stamp(path):
    stat = path.stat()
    return f'{VERSION}-{stat.st_size}-{stat.st_mtime_ns}'


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def student_segments(data):
    """Read the current student input, never its conversation history or repairs."""
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
    """Keep request order and exact submitted wording; transcripts are provenance."""
    def __init__(self):
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
            originals = []
            for segment in segments:
                source = self.sources.get(segment['id'], {})
                originals.append({'source_id': segment['id'], 'text': segment['text'],
                                  'raw_text': source.get('raw_text'), 'time': source.get('time'),
                                  'elapsed': source.get('elapsed', segment.get('at'))})
            row = {'role': 'teacher', 'text': '\n'.join(segment['text'] for segment in segments),
                   'source_ids': [segment['id'] for segment in segments], 'segments': originals,
                   'time': event.get('time'), 'elapsed': event.get('elapsed'), 'request_count': 0}
            self.batches[signature] = row
            self.rows.append(row)
        row['request_count'] += 1
        row['processing_status'] = 'submitted'
        self.submitted.update(row['source_ids'])
        self.active = row

    def processed(self, data):
        if self.active is not None and set(self.active['source_ids']) == set(data.get('processed_sources', [])):
            self.active['processing_status'] = 'processed'
            self.active = None

    def failed(self, data):
        if data.get('code') != 'model_failure' or self.active is None:
            return
        if data.get('sources') is not None and set(data['sources']) != set(self.active['source_ids']):
            return
        self.active.update(processing_status='failed', had_failure=True)

    def finish(self):
        # Only covered transcript placeholders disappear. Unconfirmed fragments
        # retain their original event position, even between student responses.
        return [row for row in self.rows if row.get('request_count', 0) or row['role'] != 'teacher'
                or row['source_ids'][0] not in self.submitted]


class HistoryService:
    def __init__(self):
        self.jobs = {}
        self.errors = {}
        self.workers = asyncio.Semaphore(2)
        self.summaries = OrderedDict()
        self.cache_lock = threading.RLock()

    def location(self, session_id):
        path = classroom_path(session_id)
        stamp = source_stamp(path)
        return path, stamp, config.ROOT / 'logs' / '.history' / session_id / stamp

    def cached(self, session_id):
        with self.cache_lock:
            return self._cached(session_id)

    def _cached(self, session_id):
        path, stamp, directory = self.location(session_id)
        key = (str(path), stamp)
        meta = self.summaries.get(key)
        if meta is not None:
            self.summaries.move_to_end(key)
            return meta, directory
        try:
            meta = read_json(directory / 'meta.json')
            if meta['version'] != VERSION or meta['stamp'] != stamp:
                return None, directory
            # A partial or damaged cache is rebuilt, never treated as missing dialogue.
            for page in range((meta['turn_count'] + PAGE_SIZE - 1) // PAGE_SIZE):
                if not (directory / f'{page}.json').is_file():
                    return None, directory
            self.summaries[key] = meta
            while len(self.summaries) > 128:
                self.summaries.popitem(last=False)
            return meta, directory
        except (OSError, ValueError, KeyError, TypeError):
            return None, directory

    def build(self, session_id):
        path, stamp, directory = self.location(session_id)
        lesson, dialogue = {}, HistoryDialogue()
        started_at = None
        finished, damaged = False, 0
        with path.open(encoding='utf-8-sig', errors='replace') as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                    data, kind = event.get('data', {}), event.get('type')
                    if not isinstance(data, dict):
                        raise ValueError('Invalid event')
                    if started_at is None and event.get('time'):
                        started_at = event['time']
                    if kind in ('ready', 'finished'):
                        if data.get('session_id') != session_id:
                            raise ValueError('课堂记录编号不一致')
                        if isinstance(data.get('lesson'), dict):
                            lesson = data['lesson']
                        if kind == 'finished':
                            finished = True
                    elif kind == 'llm_request' and data.get('phase') == 'preparation' and not lesson:
                        for message in data.get('body', {}).get('messages', []):
                            if message.get('role') == 'user':
                                value = json.loads(message['content'])
                                if isinstance(value, dict):
                                    lesson = value
                    elif kind == 'llm_request':
                        dialogue.request(event)
                    elif kind == 'transcript':
                        dialogue.transcript(event)
                    elif kind == 'review_completed':
                        for segment in data.get('segments', []):
                            row = dialogue.sources.get(segment['id'])
                            if row is not None and isinstance(segment.get('text'), str):
                                row['text'] = segment['text']
                                row['review_status'] = segment.get('review_status')
                    elif kind == 'state':
                        dialogue.processed(data)
                    elif kind == 'error':
                        dialogue.failed(data)
                    elif kind == 'reply' and data.get('kind') in ('question', 'answer', 'ack'):
                        if isinstance(data.get('text'), str) and data['text'].strip():
                            dialogue.rows.append({'role': 'student', 'text': data['text'],
                                                  'time': event.get('time'), 'elapsed': event.get('elapsed')})
                except (ValueError, KeyError, TypeError, AttributeError):
                    damaged += 1
        turns = dialogue.finish()
        warnings = []
        if not finished:
            warnings.append('课堂未正常结束或仍在进行，仅展示已保存的记录，不支持续讲。')
        if damaged:
            warnings.append(f'{damaged} 条记录无法完整读取，以下仅展示可读取内容。')
        if not finished:
            reason = '课堂未正常结束，暂不能生成报告。'
        elif not dialogue.sources:
            reason = '本次没有教师讲授内容，无法生成报告。'
        elif damaged:
            reason = '课堂记录不完整，暂不能生成报告。'
        else:
            reason = ''
        meta = {'version': VERSION, 'stamp': stamp, 'session_id': session_id,
                'topic': lesson.get('topic') or '未记录', 'grade': lesson.get('grade') or '未记录',
                'subject': lesson.get('subject') or '未记录', 'started_at': started_at,
                'student_profile': lesson.get('student_profile') or '',
                'turn_count': len(turns), 'warnings': warnings, 'report_unavailable': reason}
        directory.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='building-', dir=directory.parent))
        try:
            for index in range(0, len(turns), PAGE_SIZE):
                (temporary / f'{index // PAGE_SIZE}.json').write_text(
                    json.dumps(turns[index:index + PAGE_SIZE], ensure_ascii=False), encoding='utf-8')
            (temporary / 'meta.json').write_text(json.dumps(meta, ensure_ascii=False), encoding='utf-8')
            if source_stamp(path) != stamp:
                return  # The running classroom changed; the next request indexes the new version.
            if directory.exists():
                shutil.rmtree(directory)
            temporary.replace(directory)
            # Keep only the current derived archive; original logs are untouched.
            for old in directory.parent.iterdir():
                if old != directory and old.is_dir():
                    shutil.rmtree(old, ignore_errors=True)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)

    async def prepare(self, session_id):
        async with self.workers:
            try:
                await asyncio.to_thread(self.build, session_id)
                self.errors.pop(session_id, None)
            except (OSError, ValueError):
                self.errors[session_id] = '课堂记录整理失败，请刷新重试并检查文件及磁盘空间。'

    def schedule(self, session_id):
        task = self.jobs.get(session_id)
        if not task or task.done():
            self.jobs[session_id] = asyncio.create_task(self.prepare(session_id))

    async def detail(self, session_id):
        meta, _ = await asyncio.to_thread(self.cached, session_id)
        if meta is None:
            self.schedule(session_id)
            return {'status': 'indexing', 'message': self.errors.get(session_id, '正在整理课堂记录，请稍等……')}
        return {'status': 'ready', **{key: value for key, value in meta.items() if key not in ('version', 'stamp')}}

    async def listing(self, offset=0, limit=20):
        def collect():
            rows, missing = [], []
            for path in (config.ROOT / 'logs').glob('*.jsonl'):
                if not re.fullmatch(r'[0-9a-f]{12}', path.stem):
                    continue
                try:
                    meta, _ = self.cached(path.stem)
                except (OSError, ValueError):
                    continue
                if meta is None:
                    missing.append(path.stem)
                else:
                    rows.append({key: meta[key] for key in ('session_id', 'topic', 'grade', 'subject', 'started_at')})
            rows.sort(key=lambda row: (row['started_at'] or '', row['session_id']), reverse=True)
            return rows, missing
        rows, missing = await asyncio.to_thread(collect)
        for session_id in missing:
            self.schedule(session_id)
        return {'items': rows[offset:offset + limit], 'total': len(rows), 'indexing': bool(missing)}

    async def dialogue(self, session_id, before=None):
        meta, directory = await asyncio.to_thread(self.cached, session_id)
        if meta is None:
            return await self.detail(session_id)
        total = meta['turn_count']
        end = total if before is None else min(before, total)
        start = max(0, end - PAGE_SIZE)
        def read():
            rows = []
            if start < end:
                for index in range(start // PAGE_SIZE, (end - 1) // PAGE_SIZE + 1):
                    rows.extend(read_json(directory / f'{index}.json'))
            return rows[start % PAGE_SIZE:start % PAGE_SIZE + end - start]
        rows = await asyncio.to_thread(read)
        return {'status': 'ready', 'items': rows, 'before': start if start else None, 'total': total,
                'revision': meta['stamp']}

    async def close(self):
        tasks = [task for task in self.jobs.values() if not task.done()]
        await asyncio.gather(*tasks, return_exceptions=True)
        self.jobs.clear()
        self.workers = asyncio.Semaphore(2)
