#!/usr/bin/env python3
"""Run local, offline SenseVoice INT8 transcription on a WAV/FLAC file."""

from __future__ import annotations

import argparse
from pathlib import Path

import sherpa_onnx
import soundfile as sf


PROJECT_DIR = Path(__file__).resolve().parent
MODEL_DIR = PROJECT_DIR / "models" / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
DEFAULT_WAV = MODEL_DIR / "test_wavs" / "zh.wav"


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe audio locally with SenseVoice INT8.")
    parser.add_argument("audio", nargs="?", type=Path, default=DEFAULT_WAV, help="WAV/FLAC audio file")
    parser.add_argument("--threads", type=int, default=2, help="CPU inference threads (default: 2)")
    args = parser.parse_args()

    if not args.audio.is_file():
        parser.error(f"Audio file not found: {args.audio}")

    samples, sample_rate = sf.read(args.audio, dtype="float32", always_2d=False)
    if samples.ndim == 2:
        samples = samples.mean(axis=1)  # Stereo/multichannel -> mono

    recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
        model=str(MODEL_DIR / "model.int8.onnx"),
        tokens=str(MODEL_DIR / "tokens.txt"),
        num_threads=args.threads,
        use_itn=True,
        language="auto",
    )
    stream = recognizer.create_stream()
    stream.accept_waveform(sample_rate, samples)
    recognizer.decode_stream(stream)

    print(f"Audio: {args.audio}")
    print(f"Transcript: {stream.result.text}")


if __name__ == "__main__":
    main()
