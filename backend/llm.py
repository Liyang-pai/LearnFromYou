import asyncio
import json
import time
import httpx
from pydantic import ValidationError
from . import config

REPORT_REQUEST_TIMEOUT = 60


class ModelError(Exception):
    pass


class ModelClient:
    def __init__(self, emit):
        self.emit = emit
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15))

    async def close(self):
        await self.http.aclose()

    async def generate(self, phase, system, payload, output_type):
        report_phase = phase.startswith('teaching_report')
        if not config.API_KEY:
            raise ModelError("未找到 Deepseek_API 或 DEEPSEEK_API_KEY，请检查项目 .env")
        messages = [
            {"role": "system", "content": system + "\nJSON schema:\n" + json.dumps(output_type.model_json_schema(), ensure_ascii=False)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for attempt in range(2):
            body = {"model": config.MODEL, "messages": messages, "response_format": {"type": "json_object"},
                    "temperature": 0.25, "max_tokens": 3500}
            await self.emit("llm_request", {"phase": phase, "attempt": attempt + 1, "body": body})
            started = time.monotonic()
            try:
                async with asyncio.timeout(REPORT_REQUEST_TIMEOUT if report_phase else 90):
                    response = await self.http.post(config.BASE_URL + "/chat/completions", json=body,
                                                    headers={"Authorization": "Bearer " + config.API_KEY})
                response.raise_for_status()
                envelope = response.json()
                raw = envelope["choices"][0]["message"]["content"]
            except TimeoutError:
                raise ModelError('本阶段请求超过60秒；成功阶段已保留，可以重试继续') from None
            except httpx.HTTPStatusError as e:
                raise ModelError(f"模型服务返回 HTTP {e.response.status_code}，请检查密钥、额度或模型配置") from None
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                raise ModelError("模型服务连接失败或返回格式异常；课堂内容已保留，可重试") from None
            await self.emit("llm_output", {"phase": phase, "attempt": attempt + 1, "raw": raw,
                "seconds": round(time.monotonic() - started, 3), "usage": envelope.get("usage", {})})
            if report_phase and envelope['choices'][0].get('finish_reason') == 'length':
                raise ModelError('模型报告输出被截断，未保存为成功报告；请检查输出额度或缩小试讲范围后重试')
            try:
                return output_type.model_validate_json(raw)
            except (ValidationError, ValueError, TypeError) as exc:
                if attempt:
                    if report_phase:
                        raise ModelError('模型两次未返回有效报告结构；课堂内容保留，可以重试教学报告') from None
                    raise ModelError("模型两次未返回有效结构，未提交学生状态；可重试") from None
                correction = "刚才输出不符合给定 JSON schema。仅修复结构和字段，返回完整 JSON 对象。"
                if report_phase and isinstance(exc, ValidationError):
                    errors = [{'field': '.'.join(map(str, e['loc'])), 'error': e['msg']} for e in exc.errors()]
                    await self.emit('report_validation_error', {'errors': errors})
                    correction += '\n具体错误：' + json.dumps(errors, ensure_ascii=False)[:2000]
                messages += [{"role": "assistant", "content": str(raw)},
                             {"role": "user", "content": correction}]

