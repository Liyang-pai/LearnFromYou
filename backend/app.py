import asyncio
import json
import anyio
from collections import OrderedDict
from contextlib import asynccontextmanager
from urllib.parse import urlparse

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request, Query
from fastapi.responses import FileResponse, JSONResponse, Response, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.exception_handlers import request_validation_exception_handler
from pydantic import ValidationError

from asr.manager import ModelManager
from asr.debug import ASRDebug
from asr.upload import AudioUpload
from . import config
from .llm import ModelError
from .schemas import Lesson, TeachingSettings, StudentProfileResult, PROFILE_REQUIRED
from .student_profile import generate_profile
from .session import Session
from .runtime import RUNTIME
from .teaching_report import ReportService, export_markdown
from .classroom_images import MAX_IMAGE_BYTES, find_image
from .history import HistoryService, classroom_path
from .settings import SettingsUpdate, model_settings, public_settings, save_settings, test_connection

models = ModelManager(config.MODELS_DIR, config.ASR_THREADS)
active_session = None
classroom_starting = False
reports = ReportService()
histories = HistoryService()


def classroom_reserved():
    return classroom_starting or bool(active_session and not active_session.closed)


def require_asr_maintenance_available():
    if classroom_reserved():
        raise HTTPException(409, '请先结束正在创建或运行的试讲，再维护 ASR 模型')


@asynccontextmanager
async def lifespan(app):
    try:
        yield
    finally:
        if active_session:
            await active_session.close()
        await histories.close()
        await reports.close()
        await models.shutdown()


app = FastAPI(title="讲给我听 · Learn From You", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.ROOT / "frontend"), name="static")


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, exc: RequestValidationError):
    if request.url.path == '/api/settings':
        return JSONResponse({'detail': '设置格式无效，请检查密钥与开关选项'}, status_code=422)
    return await request_validation_exception_handler(request, exc)


def local_origin(origin):
    try:
        parsed = urlparse(origin)
        return parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost") and parsed.port == config.PORT
    except ValueError:
        return False


@app.middleware("http")
async def protect_local_actions(request: Request, call_next):
    # A remote page must not trigger downloads or open folders on this machine.
    if request.method == "POST" or request.url.path.startswith("/api/history"):
        origin = request.headers.get("origin")
        if (origin and not local_origin(origin)) or request.headers.get("sec-fetch-site") == "cross-site":
            return JSONResponse({"detail": "仅允许本机页面执行模型操作"}, status_code=403)
    response = await call_next(request)
    # This frontend is a development UI: never mix scripts from different edits.
    if request.url.path.startswith(("/static/", "/api/")) or not request.url.path.startswith('/ws/'):
        response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
@app.get('/create')
@app.get('/classroom')
@app.get('/classroom/details')
@app.get('/history')
@app.get('/history/{session_id}')
@app.get('/report/{session_id}')
@app.get('/settings')
@app.get('/settings/asr')
async def index(session_id: str | None = None):
    return FileResponse(config.ROOT / "frontend/formal/index.html")


@app.get('/debug')
@app.get('/debug/models')
@app.get('/debug/history')
@app.get('/debug/history/{session_id}')
async def debug_page(session_id: str | None = None):
    return FileResponse(config.ROOT / 'frontend/index.html')


@app.get('/api/history')
async def history_list(offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
    return await histories.listing(offset, limit)


@app.get('/api/history/{session_id}')
async def history_detail(session_id: str):
    try:
        return await histories.detail(session_id)
    except (ValueError, OSError) as exc:
        raise report_error(exc) from None


@app.get('/api/history/{session_id}/dialogue')
async def history_dialogue(session_id: str, before: int | None = Query(None, ge=0)):
    try:
        return await histories.dialogue(session_id, before)
    except (ValueError, OSError) as exc:
        raise report_error(exc) from None


@app.get('/api/history/{session_id}/log')
async def history_log(session_id: str):
    try:
        path = await asyncio.to_thread(classroom_path, session_id)
    except (ValueError, OSError) as exc:
        raise report_error(exc) from None
    return FileResponse(path, media_type='application/x-ndjson', filename=f'classroom-{session_id}.jsonl',
                        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})


