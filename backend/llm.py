import asyncio
import json
import time
import httpx
from pydantic import ValidationError
from .runtime import RUNTIME, fingerprint
from .settings import model_settings

REPORT_REQUEST_TIMEOUT = 60
# Independent of the concise report budget. Thinking and JSON share output
# tokens; one truncated student response may use the remaining bounded retry.
STUDENT_OUTPUT_TOKENS = 6000
STUDENT_RECOVERY_TOKENS = 8000
STUDENT_TOTAL_OUTPUT_TOKENS = 14000


class ModelError(Exception):
    pass


class ModelClient:
    def __init__(self, emit, snapshot=None):
        snapshot = dict(snapshot) if snapshot is not None else model_settings()
        self.api_key = snapshot['api_key']
        self.base_url = snapshot['base_url']
        self.model = snapshot['model']
        self.image_model = snapshot['image_model']
        def redact(value):
            if isinstance(value, str):
                return value.replace(self.api_key, '[REDACTED]')
            if isinstance(value, dict):
                return {redact(key): redact(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [redact(item) for item in value]
            return value
        async def safe_emit(kind, data):
            if self.api_key:
                data = redact(data)
            await emit(kind, data)
        self.emit = safe_emit
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15))

    async def close(self):
        await self.http.aclose()

    async def generate(self, phase, system, payload, output_type, *, history=(), result_validator=None, images=()):
        report_phase = phase.startswith('teaching_report')
        profile_phase = phase == 'student_profile'
        if not self.api_key:
            raise ModelError("未找到 Deepseek_API 或 DEEPSEEK_API_KEY，请检查项目 .env")
        if images and phase not in ('student', 'student_resume'):
            raise ValueError('图片仅用于本次课堂学生请求')
        content = json.dumps(payload, ensure_ascii=False)
        if images:
            urls = await asyncio.to_thread(lambda: [image.data_url() for image in images])
            content = [{'type': 'text', 'text': content}, *[
                {'type': 'image_url', 'image_url': {'url': url, 'detail': 'original'}} for url in urls]]
        messages = [
            {"role": "system", "content": system + "\nJSON schema:\n" + json.dumps(output_type.model_json_schema(), ensure_ascii=False)},
            # 只有课堂调用使用实际师生历史；生成画像、转写审核与报告不混用。
            *([dict(turn) for turn in history] if phase in ('student', 'student_resume') else []),
            {"role": "user", "content": content},
        ]
        student_tokens = STUDENT_OUTPUT_TOKENS
        remaining_tokens = STUDENT_TOTAL_OUTPUT_TOKENS
        for attempt in range(2):
            max_tokens = min(student_tokens, remaining_tokens) if phase in ('student', 'student_resume', 'knowledge_review') else 3500
            if phase in ('student', 'student_resume', 'knowledge_review'):
                remaining_tokens -= max_tokens
            body = {"model": self.image_model if images else self.model, "messages": messages, "response_format": {"type": "json_object"},
                    "temperature": 0.25, "max_tokens": max_tokens}
            # Keep screenshot binaries out of websocket/debug/JSONL exports.
            logged_body = body
            if images:
                logged_body = {**body, 'messages': [{**m, 'content': [
                    ({**part, 'image_url': {'url': '[图片二进制已省略]', 'detail': 'original'}}
                     if part.get('type') == 'image_url' else part) for part in m['content']]
                    if isinstance(m['content'], list) else m['content']} for m in messages]}
            await self.emit("llm_request", {"phase": phase, "attempt": attempt + 1, "body": logged_body,
                "runtime": RUNTIME, "prompt_fingerprint": fingerprint(messages[0]['content'])})
            started = time.monotonic()
            try:
                async with asyncio.timeout(REPORT_REQUEST_TIMEOUT if report_phase else 90):
                    response = await self.http.post(self.base_url + "/chat/completions", json=body,
                                                    headers={"Authorization": "Bearer " + self.api_key})
                response.raise_for_status()
                envelope = response.json()
                raw = envelope["choices"][0]["message"]["content"]
            except TimeoutError:
                if profile_phase:
                    raise ModelError('画像生成超时，原有内容已保留，请重试。') from None
                raise ModelError(f'本阶段请求超过{60 if report_phase else 90}秒；已有内容已保留，可以重试') from None
            except httpx.HTTPStatusError as e:
                raise ModelError(f"模型服务返回 HTTP {e.response.status_code}，请检查密钥、额度或模型配置") from None
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                raise ModelError("模型服务连接失败或返回格式异常；课堂内容已保留，可重试") from None
            finish_reason = envelope['choices'][0].get('finish_reason')
            await self.emit("llm_output", {"phase": phase, "attempt": attempt + 1, "raw": raw,
                "finish_reason": finish_reason, "seconds": round(time.monotonic() - started, 3),
                "usage": envelope.get("usage", {})})
            if phase in ('student', 'student_resume'):
                try:
                    draft = json.loads(raw)
                    release = draft.get('candidate', {}).get('question_release_request')
                    if release:
                        await self.emit('question_release', {**release, 'status': 'requested', 'attempt': attempt + 1})
                except (ValueError, TypeError, AttributeError):
                    pass
            if finish_reason == 'length' and (report_phase or phase in ('student', 'student_resume', 'knowledge_review')):
                await self.emit('report_validation_error' if report_phase else 'student_validation_error',
                    {'attempt': attempt + 1, 'kind': 'truncated',
                     'errors': [{'field': 'response', 'error': '输出达到长度限制，不能作为完整结果提交'}]})
                if report_phase:
                    raise ModelError('模型报告输出被截断，未保存为成功报告；成功阶段保留，可以重试')
                if attempt:
                    raise ModelError('学生本轮输出两次被截断，尚未完成；此前有效状态保留，可以重试')
                student_tokens = STUDENT_RECOVERY_TOKENS
                # Preserve original sources/images. Do not feed a cut-off JSON
                # back as an assistant example or treat it as committed history.
                messages = [*messages, {'role': 'user', 'content':
                    '上次输出被截断，未提交任何状态。请重新返回完整JSON，不续写残片。'
                    '只写本轮必要变更，未变化条目不用重写；保留必要来源及更正、疑问关系。'
                    '候选回复和note保持简短，不展开推理过程。'}]
                continue
            try:
                result = output_type.model_validate_json(raw)
                # JSON 与课堂关联校验共用两次请求预算，不在外层再叠加重试。
                return result_validator(result, attempt == 1) if result_validator else result
            except (ValidationError, ValueError, TypeError) as exc:
                errors = ([{'field': '.'.join(map(str, e['loc'])), 'error': e['msg']} for e in exc.errors()]
                          if isinstance(exc, ValidationError) else [{'field': 'result', 'error': str(exc)}])
                if isinstance(exc, ValidationError):
                    details = exc.errors()
                    kind = ('json' if any(e['type'] == 'json_invalid' for e in details)
                            else 'content' if any(not e['loc'] for e in details) else 'schema')
                else:
                    kind = 'content'
                if phase in ('student', 'student_resume', 'knowledge_review'):
                    await self.emit('student_validation_error', {'attempt': attempt + 1, 'kind': kind, 'errors': errors})
                    if getattr(exc, 'question_id', None) is not None and '例外申请' in str(exc):
                        await self.emit('question_release', {'status': 'rejected', 'question_id': exc.question_id,
                            'response_request_id': getattr(exc, 'request_id', None), 'reason': str(exc), 'attempt': attempt + 1})
                elif report_phase:
                    await self.emit('report_validation_error', {'attempt': attempt + 1, 'kind': kind, 'errors': errors})
                if attempt:
                    if report_phase:
                        reason = {'json': 'JSON格式', 'schema': '字段结构', 'content': '内容或来源'}[kind]
                        raise ModelError(f'报告{reason}校验两次未通过；课堂内容保留，可以重试教学报告') from None
                    if profile_phase:
                        raise ModelError('模型两次未返回有效画像结构，原有内容已保留，请重试。') from None
                    raise ModelError("模型两次未返回有效结构，未提交学生状态；可重试") from None
                correction = "刚才输出未通过结构或关联校验。修正指出的错误，保留其他有效内容，返回完整 JSON 对象。"
                correction += '\n具体错误：' + json.dumps(errors, ensure_ascii=False)[:2000]
                messages += [{"role": "assistant", "content": str(raw)},
                             {"role": "user", "content": correction}]

