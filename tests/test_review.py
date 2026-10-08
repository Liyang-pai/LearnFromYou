import asyncio
from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import numpy as np
import pytest
from fastapi.testclient import TestClient

from backend import config
from backend.llm import ModelClient, ModelError
from backend.review import TranscriptReviewer
from backend.schemas import Lesson
from backend.session import Session


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(.01)


class ReviewModel:
    def __init__(self, emit=None):
        self.calls = []
        self.review_gate = None
        self.student_gate = None
        self.fail_student = False
        self.fail_glossary = False
        self.review_output = None
        self.review_cancelled = False
        self.closed = False

    async def generate(self, phase, system, payload, output_type):
        self.calls.append((phase, deepcopy(payload)))
        if phase == 'preparation':
            result = {'scope': ['链表'], 'boundary_note': '待学内容不是已知内容'}
        elif phase == 'review_glossary':
            if self.fail_glossary:
                raise ModelError('模拟术语服务失败')
            result = {'terms': ['链表', '未讲授的专用词', 'next 指针']}
        elif phase == 'asr_review':
            if self.review_gate:
                try:
                    await self.review_gate.wait()
                except asyncio.CancelledError:
                    self.review_cancelled = True
                    raise
            result = self.review_output if self.review_output is not None else {
                'segments': [{'id': s['id'], 'text': s['text'].replace('练表', '链表')}
                             for s in payload['new_asr_segments']]}
        else:
            if self.student_gate:
                await self.student_gate.wait()
            if self.fail_student:
                self.fail_student = False
                raise ModelError('模拟学生失败')
            ids = [s['id'] for s in payload['new_teacher_segments']]
            result = {'knowledge_updates': [{'id': 'k' + ids[-1], 'text': '暂定理解',
                'status': 'tentative', 'sources': ids}], 'addressed': True,
                'candidate': {'kind': 'answer', 'text': '我大概理解了。', 'sources': ids}}
        return output_type.model_validate(result)

    async def close(self):
        self.closed = True

    def payloads(self, phase):
        return [p for name, p in self.calls if name == phase]


async def make_session(tmp_path, monkeypatch, model=None, enabled=True):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    events = []
    async def send(event):
        events.append(event)
    session = Session(send, None)
    await session.llm.close()
    session.llm = model or ReviewModel()
    await session.start(Lesson(topic='链表', points='节点与遍历', prerequisites='日常逻辑'),
                        tts=False, asr_review=enabled)
    return session, session.llm, events


def test_default_glossary_and_mixed_segment_isolate_student_inputs(tmp_path, monkeypatch):
    async def run():
        session, model, events = await make_session(tmp_path, monkeypatch)
        try:
            assert next(e['data'] for e in events if e['type'] == 'ready')['asr_review'] is True
            await session.add_transcript('呃，这个练表，然后这个练表。', 'microphone')
            await session.add_transcript('文字练表保持原样')
            await session.add_transcript('练表还有节点。', 'microphone')
            await session.flush()
            await until(lambda: len(session.processed_ids) == 3)
            request = model.payloads('asr_review')[0]
            assert [s['id'] for s in request['new_asr_segments']] == ['t1', 't3']
            assert len(request['current_segment']) == 3
            assert request['previous_teacher_sources'] == []
            assert request['terms'] == ['链表', '未讲授的专用词', 'next 指针']
            assert model.payloads('review_glossary') == [{'topic': '链表', 'points': '节点与遍历'}]
            for payload in model.payloads('student'):
                serialized = json.dumps(payload, ensure_ascii=False)
                assert '未讲授的专用词' not in serialized
                assert not any(field in serialized for field in ('raw_text', 'corrected_text', 'review_status', 'review_seconds'))
            sources = model.payloads('student')[0]['new_teacher_segments']
            assert [s['text'] for s in sources] == ['呃，这个链表，然后这个链表。', '文字练表保持原样', '链表还有节点。']
            assert session.sources['t1']['raw_text'] == '呃，这个练表，然后这个练表。'
            assert session.sources['t1']['corrected_text'] == sources[0]['text']
            assert session.sources['t2']['corrected_text'] is None
            assert session.sources['t2']['review_status'] == 'bypassed'
            # A later student's history must use exactly the same canonical text.
            await session.add_transcript('继续')
            await session.flush()
            await until(lambda: 't4' in session.processed_ids)
            assert model.payloads('student')[-1]['teacher_sources'][0] == sources[0]
            record = next(e['data'] for e in events if e['type'] == 'review_completed')
            assert record['sources'] == ['t1', 't2', 't3']
        finally:
            await session.close()
        saved = [json.loads(line) for line in (tmp_path / 'logs' / (session.id + '.jsonl')).read_text(encoding="utf-8").splitlines()]
        assert any(e['type'] == 'review_completed' and e['data']['segments'][0]['raw_text'] !=
                   e['data']['segments'][0]['corrected_text'] for e in saved)
    asyncio.run(run())


