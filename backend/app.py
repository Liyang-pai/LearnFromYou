import asyncio
import json
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from asr.recognizer import Recognizer
from . import config
from .llm import ModelError
from .schemas import Lesson
from .session import Session

engine = None
startup_error = None
active_session = None


@asynccontextmanager
async def lifespan(app):
    global engine, startup_error
    try:
        if not config.VAD_PATH.is_file():
            raise RuntimeError("缺少本地 VAD 模型")
        engine = await asyncio.to_thread(Recognizer, config.MODEL_DIR, config.ASR_THREADS)
    except Exception:
        startup_error = "本地 ASR 加载失败，请检查 asr/models 中的模型文件及 ASR_MODEL_DIR 配置"
    yield


app = FastAPI(title="听懂了吗 · AI 学生试讲", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.ROOT / "frontend"), name="static")


@app.get("/")
async def index():
    return FileResponse(config.ROOT / "frontend/index.html")


@app.get("/api/health")
async def health():
    return {"asr_ready": engine is not None, "model_configured": bool(config.API_KEY),
            "student_model": config.MODEL, "asr_model": config.MODEL_DIR.name,
            "vad_ready": config.VAD_PATH.is_file(), "error": startup_error}


@app.websocket("/ws/session")
async def websocket(ws: WebSocket):
    global active_session
    origin = ws.headers.get("origin")
    if origin:
        parsed = urlparse(origin)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.port != config.PORT:
            await ws.close(code=1008)
            return
    await ws.accept()
    session = None
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if session and session.ready and not session.finishing:
                    await session.accept_audio(message["bytes"])
                continue
            try:
                event = json.loads(message.get("text", "{}"))
                kind = event.get("type")
                if kind == "start":
                    if active_session and not active_session.closed:
                        raise ValueError("已有试讲正在运行，请先结束原试讲")
                    if not engine:
                        raise ValueError(startup_error or "ASR 尚未就绪")
                    lesson = Lesson.model_validate(event.get("lesson", {}))
                    session = Session(ws.send_json, engine)
                    active_session = session
                    await session.start(lesson, bool(event.get("muted", False)), bool(event.get("tts", True)))
                elif not session or not session.ready or session.closed:
                    raise ValueError("请先创建试讲")
                elif kind == "audio_start":
                    await session.start_audio(int(event.get("sample_rate", 0)))
                elif kind == "audio_stop":
                    await session.stop_audio()
                elif kind == "text":
                    await session.add_transcript(str(event.get("text", "")))
                    await session.flush("text_test")
                elif kind == "flush":
                    await session.flush("manual")
                elif kind == "mute":
                    await session.set_muted(bool(event.get("value")))
                elif kind == "tts":
                    session.tts_enabled = bool(event.get("value"))
                    if not session.tts_enabled:
                        await session.stop_speech("已关闭语音播报")
                elif kind == "stop_speech":
                    await session.stop_speech("手动停止播报")
                elif kind == "retry":
                    session.retry_event.set()
                    await session.emit("status", {"message": "正在重试，未处理内容仍按顺序保留"})
                elif kind == "end":
                    await session.finish()
            except (ValueError, ValidationError, ModelError) as e:
                if session:
                    await session.emit("error", {"message": str(e), "retryable": bool(session.failed_batch)})
                    if not session.ready:
                        await session.close()
                else:
                    await ws.send_json({"type": "error", "data": {"message": str(e)}})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        if session:
            await session.close()
        if active_session is session:
            active_session = None
