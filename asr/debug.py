"""Standalone microphone/file ASR diagnostic; no lesson, LLM, TTS, or saved audio."""
import asyncio
import contextlib
import time

import numpy as np

from .recognizer import AudioStream


class ASRDebug:
    def __init__(self, send, engine, audio, model_id, sample_rate, upload=None):
        self.send = send
        self.engine = engine
        self.audio = audio
        self.model_id = model_id
        self.sample_rate = sample_rate
        self.upload = upload
        self.queue = asyncio.Queue(maxsize=240)
        self.accepting = True
        self.frames = self.samples = self.segments = self.characters = 0
        self.asr_seconds = 0.0
        self.worker = asyncio.create_task(self.process())

    @classmethod
    async def create(cls, send, engine, vad_path, model_id, sample_rate, upload=None):
        audio = await asyncio.to_thread(AudioStream, vad_path, sample_rate)
        return cls(send, engine, audio, model_id, sample_rate, upload)

    async def emit(self, kind, data):
        await self.send({"type": kind, "data": data})

    async def accept(self, data):
        if not self.accepting:
            return
        if not data or len(data) % 4 or len(data) > 65536:
            raise ValueError("音频帧格式无效")
        samples = np.frombuffer(data, dtype="<f4").copy()
        if not np.isfinite(samples).all():
            raise ValueError("音频含无效数值")
        if self.upload:
            self.upload.accept(samples)
        if self.queue.full():
            self.accepting = False
            await self.emit("error", {"message": "识别处理积压，请结束录音并等待已接收内容处理完成。",
                                      "code": "audio_backlog"})
            return
        self.queue.put_nowait(samples)
        self.frames += 1
        self.samples += len(samples)
        if self.frames == 1 or self.frames % 25 == 0:
            await self.emit("audio_input", {"frames": self.frames, "audio_seconds": self.samples / self.sample_rate})

    async def decode(self, samples):
        started = time.perf_counter()
        text = await asyncio.to_thread(self.engine.transcribe, samples)
        seconds = time.perf_counter() - started
        duration = len(samples) / 16000
        self.segments += 1
        self.characters += len(text)
        self.asr_seconds += seconds
        await self.emit("transcript", {"index": self.segments, "model_id": self.model_id,
                                       "text": text, "asr_seconds": round(seconds, 3),
                                       "audio_seconds": round(duration, 3),
                                       "real_time_factor": round(seconds / max(duration, .001), 3),
                                       **(self.upload.metadata if self.upload else {})})

    async def process(self):
        while True:
            samples = await self.queue.get()
            try:
                if samples is None:
                    segments = await asyncio.to_thread(self.audio.finish)
                    for segment in segments:
                        await self.decode(segment)
                    return
                segments, transitions, _ = await asyncio.to_thread(self.audio.feed, samples)
                for speaking in transitions:
                    await self.emit("vad", {"speaking": speaking})
                for segment in segments:
                    await self.decode(segment)
                if self.upload:
                    await self.emit("upload_ack", self.upload.progress(len(samples)))
            finally:
                self.queue.task_done()

    async def finish(self):
        self.accepting = False
        # A failed worker must not leave a producer stuck on a full queue.
        enqueue = asyncio.create_task(self.queue.put(None))
        try:
            await asyncio.wait([enqueue, self.worker], return_when=asyncio.FIRST_COMPLETED)
            if self.worker.done():
                await self.worker
            else:
                await enqueue
                await self.worker
        finally:
            if not enqueue.done():
                enqueue.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await enqueue
        return {"model_id": self.model_id, "segments": self.segments, "characters": self.characters,
                "audio_seconds": round(self.samples / self.sample_rate, 3),
                "asr_seconds": round(self.asr_seconds, 3)}

    async def close(self):
        self.accepting = False
        if not self.worker.done():
            self.worker.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await self.worker
        self.audio = None
