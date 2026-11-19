"""A teaching report is independent of the AI student's post-class simulation."""
import asyncio
from collections import OrderedDict
import threading
import hashlib
from html import escape
import json
import logging
import math
from pathlib import Path
import re
from . import config
from .llm import ModelClient, ModelError
from .report_prompt import TEACHING_REPORT, ANALYZE_CHUNK, MERGE_NOTES, REFLECTION_REPORT, REFLECTION_NOTES
from .report_contract import (TeachingReport, ReflectionReport, DIMENSION_NAMES, evidence_turns,
                              validate_sources as check_sources, validate_grounding, validate_reflection)

REPORT_BUDGET = 300
SCORE_NAMES = (*DIMENSION_NAMES[:3], ('interaction', '师生互动'), ('expression', '表达规范'))
FILLER_WORDS = ('这个', '就是', '呃', '嗯')


def recorded_elapsed(event):
    value = event.get('elapsed')
    if type(value) in (float, int) and 0 <= value < math.inf:
        return {'elapsed': value}
    return {}


def sound_check(text):
    text = re.sub(r'[\s，。！？、,.!?]+', '', text)
    return not text or bool(re.fullmatch(
        r'(?:好|喂+|试音|测试麦克风|(?:老师|同学们|大家)?(?:你好|您好|好)|'
        r'(?:同学们?|大家|小纸)?(?:能听到|听得到)(?:我说话|我|声音)?吗|'
        r'(?:今天)?(?:开始|结束)(?:试讲|讲课|上课))+', text))


def read_classroom(root: Path, session_id: str):
    if not re.fullmatch(r'[0-9a-f]{12}', session_id):
        raise ValueError('课堂编号无效')
    teachers, replies, lesson, finished, timeline = {}, [], {}, None, []
    metadata, reply_questions = {}, {}
    timing, originals, finish_time = {}, {}, {}
    started = microphone_enabled = audio_received = capture_error = False
    with (root / 'logs' / f'{session_id}.jsonl').open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            kind, data = event.get('type'), event.get('data', {})
            if kind in ('ready', 'finished') and data.get('session_id') != session_id:
                raise ValueError('课堂记录编号不一致')
            if kind == 'ready':
                started = True
            elif kind == 'audio':
                microphone_enabled |= data.get('recording') is True
            elif kind == 'audio_input':
                audio_received = True
            elif kind == 'asr_empty' or (kind == 'error' and data.get('code') in {
                    'audio_backlog', 'audio_failure', 'asr_failure', 'no_audio_frames'}):
                capture_error = True
            if kind == 'llm_request' and data.get('phase') == 'preparation':
                # Older main logs store the lesson only in the preparation input.
                for message in data.get('body', {}).get('messages', []):
                    if message.get('role') == 'user':
                        lesson = json.loads(message['content'])
            elif kind == 'transcript':
                source_id = data['id']
                if source_id in teachers:
                    raise ValueError('课堂讲授编号重复')
                teachers[source_id] = {'id': source_id, 'text': data['text'], 'mode': data.get('mode', 'text')}
                originals[source_id] = data['text']
                timing[source_id] = recorded_elapsed(event)
                if data.get('review_status'):
                    metadata[source_id] = {'review_status': data['review_status']}
                if data.get('image_id'):
                    teachers[source_id]['image_id'] = data['image_id']
                timeline.append(('teacher', source_id))
            elif kind == 'review_completed':
                for segment in data.get('segments', []):
                    if segment['id'] in teachers:
                        teachers[segment['id']]['text'] = segment['text']
                        if segment.get('review_status'):
                            metadata[segment['id']] = {'review_status': segment['review_status']}
            elif kind == 'reply' and data.get('kind') in ('question', 'answer', 'ack') and data.get('text', '').strip():
                replies.append({'id': f'r{len(replies) + 1}', 'kind': data['kind'], 'text': data['text']})
                timing[replies[-1]['id']] = recorded_elapsed(event)
                if data.get('question_id') and data['kind'] == 'question':
                    reply_questions[replies[-1]['id']] = data['question_id']
                timeline.append(('ai', len(replies) - 1))
            elif kind == 'finished':
                finished = data
                finish_time = recorded_elapsed(event)
                lesson = data.get('lesson', lesson)
            elif kind == 'ready' and 'lesson' in data:
                lesson = data['lesson']
    if finished is None:
        raise ValueError('请先结束试讲，再生成教学报告')
    unprocessed = set(finished.get('unprocessed_sources', []))
    for source in teachers.values():
        source['processed'] = source['id'] not in unprocessed
    state = finished.get('state', {})
    questions = state.get('open_questions', state.get('questions', []))
    facts = {'teacher_segments': len(teachers),
             'ai_questions': sum(r['kind'] == 'question' for r in replies),
             'ai_answers': sum(r['kind'] == 'answer' for r in replies),
             'ai_acknowledgements': sum(r['kind'] == 'ack' for r in replies),
             'unprocessed_segments': sum(not s['processed'] for s in teachers.values()),
             'open_ai_questions': sum(q['status'] in ('pending', 'asked', 'deferred') for q in questions)}
    turns = [{**teachers[key], 'role': 'teacher'} if role == 'teacher'
             else {**replies[key], 'role': 'ai'} for role, key in timeline]
    order = {turn['id']: index for index, turn in enumerate(turns)}
    resolved = {q.get('id'): q.get('resolution_sources') or [] for q in questions if q.get('status') == 'resolved'}
    for reply_id, question_id in reply_questions.items():
        later = [sid for sid in resolved.get(question_id, []) if sid in teachers and order[sid] > order[reply_id]]
        if later:
            metadata[reply_id] = {'question_status': 'resolved', 'resolution_sources': later}
    recording_observed = any(t['mode'] == 'microphone' and t['text'].strip() for t in teachers.values())
    capture_incomplete = (not started or capture_error or bool(unprocessed - teachers.keys())
                          or ((microphone_enabled or audio_received) and not recording_observed))
    no_lesson = all(sound_check(text) for text in originals.values())
    transcript = [
        {'id': row['id'], 'role': row['role'], 'text': row['text'], **timing[row['id']],
         **({'original_text': originals[row['id']]}
            if row['role'] == 'teacher' and originals[row['id']] != row['text'] else {})}
        for row in turns
    ]
    voice_text = [originals[row['id']] for row in turns
                  if row['role'] == 'teacher' and row['mode'] == 'microphone']
    words = [{'word': word, 'count': sum(text.count(word) for text in voice_text)} for word in FILLER_WORDS]
    presentation = {
        'transcript': transcript, 'word_counts': sorted(words, key=lambda item: -item['count']),
        'capture_status': 'incomplete' if capture_incomplete else 'empty' if no_lesson else 'complete',
        **finish_time,
    }
    return {'lesson': lesson, 'teacher_segments': list(teachers.values()), 'ai_replies': replies,
            'facts': facts, 'turns': turns, 'evidence': metadata, 'presentation': presentation,
            'no_lesson': no_lesson}


