"""Optional ASR text review; its glossary never becomes student knowledge."""
import asyncio
import time

from . import config, prompts
from .llm import ModelError
from .schemas import ReviewGlossary, ReviewResult


def teacher_source(source):
    """Only canonical classroom text crosses the student boundary."""
    return {key: source[key] for key in ("id", "text", "mode", "revision", "at")}


class TranscriptReviewer:
    def __init__(self, llm, emit, lesson):
        self.llm, self.emit = llm, emit
        self.topic, self.points = lesson.topic, lesson.points
        self.terms = []

    async def initialize(self):
        started = time.monotonic()
        await self.emit("review_glossary", {"status": "started"})
        status = "ready"
        try:
            async with asyncio.timeout(config.ASR_REVIEW_GLOSSARY_TIMEOUT):
                result = await self.llm.generate("review_glossary", prompts.REVIEW_GLOSSARY,
                    {"topic": self.topic, "points": self.points}, ReviewGlossary)
            self.terms = list(dict.fromkeys(result.terms))
        except (ModelError, ValueError, TimeoutError):
            status = "fallback"
            await self.emit("warning", {"code": "review_glossary_failure",
                "message": "术语表准备失败或超时，将使用教学主题和课堂上下文继续审核。"})
        await self.emit("review_glossary", {"status": status, "terms": self.terms,
            "seconds": round(time.monotonic() - started, 3)})

    async def review(self, batch, context):
        # Work on copies: no partial correction can leak into sources or retries.
        reviewed = [dict(source) for source in batch]
        microphone = [s for s in reviewed if s["mode"] == "microphone"]
        if not microphone:
            return reviewed
        ids = [s["id"] for s in microphone]
        started = time.monotonic()
        await self.emit("review_started", {"sources": [s["id"] for s in batch], "asr_sources": ids})
        try:
            async with asyncio.timeout(config.ASR_REVIEW_TIMEOUT):
                result = await self.llm.generate("asr_review", prompts.REVIEW,
                    {"topic": self.topic, "points": self.points, "terms": self.terms,
                     "previous_teacher_sources": context[-8:],
                     "current_segment": [teacher_source(s) for s in batch],
                     "new_asr_segments": [{"id": s["id"], "text": s["raw_text"]} for s in microphone]},
                    ReviewResult)
                if [s.id for s in result.segments] != ids or any(not s.text.strip() for s in result.segments):
                    raise ValueError("审核来源不完整或顺序不一致")
            for source, correction in zip(microphone, result.segments):
                source.update(text=correction.text, corrected_text=correction.text, review_status="reviewed")
        except (ModelError, ValueError, TimeoutError):
            for source in microphone:
                source.update(text=source["raw_text"], corrected_text=None, review_status="fallback")
            await self.emit("warning", {"code": "asr_review_failure", "sources": ids,
                "message": "本段语音转文字审核失败、超时或结果无效，已使用原始转写继续处理。"})
        seconds = round(time.monotonic() - started, 3)
        for source in microphone:
            source["review_seconds"] = seconds
        return reviewed
