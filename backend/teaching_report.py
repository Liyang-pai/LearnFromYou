"""A teaching report is independent of the AI student's post-class simulation."""
import asyncio
import hashlib
import json
import logging
from pathlib import Path
import re
from . import config
from .llm import ModelClient, ModelError
from .report_prompt import TEACHING_REPORT, ANALYZE_CHUNK, MERGE_NOTES
from .report_contract import TeachingReport, DIMENSION_NAMES, validate_sources as check_sources

REPORT_BUDGET = 300


def read_classroom(root: Path, session_id: str):
    if not re.fullmatch(r'[0-9a-f]{12}', session_id):
        raise ValueError('课堂编号无效')
    teachers, replies, lesson, finished, timeline = {}, [], {}, None, []
    with (root / 'logs' / f'{session_id}.jsonl').open(encoding='utf-8-sig') as stream:
        for line in stream:
            if not line.strip():
                continue
            event = json.loads(line)
            kind, data = event.get('type'), event.get('data', {})
            if kind in ('ready', 'finished') and data.get('session_id') != session_id:
                raise ValueError('课堂记录编号不一致')
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
                timeline.append(('teacher', source_id))
            elif kind == 'review_completed':
                for segment in data.get('segments', []):
                    if segment['id'] in teachers:
                        teachers[segment['id']]['text'] = segment['text']
            elif kind == 'reply' and data.get('kind') in ('question', 'answer', 'ack') and data.get('text', '').strip():
                replies.append({'id': f'r{len(replies) + 1}', 'kind': data['kind'], 'text': data['text']})
                timeline.append(('ai', len(replies) - 1))
            elif kind == 'finished':
                finished = data
                lesson = data.get('lesson', lesson)
    if finished is None:
        raise ValueError('请先结束试讲，再生成教学报告')
    if not teachers:
        raise ValueError('本次没有教师讲授内容，无法生成教学报告')
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
    return {'lesson': lesson, 'teacher_segments': list(teachers.values()), 'ai_replies': replies,
            'facts': facts, 'turns': turns}


def source_hash(classroom, prompt=None):
    # Changes to the report contract must not silently reuse an older report.
    content = {'classroom': classroom, 'version': 2, 'prompt': TEACHING_REPORT if prompt is None else prompt,
               'chunk_prompt': ANALYZE_CHUNK, 'merge_prompt': MERGE_NOTES,
               'schema': TeachingReport.model_json_schema()}
    return hashlib.sha256(json.dumps(content, ensure_ascii=False, sort_keys=True).encode('utf-8')).hexdigest()


def validate_sources(report, classroom):
    teachers = {s['id'] for s in classroom['teacher_segments']}
    check_sources(report, teachers)


def export_markdown(result):
    if result['status'] != 'ready' or not result['report']:
        raise ValueError('教学报告尚未生成成功，不能导出')
    report = result['report']
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

    def path(self, session_id):
        return config.ROOT / 'logs' / f'{session_id}.teaching-report-v2.json'

    def save(self, result):
        path = self.path(result['session_id'])
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
        temporary.replace(path)

    def get(self, session_id):
        classroom = read_classroom(config.ROOT, session_id)
        digest = source_hash(classroom)
        result = {'schema_version': 2, 'session_id': session_id, 'source_hash': digest, 'status': 'idle',
                  'progress': '课堂已结束，可以生成报告。', 'report': None, 'error': ''}
        path = self.path(session_id)
        if path.exists():
            try:
                saved = json.loads(path.read_text(encoding='utf-8'))
                if not isinstance(saved, dict):
                    raise ValueError('Invalid cache')
                if saved.get('source_hash') == digest and saved.get('schema_version') == 2:
                    if saved['status'] not in ('ready', 'generating', 'failed') or not isinstance(saved['error'], str):
                        raise ValueError('Invalid cache status')
                    result.update(status=saved['status'], report=saved['report'], error=saved['error'],
                                  progress=saved.get('progress', ''))
                    if result['status'] == 'ready':
                        report = TeachingReport.model_validate(result['report'])
                        validate_sources(report, classroom)
                        result['report'] = report.model_dump()
                    task = self.tasks.get(session_id)
                    if result['status'] == 'generating' and (not task or task.done()):
                        result.update(status='failed', report=None, error='上次生成因服务中断而停止，可重试；课堂内容已保留')
            except (ValueError, KeyError, TypeError):
                result.update(status='failed', report=None, error='报告缓存无效，可以重新生成；课堂内容已保留')
        task = self.tasks.get(session_id)
        if task and not task.done():
            result.update(status='generating', report=None, error='')
        return result

    async def start(self, session_id):
        # No await between checking the job and registering it: double clicks share one job.
        result = self.get(session_id)
        if result['status'] in ('ready', 'generating'):
            return result
        classroom = read_classroom(config.ROOT, session_id)
        result.update(status='generating', error='', report=None, progress='正在等待分析，请稍等……')
        self.save(result)
        self.tasks[session_id] = asyncio.create_task(self.run(result, classroom))
        return result

    async def run(self, result, classroom):
        model = None
        try:
            async with self.lock:
                async def audit(kind, data):
                    path = self.path(result['session_id']).with_suffix('.events.jsonl')
                    text = json.dumps({'type': kind, 'data': data}, ensure_ascii=False)
                    if config.API_KEY:
                        text = text.replace(config.API_KEY, '[REDACTED]')
                    with path.open('a', encoding='utf-8') as stream:
                        stream.write(text + '\n')
                model = self.model_factory(audit)
                from .report_pipeline import ReportPipeline
                async with asyncio.timeout(REPORT_BUDGET):
                    pipeline = ReportPipeline(model, config.ROOT, result['session_id'], result['source_hash'],
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
            if config.API_KEY:
                message = message.replace(config.API_KEY, '[REDACTED]')
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