@pytest.mark.parametrize('enabled', [True, False])
def test_text_bypass_and_disabled_audio(tmp_path, monkeypatch, enabled):
    async def run():
        session, model, _ = await make_session(tmp_path, monkeypatch, enabled=enabled)
        try:
            await session.add_transcript('文字练表')
            await session.flush()
            if not enabled:
                await session.add_transcript('语音练表', 'microphone')
                await session.flush()
            expected = {'t1'} if enabled else {'t1', 't2'}
            await until(lambda: session.processed_ids == expected)
            assert not model.payloads('asr_review')
            assert bool(model.payloads('review_glossary')) == enabled
            assert session.sources['t1']['text'] == '文字练表'
            if not enabled:
                assert session.sources['t2']['review_status'] == 'disabled'
                assert session.sources['t2']['text'] == '语音练表'
        finally:
            await session.close()
    asyncio.run(run())


def test_slow_review_keeps_order_and_preserves_each_semantic_segment(tmp_path, monkeypatch):
    async def run():
        model = ReviewModel()
        model.review_gate = asyncio.Event()
        session, _, _ = await make_session(tmp_path, monkeypatch, model)
        try:
            await session.add_transcript('第一段练表', 'microphone')
            await session.flush()
            await until(lambda: bool(model.payloads('asr_review')))
            await session.add_transcript('第二段文字')
            await session.flush()
            await session.add_transcript('第三段练表', 'microphone')
            await session.flush()
            assert not model.payloads('student') and not model.review_cancelled
            model.review_gate.set()
            await until(lambda: len(session.processed_ids) == 3)
            assert [[s['id'] for s in p['new_asr_segments']] for p in model.payloads('asr_review')] == [['t1'], ['t3']]
            assert [s['id'] for p in model.payloads('student') for s in p['new_teacher_segments']] == ['t1', 't2', 't3']
            assert [s['id'] for s in model.payloads('asr_review')[1]['previous_teacher_sources']] == ['t1', 't2']
        finally:
            await session.close()
    asyncio.run(run())


def test_student_retry_reuses_review_and_new_input_does_not_cancel_learning(tmp_path, monkeypatch):
    async def run():
        model = ReviewModel()
        model.fail_student = True
        session, _, events = await make_session(tmp_path, monkeypatch, model)
        try:
            await session.add_transcript('练表', 'microphone')
            await session.flush()
            await until(lambda: session.failed_batch is not None)
            await session.add_transcript('后面的内容')
            await session.flush()
            session.retry_event.set()
            await until(lambda: len(session.processed_ids) == 2)
            assert len(model.payloads('asr_review')) == 1
            assert model.payloads('student')[0] == model.payloads('student')[1]
            assert session.state.version == 2
            assert not [e for e in events if e['type'] == 'reply']
        finally:
            await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('output', [
    {'segments': []},
    {'segments': [{'id': 't1', 'text': '   '}, {'id': 't2', 'text': '内容'}]},
    {'segments': [{'id': 't1', 'text': '部分校正'}]},
    {'segments': [{'id': 't1', 'text': '部分校正'}, {'id': 't1', 'text': '重复'}]},
    {'segments': [{'id': 't2', 'text': '乱序'}, {'id': 't1', 'text': '乱序'}]},
    {'segments': [{'id': 't1', 'text': '部分校正'}, {'id': 't99', 'text': '陌生来源'}]},
])
def test_invalid_review_falls_back_atomically(tmp_path, monkeypatch, output):
    async def run():
        model = ReviewModel()
        model.review_output = output
        session, _, events = await make_session(tmp_path, monkeypatch, model)
        try:
            for text in ['第一段练表', '第二段练表']:
                await session.add_transcript(text, 'microphone')
            await session.flush()
            await until(lambda: len(session.processed_ids) == 2)
            assert all(session.sources[k]['review_status'] == 'fallback' for k in ['t1', 't2'])
            assert all(session.sources[k]['corrected_text'] is None for k in ['t1', 't2'])
            assert [s['text'] for s in model.payloads('student')[0]['new_teacher_segments']] == ['第一段练表', '第二段练表']
            assert any(e['type'] == 'warning' for e in events)
            assert not any(e['type'] == 'error' for e in events)
        finally:
            await session.close()
    asyncio.run(run())


