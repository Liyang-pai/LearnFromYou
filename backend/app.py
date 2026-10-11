import asyncio
import json
import anyio
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from asr.manager import ModelManager
from asr.debug import ASRDebug
from asr.upload import AudioUpload
from . import config
from .llm import ModelError
from .schemas import Lesson
from .session import Session
from .teaching_report import ReportService, export_markdown

models = ModelManager(config.MODELS_DIR, config.ASR_THREADS)
active_session = None
reports = ReportService()


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        if active_session:
            await active_session.close()
        await reports.close()
        await models.shutdown()


app = FastAPI(title="讲给我听 · Learn From You（开发调试）", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.ROOT / "frontend"), name="static")


def local_origin(origin):
    try:
        parsed = urlparse(origin)
        return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == config.PORT
    except ValueError:
        return False


@app.middleware("http")
async def protect_local_actions(request: Request, call_next):
    # A remote page must not trigger downloads or open folders on this machine.
    if request.method == "POST":
        origin = request.headers.get("origin")
        if (origin and not local_origin(origin)) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "仅允许本机页面执行模型操作"}, status_code=403)
    response = await call_next(request)
    # This frontend is a development UI: never mix scripts from different edits.
    if request.url.path in ("/", "/models") or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
async def index():
    return FileResponse(config.ROOT / "frontend/index.html")


@app.get("/api/health")
async def health():
    state = await asyncio.to_thread(models.snapshot)
    return {"asr_ready": state["asr_ready"], "asr_debug_available": True, "audio_upload_available": True, "model_configured": bool(config.API_KEY),
            "student_model": config.MODEL, "asr_model": state["selected_id"],
            "vad_ready": state["vad_ready"], "engine_state": state["engine_state"],
            "asr_busy": state["busy"], "asr_activity": state["activity"], "error": state["error"], "notice": state["notice"]}


@app.get("/models")
async def models_page():
    return FileResponse(config.ROOT / "frontend/index.html")


@app.get("/api/asr/models")
async def model_list():
    return await asyncio.to_thread(models.snapshot)


def report_error(exc):
    if isinstance(exc, FileNotFoundError):
        return HTTPException(404, '未找到这次课堂记录')
    return HTTPException(400, str(exc))


@app.get('/api/teaching-report/{session_id}')
async def teaching_report(session_id: str):
    try:
        return await asyncio.to_thread(reports.get, session_id)
    except (ValueError, FileNotFoundError) as exc:
        raise report_error(exc) from None


@app.post('/api/teaching-report/{session_id}')
async def generate_teaching_report(session_id: str):
    try:
        return await reports.start(session_id)
    except (ValueError, FileNotFoundError) as exc:
        raise report_error(exc) from None


@app.get('/api/teaching-report/{session_id}/export')
async def download_teaching_report(session_id: str):
    try:
        result = await asyncio.to_thread(reports.get, session_id)
    except (ValueError, FileNotFoundError) as exc:
        raise report_error(exc) from None
    try:
        text = export_markdown(result)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return Response(text, media_type='text/markdown; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="teaching-report-{session_id}.md"'})


