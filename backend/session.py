import asyncio
import contextlib
import json
import time
import uuid
from collections import deque
from datetime import datetime, timezone

import numpy as np
from asr.recognizer import AudioStream
from . import config, prompts
from .llm import ModelClient, ModelError
from .cognition import seed_prerequisites, dependency_order
from .policy import apply_result, invited, mark_delivered, speech_block, student_result_validator, expire_invitations, release_error
from .review import TranscriptReviewer, teacher_source
from .schemas import Lesson, StudentResult, StudentState, KnowledgeReviewResult, ResumeResult, profile_preparation
from .speech import Speaker
from .runtime import RUNTIME, fingerprint
from .classroom_images import save_image

CURRENT_IMAGE = object()


class Session:
    def __init__(self, send, recognizer, vad_path=None, asr_model=None):
        self.send = send
        self.recognizer = recognizer
        self.vad_path = vad_path
        self.asr_model = asr_model
        self.id = uuid.uuid4().hex[:12]
        self.state = StudentState()
        self.sources = {}
        self.images = {}
        self.current_image = None
        self.image_lock = asyncio.Lock()
        self.processed_ids = set()
        self.prerequisites = []
        self.history = []
        self.conversation = deque(maxlen=16)
        self.pending = []
        self.pending_since = 0
        self.audio_queue = asyncio.Queue(maxsize=240)
        self.asr_queue = asyncio.Queue(maxsize=30)
        self.review_queue = asyncio.Queue()
        self.teaching_queue = asyncio.Queue()
        self.student_work_wake = asyncio.Event()
        self.control_revision = 0
        self.resume_pending = False
        self.resume_failed = False
        self.resume_running = False
        self.asr_review = True
        self.reviewer = None
        self.review_processing = False
        self.knowledge_review_busy = False
        self.knowledge_review_wake = asyncio.Event()
        self.review_context = deque(maxlen=8)
        self.asr_pending = 0
        self.audio = None
        self.upload = None
        self.audio_enabled = False
        self.audio_started_at = 0
        self.audio_frames = 0
        self.audio_segment_image = None
        self.audio_rate = 16000
        self.audio_received_seconds = 0.0
        self.audio_image_timeline = []
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
        self.llm = ModelClient(self.emit)

    async def emit(self, kind, data):
        if kind in ('ready', 'llm_request'):
            data = {**data, 'runtime': RUNTIME, 'student_prompt_fingerprint': getattr(self, 'student_prompt_fingerprint', None)}
        event = {"type": kind, "time": datetime.now(timezone.utc).isoformat(),
                 "elapsed": round(time.monotonic() - self.started, 3), "data": data}
        serialized = json.dumps(event, ensure_ascii=False)
        if config.API_KEY:
            serialized = serialized.replace(config.API_KEY, "[REDACTED]")
        event = json.loads(serialized)
        if self.log:
            self.log.write(serialized + "\n")
            self.log.flush()
        if not self.closed:
            with contextlib.suppress(Exception):
                await self.send(event)

    async def release_event(self, status, candidate, reason='', attempt=None):
        release = candidate.question_release_request
        if not release:
            return
        request = next((r for r in self.state.response_requests if r.id == release.response_request_id), None)
        await self.emit('question_release', {'status': status, 'question_id': release.question_id,
            'response_request_id': release.response_request_id,
            'teacher_source_id': request.invitation_source_id if request else None,
            'invitation_quote': request.invitation_quote if request else '',
            'input_revision': self.revision, 'reason': reason or release.reason, 'attempt': attempt})

    async def cancel_candidate(self, reason, *, expire=False):
        previous, self.candidate = self.candidate, None
        expired = expire_invitations(self.state, self.revision) if expire else []
        if previous:
            await self.release_event('cancelled', previous['reply'], reason)
        if expired:
            await self.emit('invitation_expired', {'request_ids': expired, 'reason': reason, 'state': self.state.model_dump()})

    async def candidate_ready(self):
        if not self.candidate or not self.candidate['reply'].question_release_request:
            return
        c = self.candidate
        reason = release_error(self.state, c['reply'], self.revision)
        if reason or c['revision'] != self.revision:
            await self.cancel_candidate(reason or '新的教师输入使旧邀请失效')
        else:
            await self.release_event('approved', c['reply'], '例外校验通过，等待正常发言时机；尚未消耗许可')

    async def start(self, lesson: Lesson, muted=False, tts=True, asr_review=True):
        # 再次校验并复制，防止直接调用绕过接口或外部修改课堂内固定的画像。
        self.lesson = lesson = Lesson.model_validate(lesson.model_dump())
        self.student_system = prompts.STUDENT + '\n原始确认画像（初始记录，知识修订以 current_state 为准）：\n' + json.dumps(
            {'student_profile': lesson.student_profile, 'teaching_scope_only': {
                'topic': lesson.topic, 'points': lesson.points}}, ensure_ascii=False)
        self.student_prompt_fingerprint = fingerprint(self.student_system)
        self.muted, self.tts_enabled = muted, tts
        self.asr_review = asr_review
        logs = config.ROOT / "logs"
        logs.mkdir(exist_ok=True)
        self.log = (logs / (self.id + ".jsonl")).open("a", encoding="utf-8")
        await self.emit("status", {"message": "正在分析本节课范围，尚未开始听课", "session_id": self.id})
        preparation_type = profile_preparation(lesson.student_profile)
        result = await self.llm.generate("preparation", prompts.PREPARE, lesson.model_dump(), preparation_type)
        self.preparation = preparation_type.model_validate(result.model_dump())
        for text in dict.fromkeys(self.preparation.initial_prerequisites):
            item = {"id": f"p{len(self.prerequisites) + 1}", "text": text, "kind": "prerequisite"}
            self.prerequisites.append(item)
            self.sources[item["id"]] = item
        seed_prerequisites(self.state, self.prerequisites)
        if self.asr_review:
            self.reviewer = TranscriptReviewer(self.llm, self.emit, lesson)
            await self.reviewer.initialize()
        self.ready = True
        self.tasks = [asyncio.create_task(self.audio_loop()), asyncio.create_task(self.asr_loop()),
                      asyncio.create_task(self.learn_loop()), asyncio.create_task(self.clock_loop()),
                      asyncio.create_task(self.knowledge_review_loop())]
        if self.asr_review:
            self.tasks.append(asyncio.create_task(self.review_loop()))
        await self.emit("ready", {"session_id": self.id, "asr_model": self.asr_model, "scope": self.preparation.model_dump(),
            "prerequisites": self.prerequisites, "muted": self.muted, "state": self.state.model_dump(),
            "asr_review": self.asr_review, "lesson": self.lesson.model_dump()})

    async def start_audio(self, rate, upload=None):
        if not 8000 <= rate <= 96000:
            raise ValueError("不支持的麦克风采样率")
        if self.upload or (upload and self.audio):
            raise ValueError("请先停止当前音频输入，再开始新的输入")
        if self.audio:
            await self.stop_audio()
        self.audio = await asyncio.to_thread(AudioStream, self.vad_path, rate)
        self.audio_rate = rate
        self.audio_received_seconds = 0.0
        self.audio_image_timeline = []
        self.audio_segment_image = None
        self.upload = upload
        self.audio_enabled = True
        self.audio_started_at = time.monotonic()
        self.audio_frames = 0
        self.last_audio_notice = 0
        await self.emit("audio", {"recording": True, "sample_rate": rate,
                                  **(upload.progress() if upload else {})})

    async def set_image(self, data):
        async with self.image_lock:
            if not self.ready or self.finishing or self.closed:
                raise ValueError('请在进行中的课堂上传截图')
            image = await asyncio.to_thread(save_image, config.ROOT, self.id, data)
            if self.finishing or self.closed:
                raise ValueError('课堂已结束，截图未切换')
            if self.current_image == image.id:
                return image.public(self.id)
            # Flush older text before changing the visible material; upload does
            # not enqueue a new learning task or become a knowledge source alone.
            await self.flush('image_change')
            if self.finishing or self.closed:
                raise ValueError('课堂已结束，截图未切换')
            self.images[image.id] = image
            self.current_image = image.id
            self.revision += 1
            self.candidate = None
            await self.stop_speech('教学图片已切换')
            public = image.public(self.id)
            await self.emit('teaching_image', public)
            return public

    async def clear_image(self):
        async with self.image_lock:
            if not self.ready or self.finishing or self.closed:
                raise ValueError('请在进行中的课堂移除截图')
            await self.flush('image_clear')
            if self.finishing or self.closed:
                raise ValueError('课堂已结束，当前图片保留')
            self.current_image = None
            self.revision += 1
            self.candidate = None
            await self.stop_speech('当前教学图片已移除')
            await self.emit('teaching_image', {'cleared': True})

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
        if not self.audio_image_timeline or self.audio_image_timeline[-1][1] != self.current_image:
            self.audio_image_timeline.append((self.audio_received_seconds, self.current_image))
        self.audio_received_seconds += len(samples) / self.audio_rate
        now = time.monotonic()
        if self.audio_frames == 1 or now - self.last_audio_notice >= 2:
            self.last_audio_notice = now
            await self.emit("audio_input", {"frames": self.audio_frames, "samples": int(len(samples)),
                                              "queue": self.audio_queue.qsize()})
        self.audio_queue.put_nowait((samples, self.upload, self.current_image))

    def image_for_audio_start(self, start_sample):
        # VAD starts are in the resampled 16 kHz stream, not ASR completion time.
        seconds = start_sample / 16000
        for start, image_id in reversed(self.audio_image_timeline):
            if start <= seconds:
                return image_id
        return None

    async def audio_loop(self):
        while True:
            samples, upload, image_id = await self.audio_queue.get()
            try:
                segments, transitions, active = await asyncio.to_thread(self.audio.feed, samples)
                for on in transitions:
                    if on:
                        self.audio_segment_image = image_id
                        self.revision += 1
                        await self.cancel_candidate("教师开始讲话", expire=True)
                        await self.stop_speech("教师开始讲话")
                    await self.emit("vad", {"speaking": on})
                if active:
                    self.last_voice = time.monotonic()
                elif self.active:
                    self.last_voice = time.monotonic() - .6
                self.active = active
                for index, segment in enumerate(segments):
                    self.asr_pending += 1
                    starts = getattr(self.audio, 'segment_starts', [])
                    bound = self.image_for_audio_start(starts[index]) if index < len(starts) else self.audio_segment_image
                    await self.asr_queue.put((segment, upload, bound))
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
            samples, upload, image_id = await self.asr_queue.get()
            try:
                started = time.monotonic()
                text = await asyncio.to_thread(self.recognizer.transcribe, samples)
                seconds = round(time.monotonic() - started, 3)
                if text:
                    await self.add_transcript(text, "microphone", {"asr_seconds": seconds, "audio_seconds": round(len(samples) / 16000, 3)},
                                              upload.metadata if upload else None, image_id=image_id)
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

    async def add_transcript(self, text, mode="text", metrics=None, source=None, *, image_id=CURRENT_IMAGE):
        text = text.strip()
        if not text:
            return
        if len(text) > 5000:
            raise ValueError("单次文本测试请限制在 5000 字内")
        # Capture the material before stopping speech yields to an HTTP upload.
        # An explicit None from ASR must remain a picture-free source.
        bound_image = self.current_image if image_id is CURRENT_IMAGE else image_id
        if mode == "text":
            self.last_voice = time.monotonic()
            await self.stop_speech("收到新的教师文本")
        self.revision += 1
        await self.cancel_candidate("收到新的教师输入", expire=True)
        if self.resume_failed and not self.muted:
            self.resume_failed = False
            self.resume_pending = True
        item = {"id": f"t{sum(k.startswith('t') for k in self.sources) + 1}", "text": text,
                "mode": mode, "revision": self.revision, "at": round(time.monotonic() - self.started, 3),
                "raw_text": text, "corrected_text": None, "review_seconds": 0,
                "review_status": "bypassed" if mode == "text" else ("pending" if self.asr_review else "disabled")}
        if source:
            item.update(source)
        if bound_image:
            item['image_id'] = bound_image
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
            for index, samples in enumerate(segments):
                self.asr_pending += 1
                starts = getattr(self.audio, 'segment_starts', [])
                bound = self.image_for_audio_start(starts[index]) if index < len(starts) else self.audio_segment_image
                await self.asr_queue.put((samples, upload, bound))
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
        groups = []
        for source in batch:
            if not groups or groups[-1][-1].get('image_id') != source.get('image_id'):
                groups.append([])
            groups[-1].append(source)
        for group in groups:
            queue.put_nowait(group)
        self.student_work_wake.set()
        for group in groups:
            await self.emit("segment", {"sources": [s["id"] for s in group], "text": "\n".join(s["text"] for s in group),
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
                self.student_work_wake.set()
                await self.emit("review_completed", {"sources": [s["id"] for s in reviewed],
                    "segments": reviewed, "seconds": max(s["review_seconds"] for s in reviewed),
                    "queued": self.review_queue.qsize()})
            finally:
                self.review_processing = False
                self.review_queue.task_done()

    def payload(self, batch, trigger='teacher_input'):
        relevant = {s for k in self.state.knowledge for s in k.sources}
        relevant |= {s for revision in self.state.knowledge_history for s in revision.previous.sources}
        relevant |= {s for q in self.state.questions for s in q.sources + q.resolution_sources}
        relevant |= {s for k in self.state.knowledge for s in k.correction_sources}
        relevant |= {s for c in self.state.conflicts for s in c.sources + c.resolution_sources}
        relevant |= {s for r in self.state.pending_reviews for s in r.trigger_sources}
        relevant |= {s for r in self.state.response_requests for s in r.sources + r.resolution_sources}
        teacher = [s for s in self.sources.values() if s["id"] in self.processed_ids or s in batch]
        relevant |= {s["id"] for s in teacher[-8:] + batch}
        input_revision = batch[-1]['revision'] if batch else self.revision
        result = {"interaction_context": {"muted": getattr(self, 'muted', False), "trigger": trigger, "input_revision": input_revision},
                "lesson_scope": self.preparation.scope, "student_level": self.lesson.student_level,
                "explicit_prerequisites": [k for k in self.state.learning_context()['knowledge'] if k['id'].startswith('p')],
                "current_state": self.state.learning_context(), "new_teacher_segments": [teacher_source(s) for s in batch],
                "teacher_sources": [teacher_source(s) for s in teacher if s["id"] in relevant],
                "previous_student_utterances_not_knowledge_sources": self.history[-8:]}
        image_ids = list(dict.fromkeys(s['image_id'] for s in batch if s.get('image_id')))
        if image_ids:
            result['teaching_images'] = [{'id': image_id, 'teacher_source_ids': [
                s['id'] for s in batch if s.get('image_id') == image_id]} for image_id in image_ids]
        return result

    async def learn_loop(self):
        while True:
            await self.student_work_wake.wait()
            self.student_work_wake.clear()
            if self.teaching_queue.empty():
                if self.resume_pending and self.resume_ready():
                    await self.resume_response()
                continue
            batch = self.teaching_queue.get_nowait()
            count = 1
            while not self.images and not self.teaching_queue.empty() and count < 4:
                batch += self.teaching_queue.get_nowait()
                count += 1
            self.processing = True
            target_revision = batch[-1]["revision"]
            while True:
                try:
                    ids = {s["id"] for s in batch}
                    allowed = {k: v for k, v in self.sources.items() if k.startswith("p") or k in ids or k in self.processed_ids}
                    recovery = []
                    image_ids = list(dict.fromkeys(s['image_id'] for s in batch if s.get('image_id')))
                    images = [self.images[image_id] for image_id in image_ids]
                    control_revision = self.control_revision
                    result = await self.llm.generate("student", self.student_system, self.payload(batch), StudentResult,
                        history=list(self.conversation),
                        result_validator=student_result_validator(self.state, allowed, ids, recovery, input_revision=target_revision, require_response=not self.muted),
                        **({'images': images} if images else {}))
                    next_state, candidate = apply_result(self.state, result, allowed, ids, input_revision=target_revision)
                    self.state = next_state
                    expire_invitations(self.state, self.revision)
                    self.processed_ids |= ids
                    # 只记录已成功处理的教师批次；失败重试不会重复插入历史。
                    self.conversation.append({'role': 'user', 'content': json.dumps(
                        {'teacher_segments': [teacher_source(s) for s in batch]}, ensure_ascii=False)})
                    addressed = result.addressed or bool(candidate.response_request_ids) or invited("\n".join(s["text"] for s in batch))
                    # 切换静音不丢学习进度，但旧控制状态生成的发言不再有效。
                    if not self.muted and control_revision == self.control_revision:
                        self.candidate = {"reply": candidate, "revision": target_revision,
                                          "control_revision": control_revision, "addressed": addressed,
                                          "image_id": batch[-1].get("image_id")}
                    else:
                        self.candidate = None
                    if (self.candidate and candidate.kind != 'silence' and target_revision == self.revision):
                        self.resume_pending = False
                        self.resume_failed = False
                    self.last_block = None
                    await self.candidate_ready()
                    await self.emit("state", {"state": self.state.model_dump(), "note": result.note,
                        "processed_sources": sorted(ids), "candidate": candidate.model_dump(), "addressed": addressed})
                    if self.candidate is None and candidate.kind != 'silence':
                        await self.emit('suppressed', {'reason': '学生静音或控制状态已变化，候选未发出；学习记录已保存'})
                    for detail in recovery:
                        await self.emit('student_recovery', detail)
                        if detail['code'] == 'question_release_denied':
                            await self.emit('question_release', {**detail, 'status': 'denied'})
                    self.failed_batch = None
                    break
                except (ModelError, ValueError) as e:
                    self.failed_batch = batch
                    self.retry_event.clear()
                    await self.emit("error", {"message": str(e), "code": "model_failure", "retryable": True,
                                               "sources": [s["id"] for s in batch]})
                    await self.retry_event.wait()
            self.processing = False
            self.knowledge_review_wake.set()
            for _ in range(count):
                self.teaching_queue.task_done()
            if not self.teaching_queue.empty():
                self.student_work_wake.set()

    def resume_ready(self):
        return not (self.muted or self.finishing or self.closed or self.processing or self.active
                    or self.pending or self.asr_pending or self.review_processing or self.knowledge_review_busy
                    or not self.audio_queue.empty() or not self.asr_queue.empty()
                    or not self.review_queue.empty() or not self.teaching_queue.empty()
                    or time.monotonic() - self.last_voice < config.DIRECT_PAUSE)

    def has_resume_work(self):
        return (any(r.status == 'pending' for r in self.state.response_requests)
                or any(q.status in ('pending', 'asked') and q.attempts < 2 for q in self.state.questions))

    async def resume_response(self):
        self.resume_pending = False
        if not self.has_resume_work():
            await self.emit('resume_response', {'status': 'skipped', 'message': '没有待回应请求或可主动提出的疑问'})
            return
        self.processing = self.resume_running = True
        snapshot = self.state.model_copy(deep=True)
        revisions = (self.revision, self.state.version, self.control_revision)
        allowed = {k: v for k, v in self.sources.items() if k.startswith('p') or k in self.processed_ids}

        def stale():
            return (self.muted or self.finishing or self.closed
                    or revisions != (self.revision, self.state.version, self.control_revision))

        recovery = []
        validate = student_result_validator(snapshot, allowed, set(), recovery,
            input_revision=revisions[0], resume=True)

        try:
            await self.emit('resume_response', {'status': 'running', 'message': '正在判断是否需要回应'})
            result = await self.llm.generate('student_resume', self.student_system + '\n' + prompts.RESUME,
                self.payload([], 'unmuted'), ResumeResult, history=list(self.conversation), result_validator=validate)
            if stale():
                self.resume_pending = not (self.muted or self.finishing or self.closed)
                await self.emit('resume_response', {'status': 'stale', 'message': '课堂或静音状态已变化，恢复结果已丢弃'})
                return
            proposed = StudentResult(**result.model_dump(), addressed=bool(result.candidate.response_request_ids))
            self.state, candidate = apply_result(self.state, proposed, allowed, set(), resume=True, input_revision=self.revision, allow_silence=bool(recovery))
            self.candidate = {'reply': candidate, 'revision': self.revision,
                              'control_revision': self.control_revision, 'addressed': bool(candidate.response_request_ids),
                              'resume': True}
            self.resume_failed = False
            self.last_block = None
            await self.candidate_ready()
            for detail in recovery:
                await self.emit('student_recovery', detail)
                if detail['code'] == 'question_release_denied':
                    await self.emit('question_release', {**detail, 'status': 'denied'})
            await self.emit('resume_response', {'status': 'generated' if candidate.kind != 'silence' else 'skipped',
                'message': '已生成恢复回应' if candidate.kind != 'silence' else '当前无需发言',
                'state': self.state.model_dump(), 'candidate': candidate.model_dump(), 'note': result.note})
        except (ModelError, ValueError) as exc:
            if stale():
                self.resume_pending = not (self.muted or self.finishing or self.closed)
                await self.emit('resume_response', {'status': 'stale', 'message': '过期恢复调用未提交'})
            else:
                self.resume_failed = True
                await self.emit('resume_response', {'status': 'failed', 'message': '恢复回应失败，可重试：' + str(exc),
                    'retryable': True, 'state': self.state.model_dump()})
        finally:
            self.processing = self.resume_running = False
            self.knowledge_review_wake.set()

    def retry_knowledge_reviews(self):
        for item in self.state.pending_reviews:
            item.status, item.error = 'pending', ''
        self.knowledge_review_wake.set()
        if self.resume_failed and not self.muted:
            self.resume_failed = False
            self.resume_pending = True
            self.student_work_wake.set()

    async def knowledge_review_loop(self):
        """课堂输入优先；复核只提交匹配快照的认知，失败不阻塞新输入。"""
        while True:
            await self.knowledge_review_wake.wait()
            self.knowledge_review_wake.clear()
            while not self.closed:
                # 已到达但尚未成段的教师内容也优先，完成讲授后会再次唤醒。
                if (self.processing or self.pending or self.asr_pending or self.review_processing
                        or not self.review_queue.empty() or not self.teaching_queue.empty()):
                    break
                known = {k.id: k for k in self.state.knowledge}
                pending = {r.knowledge_id: r for r in self.state.pending_reviews if r.status == 'pending'}
                targets = [kid for kid in dependency_order(known) if kid in pending][:16]
                if not targets:
                    break
                snapshot = self.state.model_copy(deep=True)
                input_revision, state_version = self.revision, self.state.version
                payload = self.payload([])
                payload['review_targets'] = targets
                evidence = {s for kid in targets for s in pending[kid].trigger_sources}
                allowed = {k: v for k, v in self.sources.items() if k.startswith('p') or k in self.processed_ids}

                def validate(result, final_attempt):
                    if {k.id for k in result.knowledge_updates} != set(targets):
                        raise ValueError('复核必须恰好返回 review_targets 指定的知识')
                    if any(k.supersedes for k in result.knowledge_updates):
                        raise ValueError('内部复核不能替代其他条目')
                    proposed = StudentResult(knowledge_updates=result.knowledge_updates)
                    return_value, _ = apply_result(snapshot, proposed, allowed, evidence)
                    if set(targets) & {r.knowledge_id for r in return_value.pending_reviews}:
                        raise ValueError('复核必须使用最新依赖；尚无依据时明确保留理解障碍')
                    return result

                self.knowledge_review_busy = True
                await self.emit('knowledge_review', {'status': 'running', 'targets': targets})
                try:
                    result = await self.llm.generate('knowledge_review', self.student_system + '\n' + prompts.KNOWLEDGE_REVIEW,
                        payload, KnowledgeReviewResult, result_validator=validate)
                    validate(result, True)
                    if self.revision != input_revision or self.state.version != state_version:
                        await self.emit('knowledge_review', {'status': 'stale', 'targets': targets})
                        # 新课堂完成后重新复核；不会用旧快照覆盖当前状态。
                        continue
                    self.state, _ = apply_result(self.state, StudentResult(knowledge_updates=result.knowledge_updates),
                                                 allowed, evidence)
                    await self.emit('knowledge_review', {'status': 'completed', 'targets': targets,
                        'state': self.state.model_dump(), 'note': result.note})
                except (ModelError, ValueError) as exc:
                    if self.revision != input_revision or self.state.version != state_version:
                        continue
                    for review in self.state.pending_reviews:
                        if review.knowledge_id in targets:
                            review.status, review.error = 'failed', str(exc)
                    await self.emit('knowledge_review', {'status': 'failed', 'targets': targets,
                        'state': self.state.model_dump(), 'message': str(exc), 'retryable': True})
                finally:
                    self.knowledge_review_busy = False

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
            if not self.teaching_queue.empty() or (self.resume_pending and self.resume_ready()):
                self.student_work_wake.set()

    async def try_speak(self, silence):
        if not self.candidate or self.finishing:
            return
        c = self.candidate
        if c.get('control_revision', self.control_revision) != self.control_revision:
            self.candidate = None
            return
        reason = release_error(self.state, c['reply'], self.revision)
        q = next((q for q in self.state.questions if q.id == c['reply'].question_id), None)
        if c['reply'].kind == 'question' and (not q or q.status == 'resolved'
                or ((q.status == 'deferred' or q.attempts >= 2) and not c['reply'].question_release_request)):
            reason = '疑问已解决或已暂缓，且没有有效例外许可'
        if reason:
            await self.cancel_candidate(reason)
            await self.emit('suppressed', {'reason': reason})
            return
        requests = {r.id: r for r in self.state.response_requests}
        if any(rid not in requests or requests[rid].status != 'pending' for rid in c['reply'].response_request_ids):
            await self.cancel_candidate('关联请求或知识已不可用')
            await self.emit('suppressed', {'reason': '教师请求已取消或已回应，这次回复已取消'})
            return
        facts = {k.id: k for k in self.state.knowledge}
        blocked = {r.knowledge_id for r in self.state.pending_reviews}
        if any(kid in blocked or kid not in facts or facts[kid].status in ('unclear', 'conflict', 'invalid')
               for kid in c['reply'].knowledge_ids):
            await self.cancel_candidate('关联请求或知识已不可用')
            await self.emit('suppressed', {'reason': '回复生成后所用知识已不可用，这次回复已取消'})
            return
        stale = c["revision"] != self.revision or c.get("image_id") != self.current_image
        reason = speech_block(muted=self.muted, stale=stale,
            busy=bool(self.processing or self.pending or self.asr_pending or self.review_processing
                      or not self.review_queue.empty() or not self.teaching_queue.empty()),
            speaking=self.active, silence=silence,
            required_pause=config.DIRECT_PAUSE if c["addressed"] else config.SPEAK_PAUSE,
            candidate=c["reply"], addressed=c["addressed"])
        if reason:
            new_reason = self.last_block != reason
            if new_reason:
                await self.emit("suppressed", {"reason": reason})
                self.last_block = reason
            if self.muted or stale or c["reply"].kind == "silence" or (not c["addressed"] and c["reply"].kind != "question"):
                await self.cancel_candidate(reason)
            elif c["reply"].question_release_request and new_reason:
                await self.release_event("waiting", c["reply"], reason)
            return
        self.candidate = None
        reply = c["reply"]
        mark_delivered(self.state, reply)
        self.history.append({"text": reply.text, "kind": reply.kind, "sources": reply.sources})
        self.conversation.append({'role': 'assistant', 'content': reply.text})
        await self.emit("reply", {**reply.model_dump(), "state": self.state.model_dump()})
        await self.release_event("delivered", reply, "实际回复已发出，累计次数加一；本次邀请已使用")
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
        changed = self.muted != muted
        self.muted = muted
        if changed:
            self.control_revision += 1
            await self.cancel_candidate("静音状态已变化")
            self.resume_failed = False
            self.resume_pending = not muted and not (self.finishing or self.closed)
            self.student_work_wake.set()
        if muted:
            self.candidate = None
            await self.stop_speech("静音已开启")
        await self.emit("mute", {"muted": muted})

    async def finish(self):
        self.finishing = True
        await self.cancel_candidate("试讲结束")
        self.resume_pending = False
        # 恢复任务属于同一课堂工作器；取消它后重启工作器处理末尾讲授。
        if self.resume_running:
            worker = self.tasks[2]
            worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker
            self.tasks[2] = asyncio.create_task(self.learn_loop())
        await self.stop_speech("试讲结束")
        await self.stop_audio()
        await self.flush("session_end")
        self.knowledge_review_wake.set()
        await self.emit("status", {"message": "正在处理最后的课堂内容"})
        deadline = time.monotonic() + 100
        while (self.processing or self.review_processing or self.knowledge_review_busy
               or any(r.status == 'pending' for r in self.state.pending_reviews) or not self.review_queue.empty()
               or not self.teaching_queue.empty()) and not self.failed_batch and time.monotonic() < deadline:
            await asyncio.sleep(.1)
        remaining = [s for s in self.sources if s.startswith("t") and s not in self.processed_ids]
        await self.emit("finished", {"state": self.state.model_dump(), "unprocessed_sources": remaining,
                                     "unreviewed_knowledge": [r.model_dump() for r in self.state.pending_reviews],
                                     "session_id": self.id, "lesson": self.lesson.model_dump(), "log_file": f"logs/{self.id}.jsonl"})
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