@app.get("/api/health")
async def health():
    state = await asyncio.to_thread(models.snapshot)
    settings = public_settings()
    return {"asr_ready": state["asr_ready"], "asr_debug_available": True, "audio_upload_available": True, "model_configured": settings['model_configured'],
            "classroom_active": bool(active_session and not active_session.closed),
            "runtime": RUNTIME,
            "image_upload_available": True, "image_model": config.IMAGE_MODEL,
            "student_model": config.MODEL, "asr_model": state["selected_id"],
            "vad_ready": state["vad_ready"], "engine_state": state["engine_state"],
            "asr_busy": state["busy"], "asr_activity": state["activity"], "error": state["error"], "notice": state["notice"]}


@app.get("/models")
async def models_page():
    return RedirectResponse('/debug/models', status_code=307)


@app.get('/api/settings')
async def settings_get():
    try:
        return await asyncio.to_thread(public_settings)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post('/api/settings')
async def settings_save(update: SettingsUpdate):
    try:
        return await asyncio.to_thread(save_settings, update)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post('/api/settings/test')
async def settings_test():
    return await test_connection()


@app.post('/api/student-profile', response_model=StudentProfileResult)
async def student_profile(settings: TeachingSettings):
    try:
        return await generate_profile(settings)
    except (ModelError, ValidationError) as exc:
        raise HTTPException(502, '画像生成失败，原有内容已保留。' + str(exc)) from None


@app.post('/api/sessions/{session_id}/image')
async def upload_classroom_image(session_id: str, request: Request):
    session = active_session
    if (not session or session.id != session_id or not session.ready
            or session.closed or session.finishing):
        raise HTTPException(409, '请先创建并保持本次课堂，再上传截图')
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > MAX_IMAGE_BYTES:
            raise HTTPException(413, '截图不能超过 5 MiB，请裁剪后上传')
        chunks.append(chunk)
    try:
        return await session.set_image(b''.join(chunks))
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    except OSError:
        raise HTTPException(400, '截图保存失败，当前图片保留；请检查目录权限或磁盘空间') from None


@app.get('/api/sessions/{session_id}/image')
async def current_classroom_image(session_id: str, request: Request):
    origin = request.headers.get('origin')
    if (origin and not local_origin(origin)) or request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, '仅允许本机课堂页面读取截图')
    session = active_session
    if not session or session.id != session_id or session.closed:
        raise HTTPException(409, '当前课堂已结束或不存在')
    image = session.images.get(session.current_image)
    return image.public(session_id) if image else None


@app.post('/api/sessions/{session_id}/image/clear')
async def clear_classroom_image(session_id: str):
    session = active_session
    if not session or session.id != session_id or session.closed:
        raise HTTPException(409, '当前课堂已结束或不存在')
    try:
        await session.clear_image()
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None
    return {'cleared': True}


