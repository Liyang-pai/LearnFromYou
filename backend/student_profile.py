"""课前画像生成服务：隔离生成上下文，校验完整结果，并保留教师已填设置。"""
import asyncio

from .llm import ModelClient, ModelError
from .prompts import STUDENT_PROFILE
from .schemas import StudentProfileResult, TeachingSettings

PROFILE_TIMEOUT = 90


async def generate_profile(settings: TeachingSettings):
    # 生成发生在课堂之前，无需建立 Session、占用 ASR 或写入课堂日志。
    async def emit(kind, data):
        pass

    model = ModelClient(emit)
    try:
        async with asyncio.timeout(PROFILE_TIMEOUT):
            result = await model.generate('student_profile', STUDENT_PROFILE,
                                          settings.model_dump(), StudentProfileResult)
        data = result.model_dump()
        for field in ('grade', 'subject', 'points'):
            value = getattr(settings, field)
            if value.strip():
                data[field] = value
        # 所有字段有效后一次性返回，前端不能收到半份候选画像。
        return StudentProfileResult.model_validate(data)
    except TimeoutError:
        raise ModelError('画像生成超时，原有内容已保留，请重试。') from None
    finally:
        await model.close()
