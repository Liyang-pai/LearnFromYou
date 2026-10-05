import json
import time
import httpx
from pydantic import ValidationError
from . import config


class ModelError(Exception):
    pass


class ModelClient:
    def __init__(self, emit):
        self.emit = emit
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=15))

    async def close(self):
        await self.http.aclose()

    async def generate(self, phase, system, payload, output_type):
        if not config.API_KEY:
            raise ModelError("未找到 Deepseek_API 或 DEEPSEEK_API_KEY，请检查项目 .env")
        messages = [
            {"role": "system", "content": system + "\nJSON schema:\n" + json.dumps(output_type.model_json_schema(), ensure_ascii=False)},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ]
        for attempt in range(2):
            body = {"model": config.MODEL, "messages": messages, "response_format": {"type": "json_object"},
                    "temperature": 0.25, "max_tokens": 8192 if phase == "trial_report" else 3500}
            await self.emit("llm_request", {"phase": phase, "attempt": attempt + 1, "body": body})
            started = time.monotonic()
            try:
                response = await self.http.post(config.BASE_URL + "/chat/completions", json=body,
                                                headers={"Authorization": "Bearer " + config.API_KEY})
                response.raise_for_status()
                envelope = response.json()
                choice = envelope["choices"][0]
                raw = choice["message"]["content"]
                finish_reason = choice.get("finish_reason")
            except httpx.HTTPStatusError as e:
                raise ModelError(f"模型服务返回 HTTP {e.response.status_code}，请检查密钥、额度或模型配置") from None
            except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
                raise ModelError("模型服务连接失败或返回格式异常；课堂内容已保留，可重试") from None
            await self.emit("llm_output", {"phase": phase, "attempt": attempt + 1, "raw": raw,
                "seconds": round(time.monotonic() - started, 3), "usage": envelope.get("usage", {}),
                "finish_reason": finish_reason})
            if phase == "trial_report" and finish_reason == "length":
                raise ModelError("报告输出达到长度限制被截断，未保存报告；请重试或检查模型输出容量")
            try:
                return output_type.model_validate_json(raw)
            except (ValidationError, ValueError, TypeError) as exc:
                if attempt:
                    if phase == "trial_report":
                        raise ModelError("报告模型两次未返回有效结构，未保存报告；可重试") from None
                    raise ModelError("模型两次未返回有效结构，未提交学生状态；可重试") from None
                detail = json.dumps(exc.errors(include_input=False, include_url=False), ensure_ascii=False)[:2000] if isinstance(exc, ValidationError) else "字段类型或 JSON 格式错误"
                messages += [{"role": "assistant", "content": str(raw)},
                             {"role": "user", "content": "刚才输出不符合给定 JSON schema。仅修复结构和字段，返回完整 JSON 对象。" + ("\n校验错误：" + detail if phase == "trial_report" else "")}]