def test_glossary_failure_and_review_timeout_continue_with_original(tmp_path, monkeypatch):
    async def run():
        model = ReviewModel()
        model.fail_glossary = True
        model.review_gate = asyncio.Event()
        monkeypatch.setattr(config, 'ASR_REVIEW_TIMEOUT', .02)
        session, _, events = await make_session(tmp_path, monkeypatch, model)
        try:
            assert session.reviewer.terms == []
            await session.add_transcript('练表', 'microphone')
            await session.flush()
            await until(lambda: bool(session.processed_ids))
            assert model.review_cancelled
            assert session.sources['t1']['review_status'] == 'fallback'
            assert model.payloads('asr_review')[0]['terms'] == []
            assert len([e for e in events if e['type'] == 'warning']) == 2
        finally:
            await session.close()
    asyncio.run(run())


@pytest.mark.parametrize('phase,timeout_name', [('asr_review', 'ASR_REVIEW_TIMEOUT'), ('review_glossary', 'ASR_REVIEW_GLOSSARY_TIMEOUT')])
def test_deadline_includes_json_repair_attempt(monkeypatch, phase, timeout_name):
    monkeypatch.setattr(config, 'API_KEY', 'test-key')
    monkeypatch.setattr(config, timeout_name, .03)
    async def run():
        calls, events = [], []
        async def transport(request):
            calls.append(json.loads(request.content))
            if len(calls) > 1:
                await asyncio.Event().wait()
            return httpx.Response(200, json={'choices': [{'message': {'content': 'not json'}}]})
        async def emit(kind, data):
            events.append((kind, data))
        llm = ModelClient(emit)
        await llm.http.aclose()
        llm.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        reviewer = TranscriptReviewer(llm, emit, Lesson(topic='链表'))
        try:
            if phase == 'review_glossary':
                await reviewer.initialize()
                assert reviewer.terms == []
            else:
                batch = [{'id': 't1', 'text': '练表', 'raw_text': '练表', 'mode': 'microphone',
                          'revision': 1, 'at': 0, 'corrected_text': None, 'review_status': 'pending', 'review_seconds': 0}]
                result = await reviewer.review(batch, [])
                assert result[0]['review_status'] == 'fallback'
                assert batch[0]['review_status'] == 'pending'
            assert len(calls) == 2
            assert any(kind == 'warning' for kind, _ in events)
        finally:
            await llm.close()
    asyncio.run(run())


@pytest.mark.parametrize('count', [40, 41, 45, 46])
def test_glossary_overrun_tolerance_keeps_model_limit_and_review_flow(monkeypatch, count):
    monkeypatch.setattr(config, 'API_KEY', 'test-key')
    async def run():
        calls, events = [], []
        terms = [f'术语{i}' for i in range(count)]
        async def transport(request):
            body = json.loads(request.content)
            calls.append(body)
            if body['messages'][0]['content'].startswith('你为语音识别文本校正器'):
                result = {'terms': terms}
            else:
                result = {'segments': [{'id': 't1', 'text': '链表'}]}
            return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})
        async def emit(kind, data):
            events.append((kind, data))
        llm = ModelClient(emit)
        await llm.http.aclose()
        llm.http = httpx.AsyncClient(transport=httpx.MockTransport(transport))
        reviewer = TranscriptReviewer(llm, emit, Lesson(topic='链表'))
        try:
            await reviewer.initialize()
            prompt, schema = calls[0]['messages'][0]['content'].split('\nJSON schema:\n')
            assert '最多 40 项' in prompt
            assert json.loads(schema)['properties']['terms']['maxItems'] == 40
            assert len(calls) == (1 if count <= 45 else 2)
            assert reviewer.terms == (terms if count <= 45 else [])
            glossary = next(data for kind, data in events if kind == 'review_glossary' and data['status'] != 'started')
            assert glossary['status'] == ('ready' if count <= 45 else 'fallback')
            assert any(kind == 'warning' for kind, _ in events) == (count > 45)
            batch = [{'id': 't1', 'text': '练表', 'raw_text': '练表', 'mode': 'microphone',
                      'revision': 1, 'at': 0, 'corrected_text': None, 'review_status': 'pending', 'review_seconds': 0}]
            result = await reviewer.review(batch, [])
            payload = json.loads(calls[-1]['messages'][1]['content'])
            assert payload['terms'] == reviewer.terms
            assert result[0]['text'] == '链表' and result[0]['review_status'] == 'reviewed'
        finally:
            await llm.close()
    asyncio.run(run())


