"""验证画像生成的数据隔离、用户设置保留、失败处理及结构修复。"""
import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from backend import app as service, config
from backend import student_profile as generator
from backend.llm import ModelClient, ModelError
from backend.schemas import StudentProfileResult


RESULT = {'grade': '八年级', 'subject': '物理', 'points': '电流与电压的关系',
          'student_profile': '已有知识：知道电流表示电荷的定向移动。\n知识边界：尚未学习欧姆定律。'}


@pytest.fixture
def profile_model(monkeypatch):
    calls = []

    class FakeModel:
        def __init__(self, emit):
            self.closed = False
            calls.append(self)

        async def generate(self, phase, system, payload, output_type):
            self.phase, self.payload = phase, payload
            return output_type.model_validate(RESULT)

        async def close(self):
            self.closed = True

    monkeypatch.setattr(generator, 'ModelClient', FakeModel)
    return calls


def test_topic_only_generation_is_independent_of_asr(profile_model, monkeypatch):
    async def forbidden(*args, **kwargs):
        pytest.fail('画像生成不能申请 ASR')
    monkeypatch.setattr(service.models, 'acquire', forbidden)
    with TestClient(service.app) as client:
        response = client.post('/api/student-profile', json={'topic': '欧姆定律'})
    assert response.status_code == 200
    assert response.json() == RESULT
    assert profile_model[0].closed
    assert profile_model[0].phase == 'student_profile'
    assert profile_model[0].payload == {'topic': '欧姆定律', 'points': '', 'grade': '', 'subject': '',
                                      'student_level': 'normal', 'lesson_type': 'new', 'textbook': ''}


def test_teacher_constraints_take_priority(profile_model):
    settings = {'topic': '欧姆定律', 'grade': '自定义阶段', 'subject': '电路基础',
                'points': '  保留原文\n包括换行  ', 'student_level': 'weak',
                'lesson_type': 'review', 'textbook': '教师自编'}
    with TestClient(service.app) as client:
        result = client.post('/api/student-profile', json=settings).json()
    for key in ('grade', 'subject', 'points'):
        assert result[key] == settings[key]
    assert profile_model[0].payload == settings


@pytest.mark.parametrize('extra', [{'topic': '  '}, {'student_level': 'expert'}, {'lesson_type': 'other'},
                                 {'history': []}, {'student_profile': '旧画像'}, {'current_state': {}}])
def test_invalid_or_classroom_input_is_rejected(profile_model, extra):
    with TestClient(service.app) as client:
        response = client.post('/api/student-profile', json={'topic': '欧姆定律', **extra})
    assert response.status_code == 422
    assert not profile_model


@pytest.mark.parametrize('failure', ['network', 'timeout'])
def test_failure_returns_no_partial_result(monkeypatch, failure):
    closed = []

    class Failure:
        def __init__(self, emit): pass
        async def generate(self, *args):
            if failure == 'timeout':
                await asyncio.sleep(1)
            raise ModelError('模拟连接失败')
        async def close(self): closed.append(True)

    monkeypatch.setattr(generator, 'ModelClient', Failure)
    monkeypatch.setattr(generator, 'PROFILE_TIMEOUT', .01)
    with TestClient(service.app) as client:
        response = client.post('/api/student-profile', json={'topic': '欧姆定律'})
    assert response.status_code == 502
    assert list(response.json()) == ['detail']
    assert '原有内容已保留' in response.json()['detail']
    assert closed == [True]


@pytest.mark.parametrize('repair', [True, False])
def test_model_repairs_invalid_profile_once(monkeypatch, repair):
    monkeypatch.setattr(config, 'API_KEY', 'test-key')
    requests = []
    def respond(request):
        requests.append(json.loads(request.content))
        result = RESULT if repair and len(requests) == 2 else {**RESULT, 'student_profile': '  '}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(result)}}]})

    async def run():
        async def emit(*args): pass
        model = ModelClient(emit)
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            if repair:
                result = await model.generate('student_profile', '画像', {'topic': '电学'}, StudentProfileResult)
                assert result.student_profile == RESULT['student_profile']
            else:
                with pytest.raises(ModelError, match='有效画像结构'):
                    await model.generate('student_profile', '画像', {'topic': '电学'}, StudentProfileResult)
        finally:
            await model.close()
    asyncio.run(run())
    assert len(requests) == 2
    assert requests[1]['messages'][-2]['role'] == 'assistant'
