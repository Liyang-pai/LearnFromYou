"""验证确认画像的课堂快照、前置来源、创建拦截、对话历史及报告兼容。"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from backend import app as service, config
from backend.llm import ModelClient
from backend.schemas import Lesson, Preparation, StudentResult, PROFILE_REQUIRED, profile_preparation
from backend.session import Session
from backend.report_pipeline import compact, chunks
from backend.teaching_report import read_classroom
from test_session import until

PROFILE = '已有知识：知道整数加法。\n理解困难：尚未学会星星运算。'


@pytest.mark.parametrize('fields', [{}, {'student_profile': ''}, {'student_profile': ' \n\t '},
                                  {'prerequisites': '', 'level': ''},
                                  {'student_profile': ' ', 'prerequisites': '明确的旧知识'}])
def test_profile_required_before_asr_session_or_log(fields, monkeypatch, tmp_path):
    async def forbidden(*args): pytest.fail('不能申请 ASR')
    monkeypatch.setattr(service.models, 'acquire', forbidden)
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/session') as ws:
            ws.send_json({'type': 'start', 'lesson': {'topic': '星星运算', **fields}})
            assert ws.receive_json() == {'type': 'error', 'data': {'message': PROFILE_REQUIRED}}
    assert not (tmp_path / 'logs').exists()


def test_legacy_migration_preserves_explicit_text_only():
    lesson = Lesson(topic='星星运算', points='新规则', prerequisites='加法\n 减法 ', level='基础薄弱')
    assert lesson.student_profile == '已有知识：加法\n 减法 \n理解基础：基础薄弱'
    assert lesson.points == '新规则'
    assert 'level' not in lesson.model_dump() and 'prerequisites' not in lesson.model_dump()
    assert Lesson(topic='星星运算', level='只有语言能力').student_profile == '理解基础：只有语言能力'
    assert Lesson(topic='星星运算', student_profile=PROFILE, prerequisites='旧数据').student_profile == PROFILE


def test_preparation_cannot_invent_or_rewrite_profile_quotes():
    schema = profile_preparation(PROFILE)
    result = schema(scope=['星星运算'], boundary_note='待学', initial_prerequisites=['知道整数加法。'])
    assert result.initial_prerequisites == ['知道整数加法。']
    with pytest.raises(ValidationError, match='逐字引用'):
        schema(scope=[], boundary_note='', initial_prerequisites=['整数可以相乘。'])


def test_direct_session_start_revalidates_before_creating_logs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    async def run():
        async def send(event): pass
        session = Session(send, None)
        try:
            with pytest.raises(ValidationError, match=PROFILE_REQUIRED):
                await session.start(Lesson.model_construct(topic='主题', student_profile=' '))
            assert not (tmp_path / 'logs').exists()
        finally:
            await session.close()
    asyncio.run(run())


def test_profile_snapshot_sources_and_delivered_history(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'ROOT', tmp_path)
    calls = []
    class Model:
        async def generate(self, phase, system, payload, output_type, *, history=(), result_validator=None):
            calls.append((phase, system, payload, list(history)))
            if phase == 'preparation':
                return output_type(scope=['星星运算'], boundary_note='待学范围',
                                   initial_prerequisites=['知道整数加法。'])
            sid = payload['new_teacher_segments'][-1]['id']
            return StudentResult(addressed=True, candidate={
                'kind': 'answer', 'text': '请讲解星星运算的规则。', 'sources': [sid]})
        async def close(self): pass

    async def run():
        events = []
        async def send(event): events.append(event)
        session = Session(send, None)
        await session.llm.close(); session.llm = Model()
        lesson = Lesson(topic='星星运算', points='星星运算的规则', student_profile=PROFILE)
        try:
            await session.start(lesson, muted=True, tts=False, asr_review=False)
            lesson.student_profile = '外部修改'
            assert session.lesson.student_profile == PROFILE
            assert session.state.version == 0
            assert [(k.id, k.status, k.reason) for k in session.state.knowledge] == [('p1', 'understood', '画像确认基础')]
            assert session.sources == {'p1': {'id': 'p1', 'text': '知道整数加法。', 'kind': 'prerequisite'}}
            assert next(e['data'] for e in events if e['type'] == 'ready')['lesson']['student_profile'] == PROFILE
            await session.add_transcript('请回答：星星运算是什么？'); await session.flush()
            await until(lambda: session.state.version == 1)
            await session.try_speak(10)
            assert [turn['role'] for turn in session.conversation] == ['user']
            await session.set_muted(False)
            await session.add_transcript('我还没讲规则，请回答你是否知道？'); await session.flush()
            await until(lambda: session.state.version == 2)
            await session.try_speak(10)
            assert [turn['role'] for turn in session.conversation] == ['user', 'user', 'assistant']
            await session.add_transcript('好的，请继续听课。'); await session.flush()
            await until(lambda: session.state.version == 3)
            student_calls = [c for c in calls if c[0] == 'student']
            assert all(PROFILE in c[1].replace('\\n', '\n') for c in student_calls)
            assert [turn['role'] for turn in student_calls[-1][3]] == ['user', 'user', 'assistant']
            assert '尚未学会' not in str(student_calls[-1][2]['explicit_prerequisites'])
            await session.finish()
            classroom = read_classroom(tmp_path, session.id)
            assert classroom['lesson']['student_profile'] == PROFILE
            assert 'student_profile' not in compact(classroom)['lesson']
        finally:
            await session.close()
    asyncio.run(run())


def test_model_wire_roles_and_generation_context_isolation(monkeypatch):
    monkeypatch.setattr(config, 'API_KEY', 'test-key')
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"scope":[],"boundary_note":""}'}}]})
    async def run():
        async def emit(*args): pass
        model = ModelClient(emit)
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        history = [{'role': 'user', 'content': '实际讲授'}, {'role': 'assistant', 'content': '实际回复'}]
        try:
            await model.generate('student', '核心规则', {'new_teacher_segments': []}, Preparation, history=history)
            await model.generate('student_profile', '生成规则', {'topic': '主题'}, Preparation, history=history)
        finally: await model.close()
    asyncio.run(run())
    assert [m['role'] for m in requests[0]['messages']] == ['system', 'user', 'assistant', 'user']
    assert [m['role'] for m in requests[1]['messages']] == ['system', 'user']
    assert '实际讲授' not in str(requests[1])


def test_long_profile_does_not_consume_report_analysis_window():
    settings = Lesson(topic='题' * 500, points='点' * 3000, student_profile='像' * 3000).model_dump()
    classroom = {'lesson': settings, 'turns': [{'id': 't1', 'role': 'teacher', 'text': '讲授。' * 12000}]}
    payload = compact(classroom)
    parts = list(chunks(payload))
    assert len(parts) > 1
    assert 'student_profile' not in payload['lesson']
    assert classroom['lesson']['student_profile'] == '像' * 3000
