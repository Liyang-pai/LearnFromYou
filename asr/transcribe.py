#!/usr/bin/env python3
"""Transcribe WAV/FLAC locally with the same selected model as the web UI."""
import argparse
import asyncio
from pathlib import Path
import sys

import soundfile as sf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asr.catalog import CATALOG, LEGACY_DIRECTORY
from asr.manager import ModelManager
from backend import config


def read_audio(path):
    samples, rate = sf.read(path, dtype="float32", always_2d=True)
    return samples.mean(axis=1), rate


def main():
    parser = argparse.ArgumentParser(description="使用已安装的本地 ASR 模型识别 WAV/FLAC")
    parser.add_argument("audio", nargs="?", type=Path, help="音频文件")
    parser.add_argument("--model", choices=[m.id for m in CATALOG], help="只为本次命令指定模型，不修改网页选择")
    parser.add_argument("--threads", type=int, default=config.ASR_THREADS)
    args = parser.parse_args()
    if args.threads < 1:
        parser.error("线程数必须大于零")
    audio = args.audio or config.MODELS_DIR / LEGACY_DIRECTORY / "test_wavs/zh.wav"
    if not audio.is_file():
        parser.error("请提供音频路径，例如：python asr/transcribe.py /完整路径/录音.wav")
    manager = ModelManager(config.MODELS_DIR, args.threads)

    async def run():
        try:
            engine, _, model_id = await manager.acquire(args.model)
            samples, rate = read_audio(audio)
            text = await asyncio.to_thread(engine.transcribe, samples, rate)
            print(f"Audio: {audio}\nModel: {model_id}\nTranscript: {text}")
        finally:
            await manager.shutdown()
    try:
        asyncio.run(run())
    except (ValueError, OSError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
