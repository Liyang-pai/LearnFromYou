#!/usr/bin/env python3
"""Opt-in comparison of installed models using the same labeled lecture audio."""
import argparse
import asyncio
import json
from pathlib import Path
import re
import sys
import time
import unicodedata

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from asr.manager import ModelManager
from asr.recognizer import AudioStream
from asr.transcribe import read_audio
from backend import config

CATEGORIES = {"plain", "mixed", "numbers", "continuous"}


def normalize(text):
    return "".join(c for c in unicodedata.normalize("NFKC", text).lower() if c.isalnum())


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, left in enumerate(a, 1):
        row = [i]
        for j, right in enumerate(b, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j-1] + (left != right)))
        previous = row
    return previous[-1]


def score(expected, actual, terms):
    reference, hypothesis = normalize(expected), normalize(actual)
    english_ref = re.findall(r"[a-z]+(?:'[a-z]+)?", expected.lower())
    english_hyp = re.findall(r"[a-z]+(?:'[a-z]+)?", actual.lower())
    return {"cer": edit_distance(reference, hypothesis) / max(1, len(reference)),
            "english_wer": edit_distance(english_ref, english_hyp) / len(english_ref) if english_ref else None,
            "term_hits": sum(term_hit(t, actual) for t in terms), "term_total": len(terms)}


def term_hit(term, text):
    normalized = normalize(term)
    if normalized.isascii():
        # English terms and digits must not match inside a longer word/number.
        term = unicodedata.normalize("NFKC", term).lower()
        text = unicodedata.normalize("NFKC", text).lower()
        pattern = r"(?<![a-z0-9_])" + re.escape(term) + r"(?![a-z0-9_])"
        return bool(re.search(pattern, text))
    return normalized in normalize(text)


def read_cases(path):
    cases = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    if not cases or not CATEGORIES.issubset({c.get("category") for c in cases}):
        raise ValueError("基准集需覆盖 plain、mixed、numbers、continuous 四类录音")
    for case in cases:
        if not isinstance(case.get("text"), str) or not normalize(case["text"]):
            raise ValueError("每条录音需要非空参考转写 text")
        case["path"] = (path.parent / case["audio"]).resolve()
        if not case["path"].is_file():
            raise ValueError(f"找不到音频：{case['path']}")
        if not isinstance(case.get("terms", []), list) or not all(isinstance(t, str) and normalize(t) for t in case.get("terms", [])):
            raise ValueError("terms 应是非空术语字符串的列表")
    return cases


async def compare(cases, model_ids):
    manager = ModelManager(config.MODELS_DIR, config.ASR_THREADS)
    installed = [m["id"] for m in manager.snapshot()["models"] if m["installed"]]
    chosen = model_ids or installed
    if not chosen:
        raise ValueError("请先在网页安装需要比较的模型")
    results = []
    try:
        for model_id in chosen:
            started = time.perf_counter()
            engine, vad_path, _ = await manager.acquire(model_id)
            load_seconds = time.perf_counter() - started
            for case in cases:
                samples, rate = read_audio(case["path"])
                audio = AudioStream(vad_path, rate)
                segments = []
                for i in range(0, len(samples), 2048):
                    found, _, _ = audio.feed(samples[i:i+2048])
                    segments.extend(found)
                segments.extend(audio.finish())
                started = time.perf_counter()
                text = " ".join(engine.transcribe(segment) for segment in segments)
                seconds = time.perf_counter() - started
                duration = len(samples) / rate
                results.append({"model_id": model_id, "category": case["category"],
                                "audio": case["audio"], "expected": case["text"], "transcript": text,
                                "audio_seconds": duration, "asr_seconds": seconds,
                                "real_time_factor": seconds / max(duration, .001),
                                "load_seconds": load_seconds,
                                **score(case["text"], text, case.get("terms", []))})
            await manager.release()
        return results
    finally:
        await manager.shutdown()


def main():
    parser = argparse.ArgumentParser(description="用同一批讲课录音比较本地已安装模型；不会自动下载")
    parser.add_argument("cases", type=Path, help="带参考转写的 JSONL，见 asr/README.md")
    parser.add_argument("--model", action="append", help="可重复；不指定时比较全部已安装模型")
    parser.add_argument("--output", type=Path, help="保存完整 JSON 结果；默认打印")
    args = parser.parse_args()
    try:
        cases = read_cases(args.cases)
        results = asyncio.run(compare(cases, args.model))
        report = json.dumps({"platform": sys.platform, "threads": config.ASR_THREADS,
                             "results": results}, ensure_ascii=False, indent=2)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(report + "\n", encoding="utf-8")
            print(f"结果已保存：{args.output}")
        else:
            print(report)
    except (ValueError, OSError, KeyError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()
