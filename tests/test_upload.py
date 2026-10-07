# 验证上传协议、发送确认、取消和来源保存，以及上传进入课堂审核和学生链路的行为。
import asyncio
import json
import threading

import numpy as np
import pytest
from fastapi.testclient import TestClient

from asr.debug import ASRDebug
from asr.upload import AudioUpload
from asr.recognizer import AudioStream
from backend import config
from test_asr_debug import debug_service, FakeAudio, receive, TextEngine
from test_models import manager
from test_review import make_session, ReviewModel, until


def upload_event(total=4096, rate=16000):
    return {"input_source": "audio_file", "upload_id": "fixture-1", "filename": "讲解.wav",
            "sample_rate": rate, "total_samples": total}


@pytest.mark.parametrize("change", [
    {"total_samples": 0}, {"total_samples": True}, {"total_samples": 9600001},
    {"upload_id": ""}, {"filename": ""}, {"input_source": "unknown"},
])
def test_metadata_rejected_before_model_acquisition(debug_service, change):
    service, models, engines = debug_service
    with TestClient(service.app) as client:
        assert client.get('/api/health').json()['audio_upload_available'] is True
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type': 'start', **upload_event(), **change})
            receive(ws, 'error')
        assert not models.busy and not engines


def test_upload_drains_tail_with_source_and_cancellation(debug_service):
    service, models, engines = debug_service
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type': 'start', **upload_event()})
            assert receive(ws, 'ready')['upload_id'] == 'fixture-1'
            ws.send_bytes(np.zeros(2048, dtype='<f4').tobytes())
            assert receive(ws, 'transcript')['filename'] == '讲解.wav'
            assert receive(ws, 'upload_ack')['processed_samples'] == 2048
            ws.send_json({'type': 'stop', 'cancelled': True})
            assert receive(ws, 'transcript')['index'] == 2
            summary = receive(ws, 'upload_finished')
            assert summary['cancelled'] and not summary['failed']
            assert summary['processed_samples'] == 2048
            assert receive(ws, 'finished')['input_source'] == 'audio_file'
        assert not models.busy and engines[0].closed
        assert service.active_session is None


def test_upload_disconnect_releases_model_without_replay(debug_service):
    service, models, engines = debug_service
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type': 'start', **upload_event()})
            receive(ws, 'ready')
            ws.send_bytes(np.zeros(2048, dtype='<f4').tobytes())
            assert receive(ws, 'transcript')['index'] == 1
            receive(ws, 'upload_ack')
        assert not models.busy and engines[0].closed
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type': 'start', **upload_event()})
            receive(ws, 'ready')
            ws.send_json({'type': 'stop', 'cancelled': True})
            # Only the fake VAD's tail is decoded; the previous connection is never replayed.
            assert receive(ws, 'transcript')['index'] == 1
            assert receive(ws, 'upload_finished')['processed_samples'] == 0
            assert receive(ws, 'finished')['segments'] == 1
        assert not models.busy


def test_upload_native_failure_releases_model(debug_service, monkeypatch):
    service, models, engines = debug_service
    def failed(self, samples): raise RuntimeError('decode failure')
    monkeypatch.setattr(TextEngine, 'transcribe', failed)
    with TestClient(service.app) as client:
        with client.websocket_connect('/ws/asr-debug') as ws:
            ws.send_json({'type': 'start', **upload_event()})
            receive(ws, 'ready')
            ws.send_bytes(np.zeros(2048, dtype='<f4').tobytes())
            assert receive(ws, 'error')['code'] == 'debug_failure'
        assert not models.busy and engines[0].closed


def test_ack_waits_for_slow_recognition():
    gate = threading.Event()
    entered = threading.Event()
    class SlowEngine:
        def transcribe(self, samples):
            entered.set()
            assert gate.wait(3)
            return '测试内容'
    async def run():
        events = []
        async def send(event): events.append(event)
        debug = ASRDebug(send, SlowEngine(), FakeAudio(None, 16000), 'fake', 16000,
                         AudioUpload(upload_event(), 16000))
        try:
            await debug.accept(np.zeros(2048, dtype='<f4').tobytes())
            await until(entered.is_set)
            assert not any(e['type'] == 'upload_ack' for e in events)
            with pytest.raises(ValueError, match='等待上一帧'):
                await debug.accept(np.zeros(2048, dtype='<f4').tobytes())
            gate.set()
            await until(lambda: any(e['type'] == 'upload_ack' for e in events))
            await debug.accept(np.zeros(2048, dtype='<f4').tobytes())
            await debug.finish()
            assert debug.upload.processed == 4096
            assert not debug.upload.summary()['cancelled']
        finally:
            gate.set()
            await debug.close()
    asyncio.run(run())


