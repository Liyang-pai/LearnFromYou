r"""Live assessment smoke test; uses the running server and configured real LLM.

Run from the project root: .venv\Scripts\python.exe scripts/verify_assessment.py
Uses text input to isolate assessment from microphone/ASR. Leaves a server JSONL log.
"""
import asyncio
import json
from pathlib import Path

import websockets


async def main():
    phases = []
    async with websockets.connect("ws://127.0.0.1:8765/ws/session") as ws:
        async def send(event):
            await ws.send(json.dumps(event, ensure_ascii=False))

        async def receive(kind):
            while True:
                event = json.loads(await asyncio.wait_for(ws.recv(), timeout=120))
                if event["type"] == "error":
                    raise RuntimeError(event["data"]["message"])
                if event["type"] == "llm_request":
                    phases.append(event["data"]["phase"])
                if event["type"] == kind:
                    return event["data"]

        await send({"type": "start", "lesson": {"topic": "十以内加减法"}, "tts": False})
        ready = await receive("ready")
        assert ready["assessment_available"] is True, "实际运行的后台没有启用通用测验"
        print("ready: assessment_available=true", flush=True)
        await send({"type": "text", "text": (
            "今天学习十以内加减法。加法表示把两部分合起来，减法表示从原有数量中去掉一部分，求剩下的数量。"
            "计算可以逐个数：加上几就向后数几步，减去几就向前数几步。"
            "例如3个苹果再放入2个，从3往后数4、5，所以3+2=5；5个苹果拿走2个，往前数4、3，所以5-2=3。"
            "同一个情境先增加再减少时，按事情发生的顺序计算，先算增加后的数量，再从这个数量减去拿走的数量。"
            "例如原来2支笔，增加3支后是5支，再拿走1支剩4支。今天的计算数量都不超过10。"
        )})
        await receive("state")
        await send({"type": "assessment", "kind": "apply"})
        result = await receive("assessment_result")
        print("question:", result["question"]["text"], flush=True)
        print("answer:", result["answer"]["text"], flush=True)
        print("evaluation:", result["evaluation"]["verdict"], result["evaluation"]["coverage"], flush=True)
        await send({"type": "end"})
        finished = await receive("finished")
        assert finished["assessments"] == [result]
        assert not finished["unprocessed_sources"]
        expected = {"assessment_question", "assessment_audit", "assessment_answer", "assessment_evaluation"}
        assert expected <= set(phases), phases
        log_path = Path(__file__).resolve().parents[1] / finished["log_file"]
        records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        assert any(e["type"] == "assessment_result" and e["data"] == result for e in records)
        assert next(e["data"] for e in records if e["type"] == "finished")["assessments"] == [result]
        print("phases:", phases, flush=True)
        print("finished assessments:", len(finished["assessments"]), flush=True)
        print("verified server log:", log_path, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
