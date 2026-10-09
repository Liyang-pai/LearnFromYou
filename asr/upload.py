# 校验上传音频的元数据和 PCM 帧，跟踪已处理进度；不负责解码、VAD 或识别。
import re


class AudioUpload:
    MAX_SECONDS = 3600
    FRAME_SAMPLES = 2048

    def __init__(self, event, sample_rate):
        if type(sample_rate) is not int or not 8000 <= sample_rate <= 96000:
            raise ValueError("不支持的音频采样率")
        total = event.get("total_samples")
        upload_id = event.get("upload_id")
        filename = event.get("filename")
        if type(total) is not int or not 0 < total <= sample_rate * self.MAX_SECONDS:
            raise ValueError("上传音频不能为空，且时长不得超过 60 分钟")
        if not isinstance(upload_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", upload_id):
            raise ValueError("上传标识无效")
        if not isinstance(filename, str) or not filename.strip() or len(filename) > 255:
            raise ValueError("音频文件名无效")
        self.sample_rate = sample_rate
        self.total_samples = total
        self.received = self.processed = 0
        self.failed = False
        self.metadata = {"input_source": "audio_file", "upload_id": upload_id,
                         "filename": filename}

    @classmethod
    def from_event(cls, event, sample_rate):
        source = event.get("input_source", "microphone")
        if source == "microphone":
            return None
        if source != "audio_file":
            raise ValueError("不支持的音频来源")
        return cls(event, sample_rate)

    def accept(self, samples):
        if self.failed:
            raise ValueError("上传处理已失败，请停止本次上传")
        if not 0 < len(samples) <= self.FRAME_SAMPLES:
            raise ValueError("上传音频帧必须包含 1 至 2048 个采样点")
        if self.received != self.processed:
            raise ValueError("请等待上一帧处理确认后继续上传")
        if self.received + len(samples) > self.total_samples:
            raise ValueError("上传采样数超过声明的音频长度")
        self.received += len(samples)

    def progress(self, count=0):
        self.processed += count
        return {**self.metadata, "processed_samples": self.processed,
                "total_samples": self.total_samples,
                "audio_seconds": round(self.processed / self.sample_rate, 3)}

    def summary(self, cancelled=False):
        return {**self.progress(), "cancelled": bool(cancelled or self.processed < self.total_samples),
                "failed": self.failed}
