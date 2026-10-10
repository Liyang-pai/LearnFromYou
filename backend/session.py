import asyncio
import contextlib
import json
import re
import time
import uuid
from collections import deque
from datetime import datetime, timezone

import numpy as np
from asr.recognizer import AudioStream
from . import config, prompts
from .llm import ModelClient, ModelError
from .policy import apply_result, invited, mark_delivered, speech_block
from .review import TranscriptReviewer, teacher_source
from .schemas import Lesson, Preparation, StudentResult, StudentState
from .speech import Speaker
from .report_data import REPORT_EVENTS, build_snapshot
from .report_v2 import report_service


class Session:
    def __init__(self, send, recognizer, vad_path=None, asr_model=None):
        self.send = send
        self.recognizer = recognizer
        self.vad_path = vad_path
        self.asr_model = asr_model
        self.id = uuid.uuid4().hex[:12]
        self.state = StudentState()
        self.sources = {}
        self.processed_ids = set()
        self.prerequisites = []
        self.history = []
        self.pending = []
        self.pending_since = 0
        self.audio_queue = asyncio.Queue(maxsize=240)
        self.asr_queue = asyncio.Queue(maxsize=30)
        self.review_queue = asyncio.Queue()
        self.teaching_queue = asyncio.Queue()
        self.asr_review = True
        self.reviewer = None
        self.review_processing = False
        self.review_context = deque(maxlen=8)
        self.asr_pending = 0
        self.audio = None
        self.upload = None
        self.audio_enabled = False
        self.audio_started_at = 0
        self.audio_frames = 0
        self.last_audio_notice = 0
        self.active = False
        self.processing = False
        self.muted = False
        self.tts_enabled = True
        self.revision = 0
        self.last_voice = time.monotonic()
        self.candidate = None
        self.last_block = None
        self.speaker = Speaker()
        self.tts_task = None
        self.tasks = []
        self.retry_event = asyncio.Event()
        self.failed_batch = None
        self.ready = False
        self.finishing = False
        self.closed = False
        self._close_task = None
        self.started = time.monotonic()
        self.log = None
        self.event_sequence = 0
        self.report_events = []
        self.llm = ModelClient(self.emit)

    async def emit(self, kind, data):
        self.event_sequence += 1
        event = {"id": f"{self.id}:e{self.event_sequence:06d}", "type": kind, "time": datetime.now(timezone.utc).isoformat(),
                  "elapsed": round(time.monotonic() - self.started, 3), "data": data}
        if kind == "finished":
            lesson_text = json.dumps(self.lesson.model_dump(), ensure_ascii=False)
            if config.API_KEY:
                lesson_text = lesson_text.replace(config.API_KEY, '[REDACTED]')
            snapshot = build_snapshot(self.id, self.report_events + [event], json.loads(lesson_text))
            event['data']['settlement'] = snapshot['settlement']
            try:
                report_service.register(snapshot)
            except OSError:
                event['data']['report_notice'] = '报告暂存文件写入失败，基础结算仍可查看；请检查文件权限。'
        serialized = json.dumps(event, ensure_ascii=False)
        if config.API_KEY:
            serialized = serialized.replace(config.API_KEY, "[REDACTED]")
        event = json.loads(serialized)
        if kind in REPORT_EVENTS:
            self.report_events.append(event)
        if self.log:
            self.log.write(serialized + "\n")
            self.log.flush()
        if not self.closed:
            with contextlib.suppress(Exception):
                await self.send(event)

    async def start(self, lesson: Lesson, muted=False, tts=True, asr_review=True):
        self.lesson = lesson
        self.muted, self.tts_enabled = muted, tts
        self.asr_review = asr_review
        logs = config.ROOT / "logs"
        logs.mkdir(exist_ok=True)
        self.log = (logs / (self.id + ".jsonl")).open("a", encoding="utf-8")
        await self.emit("status", {"message": "正在分析本节课范围，尚未开始听课", "session_id": self.id})
        self.preparation = await self.llm.generate("preparation", prompts.PREPARE, lesson.model_dump(), Preparation)
        for text in re.split(r"[\n；;]+", lesson.prerequisites):
            if text.strip():
                item = {"id": f"p{len(self.prerequisites) + 1}", "text": text.strip(), "kind": "prerequisite"}
                self.prerequisites.append(item)
                self.sources[item["id"]] = item
        if self.asr_review:
            self.reviewer = TranscriptReviewer(self.llm, self.emit, lesson)
            await self.reviewer.initialize()
        self.ready = True
        self.tasks = [asyncio.create_task(self.audio_loop()), asyncio.create_task(self.asr_loop()),
                      asyncio.create_task(self.learn_loop()), asyncio.create_task(self.clock_loop())]
        if self.asr_review:
            self.tasks.append(asyncio.create_task(self.review_loop()))
        await self.emit("ready", {"session_id": self.id, "asr_model": self.asr_model, "scope": self.preparation.model_dump(),
            "prerequisites": self.prerequisites, "muted": self.muted, "state": self.state.model_dump(),
            "asr_review": self.asr_review})

    async def start_audio(self, rate, upload=None):
        if not 8000 <= rate <= 96000:
            raise ValueError("不支持的麦克风采样率")
        if self.upload or (upload and self.audio):
            raise ValueError("请先停止当前音频输入，再开始新的输入")
        if self.audio:
            await self.stop_audio()
        self.audio = await asyncio.to_thread(AudioStream, self.vad_path, rate)
        self.upload = upload
        self.audio_enabled = True
        self.audio_started_at = time.monotonic()
        self.audio_frames = 0
        self.last_audio_notice = 0
        await self.emit("audio", {"recording": True, "sample_rate": rate,
                                  **(upload.progress() if upload else {})})

    async def accept_audio(self, data):
        if not self.audio_enabled or self.finishing:
            return
        if not data or len(data) % 4 or len(data) > 65536:
            raise ValueError("音频帧格式无效")
        samples = np.frombuffer(data, dtype="<f4").copy()
        if not np.isfinite(samples).all():
            raise ValueError("音频含无效数值")
        if self.upload:
            self.upload.accept(samples)
        if self.audio_queue.full():
            self.audio_enabled = False
            await self.emit("error", {"message": "音频处理积压，已停止接收新录音；已接收内容继续处理，请暂停讲授后重启麦克风", "code": "audio_backlog"})
            return
        self.audio_frames += 1
        now = time.monotonic()
        if self.audio_frames == 1 or now - self.last_audio_notice >= 2:
            self.last_audio_notice = now
            await self.emit("audio_input", {"frames": self.audio_frames, "samples": int(len(samples)),
                                              "queue": self.audio_queue.qsize()})
        self.audio_queue.put_nowait((samples, self.upload))

    async def audio_loop(self):
        while True:
            samples, upload = await self.audio_queue.get()
            try:
                segments, transitions, active = await asyncio.to_thread(self.audio.feed, samples)
                for on in transitions:
                    if on:
                        self.revision += 1
                        self.candidate = None
                        await self.stop_speech("教师开始讲话")
                    await self.emit("vad", {"speaking": on})
                if active:
                    self.last_voice = time.monotonic()
                elif self.active:
                    self.last_voice = time.monotonic() - .6
                self.active = active
                for segment in segments:
                    self.asr_pending += 1
                    await self.asr_queue.put((segment, upload))
                if upload and not upload.failed:
                    await self.emit("upload_ack", upload.progress(len(samples)))
            except Exception:
                self.audio_enabled = False
                if upload:
                    upload.failed = True
                await self.emit("error", {"message": "录音处理失败，请停止并重新开启麦克风", "code": "audio_failure"})
            finally:
                self.audio_queue.task_done()

    async def asr_loop(self):
        while True:
            samples, upload = await self.asr_queue.get()
            try:
                started = time.monotonic()
                text = await asyncio.to_thread(self.recognizer.transcribe, samples)
                seconds = round(time.monotonic() - started, 3)
                if text:
                    await self.add_transcript(text, "microphone", {"asr_seconds": seconds, "audio_seconds": round(len(samples) / 16000, 3)},
                                              upload.metadata if upload else None)
                else:
                    await self.emit("asr_empty", {"asr_seconds": seconds})
            except Exception:
                if upload:
                    upload.failed = True
                    self.audio_enabled = False
                await self.emit("error", {"message": "本段语音识别失败，未生成课堂文本；请重讲这一段", "code": "asr_failure"})
            finally:
                self.asr_pending -= 1
                self.asr_queue.task_done()

    async def add_transcript(self, text, mode="text", metrics=None, source=None, question_count=None):
        text = text.strip()
        if not text:
            return
        if len(text) > 5000:
            raise ValueError("单次文本测试请限制在 5000 字内")
        if question_count is not None and (type(question_count) is not int or not 0 <= question_count <= 50):
            raise ValueError('提问次数必须是 0 到 50 的整数，或留空表示未标注')
        if mode == "text":
            self.last_voice = time.monotonic()
            await self.stop_speech("收到新的教师文本")
        self.revision += 1
        self.candidate = None
        item = {"id": f"t{sum(k.startswith('t') for k in self.sources) + 1}", "text": text,
                "mode": mode, "revision": self.revision, "at": round(time.monotonic() - self.started, 3),
                "raw_text": text, "corrected_text": None, "review_seconds": 0,
                "review_status": "bypassed" if mode == "text" else ("pending" if self.asr_review else "disabled")}
        if mode == 'text':
            item['question_count'] = question_count
        if source:
            item.update(source)
        self.sources[item["id"]] = item
        if not self.pending:
            self.pending_since = time.monotonic()
        self.pending.append(item)
        await self.emit("transcript", {**item, **(metrics or {})})

    async def stop_audio(self, cancelled=False):
        self.audio_enabled = False
        upload = self.upload
        await self.audio_queue.join()
        if self.audio:
            try:
                segments = await asyncio.to_thread(self.audio.finish)
            except Exception:
                segments = []
                if upload:
                    upload.failed = True
                await self.emit("error", {"message": "末尾音频处理失败，已有转写保留", "code": "audio_failure"})
            for samples in segments:
                self.asr_pending += 1
                await self.asr_queue.put((samples, upload))
        await self.asr_queue.join()
        self.audio = None
        self.upload = None
        self.active = False
        await self.emit("audio", {"recording": False})
        if upload:
            await self.flush("audio_upload_stopped" if cancelled else "audio_upload_end")
            await self.emit("upload_finished", upload.summary(cancelled))

    async def flush(self, reason="manual"):
        if not self.pending:
            return
        batch, self.pending = self.pending, []
        self.pending_since = 0
        queue = self.review_queue if self.asr_review else self.teaching_queue
        # The queue insertion precedes the first await to preserve flush order.
        queue.put_nowait(batch)
        await self.emit("segment", {"sources": [s["id"] for s in batch], "text": "\n".join(s["text"] for s in batch),
            "reason": reason, "queued": queue.qsize(), "destination": "review" if self.asr_review else "student"})

    async def review_loop(self):
        while True:
            batch = await self.review_queue.get()
            self.review_processing = True
            try:
                reviewed = await self.reviewer.review(batch, list(self.review_context))
                for source in reviewed:
                    self.sources[source["id"]] = source
                    self.review_context.append(teacher_source(source))
                self.teaching_queue.put_nowait(reviewed)
                await self.emit("review_completed", {"sources": [s["id"] for s in reviewed],
                    "segments": reviewed, "seconds": max(s["review_seconds"] for s in reviewed),
                    "queued": self.review_queue.qsize()})
            finally:
                self.review_processing = False
                self.review_queue.task_done()

    def payload(self, batch):
        relevant = {s for k in self.state.knowledge for s in k.sources}
        relevant |= {s for revision in self.state.knowledge_history for s in revision.previous.sources}
        relevant |= {s for q in self.state.questions for s in q.sources + q.resolution_sources}
        teacher = [s for s in self.sources.values() if s["id"] in self.processed_ids or s in batch]
        relevant |= {s["id"] for s in teacher[-8:] + batch}
        return {"lesson_scope": self.preparation.scope, "student_level": self.lesson.level,
                "explicit_prerequisites": self.prerequisites,
                "current_state": self.state.learning_context(), "new_teacher_segments": [teacher_source(s) for s in batch],
                "teacher_sources": [teacher_source(s) for s in teacher if s["id"] in relevant],
                "previous_student_utterances_not_knowledge_sources": self.history[-8:]}

    async def learn_loop(self):
        while True:
            batch = await self.teaching_queue.get()
            count = 1
            while not self.teaching_queue.empty() and count < 4:
                batch += self.teaching_queue.get_nowait()
                count += 1
            self.processing = True
            target_revision = batch[-1]["revision"]
            retry_error = None
            while True:
                try:
                    ids = {s["id"] for s in batch}
                    allowed = {k: v for k, v in self.sources.items() if k.startswith("p") or k in ids or k in self.processed_ids}
                    payload = self.payload(batch)
                    if retry_error:
                        payload['retry_feedback'] = {'error': retry_error, 'allowed_source_ids': sorted(allowed)}
                    result = await self.llm.generate("student", prompts.STUDENT, payload, StudentResult)
                    next_state, candidate = apply_result(self.state, result, allowed, ids)
                    self.state = next_state
                    self.processed_ids |= ids
                    addressed = result.addressed or invited("\n".join(s["text"] for s in batch))
                    self.candidate = {"reply": candidate, "revision": target_revision, "addressed": addressed}
                    self.last_block = None
                    await self.emit("state", {"state": self.state.model_dump(), "note": result.note,
                        "processed_sources": sorted(ids), "candidate": candidate.model_dump(), "addressed": addressed})
                    self.failed_batch = None
                    break
                except (ModelError, ValueError) as e:
                    retry_error = str(e) if isinstance(e, ValueError) else None
                    self.failed_batch = batch
                    self.retry_event.clear()
                    await self.emit("error", {"message": str(e), "code": "model_failure", "retryable": True,
                                               "sources": [s["id"] for s in batch]})
                    await self.retry_event.wait()
            self.processing = False
            for _ in range(count):
                self.teaching_queue.task_done()

    async def clock_loop(self):
        while True:
            await asyncio.sleep(.15)
            now = time.monotonic()
            if self.audio_enabled and not self.upload and self.audio_started_at and self.audio_frames == 0 and now - self.audio_started_at > 2:
                self.audio_started_at = 0
                await self.emit("error", {"message": "录音已开启，但没有收到浏览器音频帧。请检查 Chrome 的麦克风设备选择和权限，然后重新开启麦克风。", "code": "no_audio_frames", "retryable": True})
            silence = 0 if self.active else now - self.last_voice
            if self.pending:
                text = "\n".join(s["text"] for s in self.pending)
                pause = config.DIRECT_PAUSE if invited(text) else (config.LONG_PAUSE if len(text) > 300 else config.SEGMENT_PAUSE)
                if not self.asr_pending and not self.active and silence >= pause:
                    await self.flush("pause")
                elif now - self.pending_since >= config.SEGMENT_MAX_WAIT:
                    await self.flush("max_wait")
            await self.try_speak(silence)

    async def try_speak(self, silence):
        if not self.candidate or self.finishing:
            return
        c = self.candidate
        reason = speech_block(muted=self.muted, stale=c["revision"] != self.revision,
            busy=bool(self.processing or self.pending or self.asr_pending or self.review_processing
                      or not self.review_queue.empty() or not self.teaching_queue.empty()),
            speaking=self.active, silence=silence,
            required_pause=config.DIRECT_PAUSE if c["addressed"] else config.SPEAK_PAUSE,
            candidate=c["reply"], addressed=c["addressed"])
        if reason:
            if self.last_block != reason:
                await self.emit("suppressed", {"reason": reason})
                self.last_block = reason
            if self.muted or c["revision"] != self.revision or c["reply"].kind == "silence" or (not c["addressed"] and c["reply"].kind != "question"):
                self.candidate = None
            return
        self.candidate = None
        reply = c["reply"]
        mark_delivered(self.state, reply)
        self.history.append({"text": reply.text, "kind": reply.kind, "sources": reply.sources})
        await self.emit("reply", {**reply.model_dump(), "state": self.state.model_dump()})
        if self.tts_enabled:
            self.tts_task = asyncio.create_task(self.say(reply.text))

    async def say(self, text):
        try:
            await self.emit("speech", {"speaking": True})
            code = await self.speaker.speak(text)
            if code not in (0, -15):
                await self.emit("error", {"message": "系统语音播报失败，文字回复仍可查看", "code": "tts_failure"})
        except OSError:
            await self.emit("error", {"message": "系统语音不可用，文字回复仍可查看", "code": "tts_failure"})
        finally:
            await self.emit("speech", {"speaking": False})

    async def stop_speech(self, reason):
        await self.speaker.stop()
        if self.tts_task and not self.tts_task.done():
            self.tts_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.tts_task
        self.tts_task = None
        await self.emit("speech_stopped", {"reason": reason})

    async def set_muted(self, muted):
        self.muted = muted
        if muted:
            self.candidate = None
            await self.stop_speech("静音已开启")
        await self.emit("mute", {"muted": muted})

    async def finish(self):
        self.finishing = True
        await self.stop_speech("试讲结束")
        await self.stop_audio()
        await self.flush("session_end")
        await self.emit("status", {"message": "正在处理最后的课堂内容"})
        deadline = time.monotonic() + 100
        while (self.processing or self.review_processing or not self.review_queue.empty()
               or not self.teaching_queue.empty()) and not self.failed_batch and time.monotonic() < deadline:
            await asyncio.sleep(.1)
        remaining = [s for s in self.sources if s.startswith("t") and s not in self.processed_ids]
        await self.emit("finished", {"state": self.state.model_dump(), "unprocessed_sources": remaining,
                                     "session_id": self.id, "log_file": f"logs/{self.id}.jsonl"})
        await self.close()

    async def close(self):
        # A cancelled caller may retry close; reuse the same cleanup task rather
        # than treating a partially released native recognizer as already closed.
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close_resources())
        await asyncio.shield(self._close_task)

    async def _close_resources(self):
        self.closed = True
        await self.speaker.stop()
        for task in self.tasks + ([self.tts_task] if self.tts_task else []):
            task.cancel()
        for task in self.tasks + ([self.tts_task] if self.tts_task else []):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.recognizer and hasattr(self.recognizer, "close"):
            await asyncio.to_thread(self.recognizer.close)
        self.recognizer = None
        self.audio = None
        await self.llm.close()
        if self.log:
            self.log.close()