def source_hash(classroom, prompt=None, *, legacy=False):
    # Changes to the report contract must not silently reuse an older report.
    classroom = {key: value for key, value in classroom.items() if key not in ('presentation', 'no_lesson')}
    if legacy:
        classroom = {key: value for key, value in classroom.items() if key != 'evidence'}
    schema = TeachingReport if legacy else ReflectionReport
    notes = '' if legacy else REFLECTION_NOTES
    content = {'classroom': classroom, 'version': 2 if legacy else 3,
               'prompt': prompt if prompt is not None else TEACHING_REPORT if legacy else REFLECTION_REPORT,
               'chunk_prompt': ANALYZE_CHUNK + notes, 'merge_prompt': MERGE_NOTES + notes,
               'schema': schema.model_json_schema()}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def validate_sources(report, classroom):
    teachers = {s['id'] for s in classroom['teacher_segments']}
    check_sources(report, teachers)
    if isinstance(report, ReflectionReport):
        validate_reflection(report, teachers, evidence_turns(classroom))
    elif 'turns' in classroom:
        validate_grounding(report, classroom['turns'])


def public_reflection(report):
    display = report.model_dump(exclude={'assessment'})
    if report.expression:
        display['expression']['quote'] = report.expression.evidence[0].quote
    return display


def status_scores(score, reason):
    return {key: {'score': score, 'reason': reason, 'evidence': []} for key, _ in SCORE_NAMES}


def export_reflection(report):
    lines = ['# 试讲教学报告', '', '## 本次状态', '', escape(report['overview']['text'])]
    if report['keep']:
        lines.extend(['', '**值得保留**　' + escape(report['keep']['text'])])
    if report['findings']:
        lines.extend(['', '## 改进建议'])
        for finding in report['findings']:
            lines.extend(['', f"### {finding['kind']} · {escape(finding['title'])}", '',
                          '**课堂依据**　' + escape(finding['fact']), '',
                          '**下次做法**　' + escape(finding['action'])])
    if report['expression']:
        item = report['expression']
        lines.extend(['', '## 表达观察', '', escape(item['text']), '',
                      '转写片段　“' + escape(item['quote']) + '”', '', escape(item['action'])])
    if report['practice']:
        practice = report['practice']
        lines.extend(['', '## 下次试试', '', f"{practice['minutes']}分钟练习　{escape(practice['task'])}", '',
                      escape(practice['example']), '', '**完成标准**　' + escape(practice['criterion'])])
    return '\n'.join([*lines, '', '报告依据课堂文字和小纸的模拟回应。', ''])


