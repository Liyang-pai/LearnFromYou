import asyncio
import json
import time
import httpx
from pydantic import ValidationError
from . import config
from .runtime import RUNTIME, fingerprint

REPORT_REQUEST_TIMEOUT = 60


class ModelError(Exception):
    pass


class ModelClient:
    def __init__(self, emit):
        self.emit = emit
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15))

    async def close(self):
        await self.http.aclose()

    async def generate(self, phase, system, payload, output_type, *, history=(), result_validator=None):
        report_phase = phase.startswith('teaching_report')
        profile_phase = phase == 'student_profile'
        if not config.API_KEY:
            raise ModelError("未找到 Deepseek_API 或 DEEPSEEK_API_KEY，请检查项目 .env")
        messages = [
            {"role": "system", "content": system + "\nJSON schema:\n" + json.dumps(output_type.model_json_schema(), ensure_ascii=False)},
            # 只有课堂调用使用实际师生历史；生成画像、转写审核与报告不混用。
            *([dict(turn) for turn in history] if phase in ('student', 'student_resume') else []),
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for attempt in range(2):
            body = {"model": config.MODEL, "messages": messages, "response_format": {"type": "json_object"},
                    "temperature": 0.25, "max_tokens": 6000 if phase in ('student', 'student_resume', 'knowledge_review') else 3500}
            await self.emit("llm_request", {"phase": phase, "attempt": attempt + 1, "body": body,
                "runtime": RUNTIME, "prompt_fingerprint": fingerprint(messages[0]['content'])})
            started = time.monotonic()
            try:
                async with asyncio.timeout(REPORT_REQUEST_TIMEOUT if report_phase else 90):
                    response = await self.http.post(config.BASE_URL + "/chat/completions", json=body,
                                                    headers={"Authorization": "Bearer " + config.API_KEY})
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
            await self.emit("llm_output", {"phase": phase, "attempt": attempt + 1, "raw": raw,
                "seconds": round(time.monotonic() - started, 3), "usage": envelope.get("usage", {})})
            if phase in ('student', 'student_resume'):
                try:
                    draft = json.loads(raw)
                    release = draft.get('candidate', {}).get('question_release_request')
                    if release:
                        await self.emit('question_release', {**release, 'status': 'requested', 'attempt': attempt + 1})
                except (ValueError, TypeError, AttributeError):
                    pass  # Invalid output is handled by the shared repair budget below.
            if report_phase and envelope['choices'][0].get('finish_reason') == 'length':
                raise ModelError('模型报告输出被截断，未保存为成功报告；请检查输出额度或缩小试讲范围后重试')
            try:
                result = output_type.model_validate_json(raw)
                # JSON 与课堂关联校验共用两次请求预算，不在外层再叠加重试。
                return result_validator(result, attempt == 1) if result_validator else result
            except (ValidationError, ValueError, TypeError) as exc:
                errors = ([{'field': '.'.join(map(str, e['loc'])), 'error': e['msg']} for e in exc.errors()]
                          if isinstance(exc, ValidationError) else [{'field': 'result', 'error': str(exc)}])
                if phase in ('student', 'student_resume', 'knowledge_review'):
                    await self.emit('student_validation_error', {'attempt': attempt + 1, 'errors': errors})
                    if getattr(exc, 'question_id', None) is not None and '例外申请' in str(exc):
                        await self.emit('question_release', {'status': 'rejected', 'question_id': exc.question_id,
                            'response_request_id': getattr(exc, 'request_id', None), 'reason': str(exc), 'attempt': attempt + 1})
                if attempt:
                    if report_phase:
                        raise ModelError('模型两次未返回有效报告结构；课堂内容保留，可以重试教学报告') from None
                    if profile_phase:
                        raise ModelError('模型两次未返回有效画像结构，原有内容已保留，请重试。') from None
                    raise ModelError("模型两次未返回有效结构，未提交学生状态；可重试") from None
                correction = "刚才输出未通过结构或关联校验。修正指出的错误，保留其他有效内容，返回完整 JSON 对象。"
                if isinstance(exc, ValidationError):
                    if report_phase:
                        await self.emit('report_validation_error', {'errors': errors})
                correction += '\n具体错误：' + json.dumps(errors, ensure_ascii=False)[:2000]
                messages += [{"role": "assistant", "content": str(raw)},
                             {"role": "user", "content": correction}]

