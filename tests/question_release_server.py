"""Browser acceptance fixture: real service and schema, entirely offline model transport."""
import argparse
import json
import shutil
import sys
from pathlib import Path

import httpx
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from backend import config, prompts
from backend.llm import ModelClient
from question_recovery_server import OfflineModels


class OfflineModel(ModelClient):
    def __init__(self, emit):
        self.emit = emit
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond))

    def respond(self, request):
        body = json.loads(request.content)
        if body['messages'][0]['content'].startswith(prompts.PREPARE):
            data = {'scope': ['盒子规则'], 'boundary_note': '尚未学习'}
        else:
            payload = next(json.loads(m['content']) for m in reversed(body['messages'])
                           if m['role'] == 'user' and m['content'].startswith('{'))
            context = payload['interaction_context']
            if context['trigger'] == 'unmuted':
                sid = 't6'
            else:
                sid = payload['new_teacher_segments'][-1]['id']
            data = {}
            if sid in ('t1', 't2'):
                data = {'question_updates': [{'id': 'q1', 'topic': '折光含义', 'text': '折光是什么？',
                         'status': 'pending', 'sources': [sid]}],
                        'candidate': {'kind': 'question', 'question_id': 'q1', 'text': '折光是什么？', 'sources': [sid]}}
            if sid in ('t4', 't6'):
                rid = 'r_' + sid
                if context['trigger'] != 'unmuted':
                    data['response_request_updates'] = [{'id': rid, 'kind': 'invite_questions', 'text': '说明疑问',
                        'sources': [sid], 'invitation_source_id': sid,
                        'invitation_quote': payload['new_teacher_segments'][-1]['text']}]
                if not context['muted']:
                    data['candidate'] = {'kind': 'question', 'question_id': 'q1', 'text': '我仍想问，折光是什么？',
                        'sources': [sid], 'response_request_ids': [rid], 'question_release_request': {
                            'question_id': 'q1', 'response_request_id': rid, 'reason': '教师本次明确邀请说明疑问'}}
            if sid == 't5':
                data = {'response_request_updates': [{'id': 'r5', 'kind': 'check_audio', 'text': '检查声音', 'sources': [sid]}],
                        'candidate': {'kind': 'ack', 'text': '老师，我在听。', 'sources': [sid], 'response_request_ids': ['r5']}}
        return httpx.Response(200, json={'choices': [{'message': {'content': json.dumps(data, ensure_ascii=False)}}]})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--root', type=Path, required=True)
    args = parser.parse_args()
    shutil.copytree(config.ROOT / 'frontend', args.root / 'frontend')
    config.ROOT, config.PORT = args.root, args.port
    config.API_KEY = 'offline-test-key'
    config.DIRECT_PAUSE = config.SPEAK_PAUSE = .05
    from backend import app as service, session
    session.ModelClient = OfflineModel
    service.models = OfflineModels()
    uvicorn.run(service.app, host='127.0.0.1', port=args.port, log_level='warning')