def export_markdown(result):
    if result['status'] != 'ready' or not result['report']:
        raise ValueError('教学报告尚未生成成功，不能导出')
    report = result['report']
    if 'findings' in report:
        text = export_reflection(report)
        if 'scores' in result:
            lines = ['', '## 五维评价', '']
            for key, name in SCORE_NAMES:
                row = result['scores'][key]
                grade = '资料待核对' if row['score'] is None else f"{row['score']:.1f} / 5.0"
                lines.append(f"- {name} · {grade}　{escape(row['reason'])}")
            text += '\n'.join([*lines, ''])
        return text
    lines = ['# 试讲教学报告', '', '## 总体概括', '', report['overview']['text'], '']
    for key, name in DIMENSION_NAMES:
        dim = report['dimensions'][key]
        lines.append(f"- {name} · {dim['label']}：{dim['reason']}")
    finding, practice = report['finding'], report['practice']
    lines += ['', '## 最值得注意的发现', '', f"{finding['kind']}：{finding['text']}",
              f"影响：{finding['impact']}", '',
              '值得保留：' + (report['keep']['text'] if report['keep'] else '材料不足，暂不单列优点。'),
              '', '## 应该如何改', '', report['action']['text'], '', '## 下次怎么办', '',
              f"{practice['minutes']}分钟练习：{practice['task']}", '', f"完成标准：{practice['criterion']}", '']
    lines += ['说明：报告基于课堂文本，不代表真实学生掌握。', '']
    return '\n'.join(lines)