@app.post("/api/asr/models/{model_id}/download")
async def download_model(model_id: str):
    try:
        return await models.download(model_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/asr/models/{model_id}/cancel")
async def cancel_download(model_id: str):
    try:
        return models.cancel(model_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/asr/models/{model_id}/select")
async def select_model(model_id: str):
    try:
        return await asyncio.to_thread(models.select, model_id)
    except ValueError as exc:
        raise HTTPException(409 if models.busy else 400, str(exc)) from None


@app.post("/api/asr/open-folder")
async def open_model_folder():
    try:
        await asyncio.to_thread(models.open_folder)
        return {"opened": True}
    except OSError:
        raise HTTPException(400, "无法打开文件夹，请复制页面显示的路径手动打开") from None


@app.websocket("/ws/asr-debug")
async def asr_debug(ws: WebSocket):
    origin = ws.headers.get("origin")
    if origin and not local_origin(origin):
        await ws.close(code=1008)
        return
    await ws.accept()
    debug = None
    owns_engine = False
    try:
        while True:
            # Also observe the worker: a native decode failure should immediately
            # report and release the model, even if the browser sends no more frames.
            incoming = asyncio.create_task(ws.receive())
            try:
                targets = [incoming] + ([debug.worker] if debug else [])
                await asyncio.wait(targets, return_when=asyncio.FIRST_COMPLETED)
                if debug and debug.worker.done():
                    await debug.worker
                    raise ValueError("ASR 调试处理已停止，请重新开始测试")
                message = await incoming
            finally:
                if not incoming.done():
                    incoming.cancel()
                    with anyio.CancelScope(shield=True):
                        try:
                            await incoming
                        except asyncio.CancelledError:
                            pass
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if debug:
                    await debug.accept(message["bytes"])
                else:
                    raise ValueError("请先开始 ASR 调试")
                continue
            event = json.loads(message.get("text", "{}"))
            if not isinstance(event, dict):
                raise ValueError("调试消息格式无效")
            if event.get("type") == "start":
                if debug:
                    raise ValueError("ASR 调试已经开始")
                rate = event.get("sample_rate")
                if type(rate) is not int or not 8000 <= rate <= 96000:
                    raise ValueError("不支持的麦克风采样率")
                upload = AudioUpload.from_event(event, rate)
                await ws.send_json({"type": "status", "data": {"message": "正在加载当前 ASR 模型……"}})
                engine, vad_path, model_id = await models.acquire(purpose="debug")
                owns_engine = True
                debug = await ASRDebug.create(ws.send_json, engine, vad_path, model_id, rate, upload)
                await debug.emit("ready", {"model_id": model_id, "sample_rate": rate,
                                           **(upload.progress() if upload else {})})
            elif event.get("type") == "stop" and debug:
                await debug.emit("status", {"message": "正在处理末尾录音，请稍等……"})
                summary = await debug.finish()
                await debug.close()
                await models.release()
                owns_engine = False
                if debug.upload:
                    await debug.emit("upload_finished", debug.upload.summary(event.get("cancelled", False)))
                    summary.update(debug.upload.summary(event.get("cancelled", False)))
                await debug.emit("finished", summary)
                break
            else:
                raise ValueError("请使用开始或结束 ASR 调试操作")
    except WebSocketDisconnect:
        pass
    except Exception as exc:
        text = str(exc) if isinstance(exc, ValueError) else "本机 ASR 调试失败，请检查模型后重新测试"
        try:
            await ws.send_json({"type": "error", "data": {"message": text, "code": "debug_failure"}})
        except (WebSocketDisconnect, RuntimeError):
            pass
    finally:
        with anyio.CancelScope(shield=True):
            if debug:
                await debug.close()
            if owns_engine:
                await models.release()
            try:
                await ws.close()
            except (WebSocketDisconnect, RuntimeError):
                pass


@app.websocket("/ws/session")
async def websocket(ws: WebSocket):
    global active_session
    origin = ws.headers.get("origin")
    if origin:
        if not local_origin(origin):
            await ws.close(code=1008)
            return
    await ws.accept()
    session = None
    owns_engine = False
    try:
        while True:
            message = await ws.receive()
            if message["type"] == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                if session and session.ready and not session.finishing:
                    try:
                        await session.accept_audio(message["bytes"])
                    except ValueError as exc:
                        session.audio_enabled = False
                        if session.upload:
                            session.upload.failed = True
                        await session.emit("error", {"message": str(exc), "code": "audio_failure"})
                continue
            try:
                event = json.loads(message.get("text", "{}"))
                kind = event.get("type")
                if kind == "start":
                    if active_session and not active_session.closed:
                        raise ValueError("已有试讲正在运行，请先结束原试讲")
                    lesson = Lesson.model_validate(event.get("lesson", {}))
                    asr_review = event.get("asr_review", True)
                    if type(asr_review) is not bool:
                        raise ValueError("语音转文字审核选项必须为布尔值")
                    await ws.send_json({"type": "status", "data": {"message": "正在加载本机 ASR 模型"}})
                    engine, vad_path, model_id = await models.acquire()
                    owns_engine = True
                    session = Session(ws.send_json, engine, vad_path, model_id)
                    active_session = session
                    await session.start(lesson, bool(event.get("muted", False)), bool(event.get("tts", True)), asr_review)
                elif not session or not session.ready or session.closed:
                    raise ValueError("请先创建试讲")
                elif kind == "audio_start":
                    rate = event.get("sample_rate", 0)
                    if type(rate) is not int:
                        raise ValueError("音频采样率必须为整数")
                    await session.start_audio(rate, AudioUpload.from_event(event, rate))
                elif kind == "audio_stop":
                    await session.stop_audio(event.get("cancelled", False))
                elif kind == "text":
                    if session.upload:
                        raise ValueError("请先完成或停止音频上传，再发送文字")
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
                    # A normal end must register the independent report even
                    # when the browser disconnects during tail processing.
                    with anyio.CancelScope(shield=True):
                        await session.finish()
                        await models.release()
                        owns_engine = False
                        if active_session is session:
                            active_session = None
                        try:
                            await reports.start(session.id)
                        except (ValueError, FileNotFoundError) as exc:
                            try:
                                await ws.send_json({'type': 'report_unavailable',
                                                    'data': {'session_id': session.id, 'message': str(exc)}})
                            except (WebSocketDisconnect, RuntimeError):
                                pass
            except (ValueError, ValidationError, ModelError) as e:
                if session and not session.closed:
                    await session.emit("error", {"message": str(e), "retryable": bool(session.failed_batch)})
                    if not session.ready:
                        await session.close()
                        if owns_engine:
                            await models.release()
                            owns_engine = False
                        if active_session is session:
                            active_session = None
                else:
                    await ws.send_json({"type": "error", "data": {"message": str(e)}})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        # A disconnect/server cancellation must finish releasing native workers.
        with anyio.CancelScope(shield=True):
            if session:
                await session.close()
            if owns_engine:
                await models.release()
            if active_session is session:
                active_session = None
