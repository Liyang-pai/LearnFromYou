import numpy as np
import soundfile as sf
from asr.recognizer import AudioStream, Recognizer, Resampler
from backend import config


def test_resampling_preserves_rate_across_irregular_frames():
    y = np.sin(np.arange(48000) * (2 * np.pi * 440 / 48000)).astype(np.float32)
    expected = Resampler(48000).feed(y)
    converter = Resampler(48000)
    actual = np.concatenate([converter.feed(y[i:i+137]) for i in range(0,len(y),137)])
    assert abs(len(actual) - 16000) <= 1
    np.testing.assert_allclose(actual, expected, atol=1e-5)


def test_silence_does_not_create_speech_segments():
    audio = AudioStream(config.VAD_PATH,48000)
    segments=[]
    for _ in range(150):
        found,_,active=audio.feed(np.zeros(9600,dtype=np.float32)); segments+=found
        assert not active
    segments+=audio.finish()
    assert not segments


def test_existing_model_receives_resampled_recording():
    y, rate = sf.read(config.MODEL_DIR / 'test_wavs/zh.wav',dtype='float32')
    # Exercise the same 48kHz browser PCM -> resampler -> VAD -> recognizer path.
    samples = np.interp(np.arange(len(y) * 3)/3,np.arange(len(y)),y).astype(np.float32)
    assert rate == 16000
    audio=AudioStream(config.VAD_PATH,48000)
    segments=[]
    for i in range(0,len(samples),2048):
        found,_,_=audio.feed(samples[i:i+2048]); segments+=found
    segments+=audio.finish()
    assert segments
    recognizer=Recognizer(config.MODEL_DIR,2)
    text=''.join(recognizer.transcribe(s) for s in segments)
    assert '九点' in text and '五点' in text