@pytest.mark.parametrize('enabled,fallback', [(False, False), (True, False), (True, True)])
def test_classroom_upload_enters_review_learning_and_logs(tmp_path, monkeypatch, enabled, fallback):
    async def run():
        model = ReviewModel()
        if fallback: model.review_output = {'segments': []}
        session, _, events = await make_session(tmp_path, monkeypatch, model, enabled=enabled)
        monkeypatch.setattr('backend.session.AudioStream', FakeAudio)
        session.recognizer = TextEngine('fake')
        try:
            await session.start_audio(16000, AudioUpload(upload_event(), 16000))
            with pytest.raises(ValueError, match='停止当前音频'):
                await session.start_audio(16000)
            await session.accept_audio(np.zeros(2048, dtype='<f4').tobytes())
            await until(lambda: any(e['type'] == 'upload_ack' for e in events))
            await session.stop_audio(cancelled=True)
            await until(lambda: len(session.processed_ids) == 2)
            assert all(s['filename'] == '讲解.wav' and s['mode'] == 'microphone' and
                       s['input_source'] == 'audio_file' for s in session.sources.values()
                       if s['id'].startswith('t'))
            assert session.sources['t1']['review_status'] == ('fallback' if fallback else 'reviewed' if enabled else 'disabled')
            assert bool(model.payloads('asr_review')) == enabled
            assert [s['id'] for p in model.payloads('student') for s in p['new_teacher_segments']] == ['t1', 't2']
            finished = next(e['data'] for e in events if e['type'] == 'upload_finished')
            assert finished['cancelled'] and session.audio is None
            # Switching back cannot attach the previous file's metadata to microphone segments.
            await session.start_audio(16000)
            await session.accept_audio(np.zeros(1000, dtype='<f4').tobytes())
            await session.stop_audio()
            assert 'input_source' not in session.sources['t3']
        finally: await session.close()
        log = [json.loads(line) for line in (tmp_path / 'logs' / f'{session.id}.jsonl').read_text().splitlines()]
        raw = next(e['data'] for e in log if e['type'] == 'transcript')
        assert raw['filename'] == '讲解.wav' and raw['upload_id'] == 'fixture-1'
    asyncio.run(run())


def test_audio_failure_stops_sending_and_flushes_existing_content(tmp_path, monkeypatch):
    async def run():
        session, _, events = await make_session(tmp_path, monkeypatch, enabled=False)
        class BrokenAudio(FakeAudio):
            def feed(self, samples): raise RuntimeError('bad VAD')
            def finish(self): raise RuntimeError('bad tail')
        monkeypatch.setattr('backend.session.AudioStream', BrokenAudio)
        try:
            await session.start_audio(16000, AudioUpload(upload_event(), 16000))
            await session.accept_audio(np.zeros(2048, dtype='<f4').tobytes())
            await session.audio_queue.join()
            assert not session.audio_enabled and session.upload.failed
            await session.stop_audio(True)
            assert next(e['data'] for e in events if e['type'] == 'upload_finished')['failed']
            assert session.upload is None
        finally: await session.close()
    asyncio.run(run())


def test_protocol_limits_empty_oversize_and_declared_length():
    upload = AudioUpload(upload_event(total=1), 16000)
    for samples in [np.zeros(0), np.zeros(2049), np.zeros(2)]:
        with pytest.raises(ValueError): upload.accept(samples)
    upload.accept(np.zeros(1))
    upload.progress(1)
    assert upload.summary()['cancelled'] is False


def test_finish_session_flushes_upload_before_end(tmp_path, monkeypatch):
    async def run():
        session, model, events = await make_session(tmp_path, monkeypatch)
        monkeypatch.setattr('backend.session.AudioStream', FakeAudio)
        session.recognizer = TextEngine('fake')
        await session.start_audio(16000, AudioUpload(upload_event(total=2048), 16000))
        await session.accept_audio(np.zeros(2048, dtype='<f4').tobytes())
        await session.finish()
        assert session.closed and len(session.processed_ids) == 2
        kinds = [e['type'] for e in events]
        assert kinds.index('upload_finished') < kinds.index('finished')
        assert model.payloads('student')
    asyncio.run(run())


VAD = config.MODELS_DIR / '_shared/silero_vad.onnx'
SAMPLE = config.MODELS_DIR / 'sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/test_wavs/zh.wav'


@pytest.mark.skipif(not VAD.is_file() or not SAMPLE.is_file(), reason='需要本地 VAD 和语音样本')
def test_upload_and_microphone_produce_identical_real_vad_segments():
    import soundfile as sf
    speech, rate = sf.read(SAMPLE, dtype='float32')
    # Include silence, speech longer than the VAD limit, and speech ending without a final pause.
    samples = np.concatenate([np.zeros(rate), np.tile(speech[int(.3*rate):-int(.7*rate)], 4),
                              np.zeros(rate), speech[:int(4*rate)]]).astype('<f4')
    class CaptureEngine:
        def __init__(self): self.segments = []
        def transcribe(self, audio): self.segments.append(audio.copy()); return '内容'
    async def run():
        engines = []
        for source in [None, AudioUpload(upload_event(len(samples), rate), rate)]:
            engine = CaptureEngine(); engines.append(engine)
            async def send(event): pass
            debug = ASRDebug(send, engine, AudioStream(VAD, rate), 'fake', rate, source)
            try:
                for offset in range(0, len(samples), 2048):
                    await debug.accept(samples[offset:offset+2048].tobytes())
                    if source: await debug.queue.join()
                await debug.finish()
            finally: await debug.close()
        assert len(engines[0].segments) >= 2
        assert len(engines[0].segments) == len(engines[1].segments)
        for microphone, upload in zip(engines[0].segments, engines[1].segments):
            np.testing.assert_array_equal(microphone, upload)
    asyncio.run(run())