class ReportService:
    def __init__(self, model_factory=None):
        self.model_factory = model_factory or ModelClient
        self.tasks = {}
        self.lock = asyncio.Lock()
        self.start_lock = asyncio.Lock()
        self.materials = OrderedDict()
        self.material_lock = threading.RLock()

    def path(self, session_id):
        return config.data_root() / 'logs' / f'{session_id}.teaching-report-reflection.json'

    def save(self, result):
        path = self.path(result['session_id'])
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)

    def material(self, session_id):
        # Source and prompt changes invalidate material/hash together. Polling never
        # rereads an unchanged raw log; cap memory retained across historical classes.
        from .history import classroom_path, source_stamp
        path = classroom_path(session_id)
        key = (str(path), source_stamp(path), source_hash({}))
        with self.material_lock:
            if key not in self.materials:
                classroom = read_classroom(config.data_root(), session_id)
                self.materials[key] = (classroom, source_hash(classroom))
                while len(self.materials) > 8:
                    self.materials.popitem(last=False)
            self.materials.move_to_end(key)
            return self.materials[key]

    def get(self, session_id):
        classroom, digest = self.material(session_id)
        result = {'schema_version': 3, 'session_id': session_id, 'source_hash': digest, 'status': 'idle',
                  'progress': '课堂已结束，可以生成报告。', 'report': None, 'error': '',
                  'topic': classroom['lesson'].get('topic', ''),
                  'grade': classroom['lesson'].get('grade', ''), 'subject': classroom['lesson'].get('subject', ''),
                  **classroom['presentation']}
        if classroom['no_lesson']:
            incomplete = result['capture_status'] == 'incomplete'
            reason = ('课堂采集记录不完整，资料待核对。' if incomplete else
                      '本次试讲已结束，课堂记录中没有可评价的讲授内容。')
            result.update(status='ready', progress='报告已生成。',
                          report={'overview': {'text': reason, 'evidence': []}, 'keep': None,
                                  'findings': [], 'expression': None, 'practice': None},
                          scores=status_scores(None if incomplete else 1.0, reason))
            return result
        path = self.path(session_id)
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(saved, dict):
                    raise ValueError('Invalid cache')
                if saved.get('source_hash') == digest and saved.get('schema_version') == 3:
                    if saved['status'] not in ('ready', 'generating', 'failed') or not isinstance(saved['error'], str):
                        raise ValueError('Invalid cache status')
                    result.update(status=saved['status'], report=saved['report'], error=saved['error'],
                                  progress=saved.get('progress', ''))
                    if result['status'] == 'ready':
                        from .report_pipeline import ScoredReflectionReport
                        from .report_scoring import score_assessment
                        cached = result['report']
                        assessment = cached.get('assessment') if isinstance(cached, dict) else None
                        if assessment is not None:
                            report = ScoredReflectionReport.model_validate(cached)
                            assessment = report.assessment
                        else:
                            report = ReflectionReport.model_validate(
                                {key: value for key, value in cached.items() if key != 'assessment'}
                                if isinstance(cached, dict) else cached)
                        validate_sources(report, classroom)
                        result['report'] = public_reflection(report)
                        if result['capture_status'] == 'incomplete':
                            result['scores'] = status_scores(None, '课堂采集记录不完整，资料待核对。')
                        elif assessment is not None:
                            result['scores'] = score_assessment(assessment, evidence_turns(classroom))
                    task = self.tasks.get(session_id)
                    if result['status'] == 'generating' and (not task or task.done()):
                        result.update(status='failed', report=None, error='上次生成因服务中断而停止，可重试；课堂内容已保留')
            except (ValueError, KeyError, TypeError):
                result.update(status='failed', report=None, error='报告缓存无效，可以重新生成；课堂内容已保留')
        if result['status'] == 'idle':
            legacy_path = path.with_name(f'{session_id}.teaching-report-v2.json')
            if legacy_path.is_file():
                try:
                    saved = json.loads(legacy_path.read_text(encoding='utf-8'))
                    if (saved['status'] == 'ready' and saved['schema_version'] == 2
                            and saved['source_hash'] == source_hash(classroom, legacy=True)):
                        report = TeachingReport.model_validate(saved['report'])
                        validate_sources(report, classroom)
                        result.update(schema_version=2, source_hash=saved['source_hash'], status='ready',
                                      report=report.model_dump(exclude={'action_evidence'}), progress='报告已生成。')
                except (ValueError, KeyError, TypeError):
                    result.update(status='failed', report=None, error='旧报告缓存无效，可以重新生成；课堂内容已保留')
        task = self.tasks.get(session_id)
        if task and not task.done():
            result.update(status='generating', report=None, error='')
        return result

    async def start(self, session_id):
        # Reading a large old classroom must not block websocket/audio work.
        # The lock covers job registration after the asynchronous material lookup.
        async with self.start_lock:
            result = await asyncio.to_thread(self.get, session_id)
            if result['status'] in ('ready', 'generating'):
                return result
            classroom, _ = await asyncio.to_thread(self.material, session_id)
            result.update(status='generating', error='', report=None, progress='正在等待分析，请稍等……')
            work = {key: result[key] for key in
                    ('schema_version', 'session_id', 'source_hash', 'status', 'progress', 'report', 'error', 'topic')}
            self.save(work)
            self.tasks[session_id] = asyncio.create_task(self.run(work, classroom))
            # Enter the task's try/finally and freeze its model configuration before
            # returning, including when it must wait for another report's worker.
            await asyncio.sleep(0)
            return result

    async def run(self, result, classroom):
        model = None
        try:
            async def audit(kind, data):
                path = self.path(result['session_id']).with_suffix('.events.jsonl')
                text = json.dumps({'type': kind, 'data': data}, ensure_ascii=False)
                for secret in (config.API_KEY, getattr(model, 'api_key', None)):
                    if secret:
                        text = text.replace(json.dumps(secret, ensure_ascii=False)[1:-1], '[REDACTED]')
                with path.open('a', encoding='utf-8') as stream:
                    stream.write(text + '\n')
            model = self.model_factory(audit)
            async with self.lock:
                from .report_pipeline import ReportPipeline
                async with asyncio.timeout(REPORT_BUDGET):
                    pipeline = ReportPipeline(model, config.data_root(), result['session_id'], result['source_hash'],
                                              lambda message: self.progress(result, message))
                    report = await pipeline.generate(classroom)
                validate_sources(report, classroom)
                result.update(status='ready', report=report.model_dump(), error='', progress='报告已生成。')
        except asyncio.CancelledError:
            result.update(status='failed', report=None, error='生成已中断；课堂内容保留，可以重试')
            raise
        except TimeoutError:
            result.update(status='failed', report=None, error='本次分析已到5分钟执行上限；成功阶段已保存，可重试继续')
        except (ModelError, ValueError) as exc:
            message = str(exc)
            for secret in (config.API_KEY, getattr(model, 'api_key', None)):
                if secret:
                    message = message.replace(secret, '[REDACTED]')
            result.update(status='failed', report=None, error=message)
        except Exception:
            logging.getLogger(__name__).exception('Teaching report generation failed')
            result.update(status='failed', report=None, error='教学报告生成失败；课堂内容保留，请检查服务日志后重试')
        finally:
            self.save(result)
            if model:
                await model.close()

    async def close(self):
        tasks = [task for task in self.tasks.values() if not task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def progress(self, result, message):
        result['progress'] = message
        self.save(result)