def test_context_is_last_eight_finalized_teacher_sources(tmp_path, monkeypatch):
    async def run():
        model = ReviewModel()
        model.student_gate = asyncio.Event()
        session, _, _ = await make_session(tmp_path, monkeypatch, model)
        try:
            for i in range(10):
                await session.add_transcript(f'教师内容 {i}')
                await session.flush()
            await session.review_queue.join()
            await session.add_transcript('练表', 'microphone')
            await session.flush()
            await session.review_queue.join()
            context = model.payloads('asr_review')[0]['previous_teacher_sources']
            assert [s['id'] for s in context] == [f't{i}' for i in range(3, 11)]
            assert session.processed_ids == set()  # Review is independent of student completion.
            assert 'current_state' not in model.payloads('asr_review')[0]
        finally:
            await session.close()
    asyncio.run(run())


def test_finish_drains_audio_tail_and_review_queue(tmp_path, monkeypatch):
    async def run():
        session, model, events = await make_session(tmp_path, monkeypatch)
        session.audio = SimpleNamespace(finish=lambda: [np.zeros(1600, dtype=np.float32)])
        session.recognizer = SimpleNamespace(transcribe=lambda samples: '末尾练表')
        await session.finish()
        assert session.closed and model.closed
        assert session.processed_ids == {'t1'}
        assert session.sources['t1']['text'] == '末尾链表'
        finished = next(e['data'] for e in events if e['type'] == 'finished')
        assert finished['unprocessed_sources'] == []
        assert all(t.done() for t in session.tasks)
    asyncio.run(run())


@pytest.mark.parametrize('finish', [False, True])
def test_close_cancels_pending_review_and_finish_reports_unprocessed(tmp_path, monkeypatch, finish):
    async def run():
        model = ReviewModel()
        model.review_gate = asyncio.Event()
        session, _, events = await make_session(tmp_path, monkeypatch, model)
        await session.add_transcript('练表', 'microphone')
        await session.flush()
        await until(lambda: bool(model.payloads('asr_review')))
        if finish:
            # Advance only this module's clock past the existing 100-second finish limit.
            ticks = iter(range(0, 10000, 101))
            monkeypatch.setattr('backend.session.time', SimpleNamespace(monotonic=lambda: next(ticks)))
            await session.finish()
            finished = next(e['data'] for e in events if e['type'] == 'finished')
            assert finished['unprocessed_sources'] == ['t1']
        else:
            await session.close()
        assert model.review_cancelled and model.closed
        assert session.sources['t1']['raw_text'] == '练表'
        assert not model.payloads('student')
        assert all(t.done() for t in session.tasks)
    asyncio.run(run())


@pytest.mark.parametrize('option', [None, False, True, 'false'])
def test_websocket_review_option(tmp_path, monkeypatch, option):
    from backend import app as service
    class Models:
        acquired = False
        async def acquire(self):
            self.acquired = True
            return None, None, 'fake'
        async def release(self): pass
        async def shutdown(self): pass
    models = Models()
    monkeypatch.setattr(service, 'models', models)
    monkeypatch.setattr(service, 'active_session', None)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    monkeypatch.setattr('backend.session.ModelClient', ReviewModel)
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            event = {'type': 'start', 'lesson': {'topic': '链表'}, 'tts': False}
            if option is not None:
                event['asr_review'] = option
            ws.send_json(event)
            while True:
                response = ws.receive_json()
                if response['type'] in ('ready', 'error'):
                    break
            if option == 'false':
                assert response['type'] == 'error' and not models.acquired
            else:
                assert response['type'] == 'ready'
                assert response['data']['asr_review'] == (option is not False)
