"""验证学生请求精简后的 Schema、发言历史及来源保留边界。"""

import asyncio
import json
from collections import deque

import httpx
from pydantic import BaseModel, Field

from backend.llm import ModelClient
from backend.schemas import Knowledge, StudentState
from backend.student_prompt import compact_learning_context, student_history


def test_student_schema_removes_annotations_without_removing_title_data():
    class Item(BaseModel):
        title: str = Field(description='课堂标题', min_length=1)

    class Output(BaseModel):
        data: Item = Item(title='一 二')

    async def run():
        async def emit(_kind, _data):
            pass

        requests = []

        def respond(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': [{'message': {'content': '{}'}}]})

        model = ModelClient(emit, snapshot={'api_key': 'offline', 'base_url': 'https://example.invalid',
                                            'model': 'text', 'image_model': 'image'})
        await model.http.aclose()
        model.http = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            for phase in ('student', 'student_resume', 'knowledge_review', 'asr_review'):
                await model.generate(phase, '规则', {'text': 'SQL  Server'}, Output)
        finally:
            await model.close()

        original = Output.model_json_schema()
        for request in requests[:3]:
            assert request['messages'][-1]['content'] == '{"text":"SQL  Server"}'
            schema = json.loads(request['messages'][0]['content'].split('\nJSON schema:\n', 1)[1])
            assert 'title' not in schema and 'title' not in schema['$defs']['Item']
            assert schema['$defs']['Item']['properties']['title'] == {
                'type': 'string', 'description': '课堂标题', 'minLength': 1}
            assert schema['properties']['data']['default'] == {'title': '一 二'}
            assert schema['properties']['data']['$ref'] == '#/$defs/Item'
        assert requests[-1]['messages'][-1]['content'] == '{"text": "SQL  Server"}'
        assert json.loads(requests[-1]['messages'][0]['content'].split('\nJSON schema:\n', 1)[1]) == original

    asyncio.run(run())


def test_history_keeps_identical_replies_as_distinct_events_across_window_rollover():
    utterances = [{'text': '听得到。', 'kind': 'ack', 'sources': [f't{i}']} for i in range(10)]
    conversation = deque(maxlen=16)
    for reply in utterances:
        conversation.append({'role': 'user', 'content': reply['sources'][0]})
        conversation.append({'role': 'assistant', 'content': reply})
    conversation.append({'role': 'user', 'content': 't10'})
    conversation.append({'role': 'user', 'content': 't11'})

    history = student_history(conversation, utterances)
    assert len(history) == 17
    assert json.loads(history[0]['content']) == utterances[2]
    assert [json.loads(turn['content']) for turn in history if turn['role'] == 'assistant'] == utterances[-8:]
    assert [turn['content'] for turn in history if turn['role'] == 'user'] == [f't{i}' for i in range(3, 12)]


def test_nonempty_knowledge_fields_and_non_knowledge_empty_fields_remain():
    state = StudentState(knowledge=[Knowledge(
        id='k1', text='已有规则', status='understood', sources=['t1'], conditions='整数',
        revision=3, dependencies=[{'id': 'p1', 'revision': 2}], independent_sources=['t1'],
        correction_sources=['t1'], supersedes=['old'],
    )])
    original = state.model_dump()
    context = compact_learning_context(state.learning_context())
    assert context['knowledge'] == original['knowledge']
    assert context['response_requests'] == [] and context['open_questions'] == []
    assert state.model_dump() == original