@app.get('/api/sessions/{session_id}/images/{image_id}')
async def classroom_image(session_id: str, image_id: str, request: Request):
    origin = request.headers.get('origin')
    if (origin and not local_origin(origin)) or request.headers.get('sec-fetch-site') == 'cross-site':
        raise HTTPException(403, '仅允许本机课堂页面读取截图')
    try:
        path, mime = await asyncio.to_thread(find_image, config.data_root(), session_id, image_id)
    except (ValueError, FileNotFoundError):
        raise HTTPException(404, '未找到本次课堂的图片') from None
    return FileResponse(path, media_type=mime, headers={'Cache-Control': 'no-store',
                        'Cross-Origin-Resource-Policy': 'same-origin', 'X-Content-Type-Options': 'nosniff'})


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
    require_asr_maintenance_available()
    try:
        return await models.download(model_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(400, str(exc)) from None


@app.post('/api/asr/models/{model_id}/verify')
async def verify_model(model_id: str):
    require_asr_maintenance_available()
    try:
        return await models.verify(model_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(409 if models.busy else 400, str(exc)) from None


@app.post('/api/asr/models/{model_id}/prepare-vad')
async def prepare_model_vad(model_id: str):
    require_asr_maintenance_available()
    try:
        return await models.prepare_vad(model_id)
    except (ValueError, OSError) as exc:
        raise HTTPException(409 if models.busy else 400, str(exc)) from None


@app.post("/api/asr/models/{model_id}/cancel")
async def cancel_download(model_id: str):
    try:
        return models.cancel(model_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from None


@app.post("/api/asr/models/{model_id}/select")
async def select_model(model_id: str):
    require_asr_maintenance_available()
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
                if classroom_reserved():
                    raise ValueError('已有试讲正在创建或运行，请先结束原试讲')
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
    global active_session, classroom_starting
    origin = ws.headers.get("origin")
    if origin:
        if not local_origin(origin):
            await ws.close(code=1008)
            return
    await ws.accept()
    session = None
    owns_engine = False
    owns_starting = False
    accepted_inputs = OrderedDict()
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
            event = {}
            try:
                event = json.loads(message.get("text", "{}"))
                if not isinstance(event, dict):
                    event = {}
                    raise ValueError('课堂消息格式无效，请发送 JSON 对象')
                kind = event.get("type")
                if kind == "start":
                    if classroom_reserved():
                        raise ValueError("已有试讲正在运行，请先结束原试讲")
                    lesson = Lesson.model_validate(event.get("lesson", {}))
                    defaults = public_settings()
                    muted, tts, asr_review = (event.get('muted', False),
                        event.get('tts', defaults['tts']), event.get('asr_review', defaults['asr_review']))
                    for value, label in ((muted, '静音'), (tts, '语音播报'), (asr_review, '语音转文字审核')):
                        if type(value) is not bool:
                            raise ValueError(label + '选项必须为布尔值')
                    # Claim synchronously and freeze configuration before sending
                    # status or awaiting ASR. A second socket cannot take this slot.
                    snapshot = model_settings()
                    classroom_starting = owns_starting = True
                    try:
                        await ws.send_json({"type": "status", "data": {"message": "正在加载本机 ASR 模型"}})
                        engine, vad_path, model_id = await models.acquire()
                        owns_engine = True
                        session = Session(ws.send_json, engine, vad_path, model_id, snapshot)
                        active_session = session
                        await session.start(lesson, muted, tts, asr_review)
                        accepted_inputs.clear()
                        histories.schedule(session.id)
                    finally:
                        classroom_starting = owns_starting = False
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
                    text = event.get('text')
                    if not isinstance(text, str) or not text.strip():
                        raise ValueError('请输入非空的讲授文字')
                    message_id = event.get('client_message_id')
                    if message_id is not None and (not isinstance(message_id, str) or not message_id or len(message_id) > 128):
                        raise ValueError('输入消息编号格式无效')
                    if message_id in accepted_inputs:
                        if accepted_inputs[message_id] != text:
                            raise ValueError('此输入消息编号已经用于其他讲授内容，请重新发送')
                        await session.emit('teacher_input_accepted', {'client_message_id': message_id})
                        continue
                    await session.add_transcript(text)
                    await session.flush("text_test")
                    if message_id:
                        accepted_inputs[message_id] = text
                        while len(accepted_inputs) > 512:
                            accepted_inputs.popitem(last=False)
                        await session.emit('teacher_input_accepted', {'client_message_id': message_id})
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
                    session.retry_knowledge_reviews()
                    await session.emit("status", {"message": "正在重试，未处理内容仍按顺序保留"})
                elif kind == "end":
                    # A normal end must register the independent report even
                    # when the browser disconnects during tail processing.
                    with anyio.CancelScope(shield=True):
                        await session.finish()
                        histories.schedule(session.id)
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
                else:
                    raise ValueError('不支持的课堂操作')
            except (ValueError, ValidationError, ModelError) as e:
                message = PROFILE_REQUIRED if PROFILE_REQUIRED in str(e) else str(e)
                if owns_engine and (session is None or session.closed):
                    await models.release()
                    owns_engine = False
                if session and not session.closed:
                    await session.emit("error", {"message": message, "retryable": bool(session.failed_batch),
                                                 **({'client_message_id': str(event['client_message_id'])[:128]} if event.get('client_message_id') else {})})
                    if not session.ready:
                        await session.close()
                        if owns_engine:
                            await models.release()
                            owns_engine = False
                        if active_session is session:
                            active_session = None
                else:
                    await ws.send_json({"type": "error", "data": {"message": message,
                        **({'client_message_id': str(event['client_message_id'])[:128]} if event.get('client_message_id') else {})}})
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        # A disconnect/server cancellation must finish releasing native workers.
        with anyio.CancelScope(shield=True):
            if owns_starting:
                classroom_starting = False
            if session:
                await session.close()
                histories.schedule(session.id)
            if owns_engine:
                await models.release()
            if active_session is session:
                active_session = None
