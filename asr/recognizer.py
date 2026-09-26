"""Persistent SenseVoice model + per-session VAD. No audio leaves the machine."""
from pathlib import Path
import threading
import numpy as np
import sherpa_onnx


class Recognizer:
    def __init__(self, model_dir: Path, threads=2):
        self.lock = threading.Lock()
        self.engine = sherpa_onnx.OfflineRecognizer.from_sense_voice(
            model=str(model_dir / "model.int8.onnx"), tokens=str(model_dir / "tokens.txt"),
            num_threads=threads, use_itn=True, language="auto")

    def transcribe(self, samples, sample_rate=16000):
        with self.lock:
            stream = self.engine.create_stream()
            stream.accept_waveform(sample_rate, np.asarray(samples, dtype=np.float32))
            self.engine.decode_stream(stream)
            return stream.result.text.strip()


class Resampler:
    """Streaming linear interpolation with a persistent phase across browser frames."""
    def __init__(self, source_rate, target_rate=16000):
        self.step = source_rate / target_rate
        self.buffer = np.empty(0, dtype=np.float32)
        self.position = 0.0

    def feed(self, samples):
        self.buffer = np.concatenate((self.buffer, samples))
        if len(self.buffer) < 2:
            return np.empty(0, dtype=np.float32)
        positions = np.arange(self.position, len(self.buffer) - 1, self.step)
        out = np.interp(positions, np.arange(len(self.buffer)), self.buffer).astype(np.float32)
        if len(positions):
            self.position = positions[-1] + self.step
        drop = min(int(self.position), len(self.buffer) - 1)
        self.buffer = self.buffer[drop:]
        self.position -= drop
        return out


class AudioStream:
    def __init__(self, vad_path, sample_rate):
        c = sherpa_onnx.VadModelConfig()
        c.silero_vad.model = str(vad_path)
        c.silero_vad.min_silence_duration = .6
        c.silero_vad.min_speech_duration = .25
        c.silero_vad.max_speech_duration = 12
        c.sample_rate = 16000
        c.num_threads = 1
        self.vad = sherpa_onnx.VoiceActivityDetector(c, buffer_size_in_seconds=60)
        self.resampler = Resampler(sample_rate)
        self.buffer = np.empty(0, dtype=np.float32)
        self.active = False

    def feed(self, samples):
        samples = self.resampler.feed(samples)
        self.buffer = np.concatenate((self.buffer, samples))
        segments, transitions = [], []
        while len(self.buffer) >= 512:
            self.vad.accept_waveform(self.buffer[:512])
            self.buffer = self.buffer[512:]
            active = self.vad.is_speech_detected()
            if active != self.active:
                transitions.append(active)
                self.active = active
            while not self.vad.empty():
                segments.append(np.array(self.vad.front.samples, dtype=np.float32, copy=True))
                self.vad.pop()
        return segments, transitions, self.active

    def finish(self):
        if len(self.buffer):
            self.vad.accept_waveform(np.pad(self.buffer, (0, 512 - len(self.buffer))))
            self.buffer = np.empty(0, dtype=np.float32)
        self.vad.flush()
        segments = []
        while not self.vad.empty():
            segments.append(np.array(self.vad.front.samples, dtype=np.float32, copy=True))
            self.vad.pop()
        self.active = False
        return segments

