"""Local installation state and cancellable downloads; no weights live in Git."""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

import anyio
import httpx

from .catalog import CATALOG, LEGACY_ID, VAD
from .recognizer import Recognizer

logger = logging.getLogger(__name__)


class DownloadCancelled(Exception):
    pass


class ModelManager:
    def __init__(self, root: Path, threads=2, catalog=CATALOG, vad=VAD, transport=None):
        self.root = Path(root)
        self.threads = threads
        self.catalog = {m.id: m for m in catalog}
        self.vad = vad
        self.transport = transport  # Tests use a local HTTP transport, never real weights.
        self.lock = threading.RLock()
        self.jobs = {}
        self.download_task = None
        self.cancel_event = None
        self.busy = False
        self.activity = None
        self.loading = False
        self.engine = None
        self.error = None
        self.verified = {}
        self.invalid = {}
        self.record_write_errors = set()
        self.settings_error = None
        self.selected_id = None
        settings = self.root / ".settings.json"
        try:
            choice = json.loads(self._safe_file(settings).read_text(encoding="utf-8")).get("selected_id")
            if choice is not None and not isinstance(choice, str):
                raise ValueError("模型选择配置格式无效")
            self.selected_id = choice
        except FileNotFoundError:
            # Adopt only the pre-existing developer installation, never another model.
            if LEGACY_ID in self.catalog and self._installed(self.catalog[LEGACY_ID]):
                self.selected_id = LEGACY_ID
                self._save_selection()
        except (OSError, ValueError, AttributeError):
            self.settings_error = "本地模型选择配置无法读取，请重新选择模型。"

    def model(self, model_id):
        if model_id not in self.catalog:
            raise ValueError("未知模型，请从模型列表中选择")
        return self.catalog[model_id]

    def directory(self, model):
        path = self.root / model.directory
        # Downloads and runtime state must stay inside models even after a manual move.
        if path.is_symlink() or path.resolve().parent != self.root.resolve():
            raise ValueError("模型目录不能是指向其他位置的链接")
        return path

    def _safe_file(self, path):
        if path.is_symlink() or not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("模型文件不能是指向其他位置的链接")
        return path

    def _signature(self, path):
        self._safe_file(path)
        stat = path.stat()
        if not path.is_file():
            raise ValueError("模型文件无效")
        return [stat.st_size, stat.st_mtime_ns]

    def _installed(self, model):
        try:
            directory = self.directory(model)
            if not model.downloadable:
                return all(self._signature(directory / f.name)[0] > 0 for f in model.files)
            marker = self._safe_file(directory / ".installed.json")
            record = json.loads(marker.read_text(encoding="utf-8"))
            if record["revision"] != model.revision:
                return False
            signatures = {f.name: self._signature(directory / f.name) for f in model.files}
            changed = []
            for asset in model.files:
                previous = record["files"][asset.name]
                if (not isinstance(previous, list) or len(previous) != 2
                        or any(type(value) is not int for value in previous)
                        or signatures[asset.name][0] != asset.size
                        or self.invalid.get((str(directory / asset.name), asset.sha256)) == signatures[asset.name]):
                    return False
                if signatures[asset.name] != previous:
                    changed.append(asset)
            for asset in changed:
                self._verify(directory / asset.name, asset)
            # Check the whole set again: an earlier file may change while another hashes.
            if any(self._signature(directory / f.name) != signatures[f.name] for f in model.files):
                return False
            if changed:
                record["files"] = signatures
                try:
                    self._write_record(directory, record)
                    self.record_write_errors.discard(str(marker))
                except (OSError, ValueError):
                    # Verified files remain usable even on a read-only copied installation.
                    if str(marker) not in self.record_write_errors:
                        logger.warning("模型安装记录无法更新：%s", marker, exc_info=True)
                        self.record_write_errors.add(str(marker))
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False

    def _write_record(self, directory, record):
        marker = self._safe_file(directory / ".installed.json")
        temporary = self._safe_file(directory / ".installed.json.part")
        try:
            temporary.write_text(json.dumps(record), encoding="utf-8")
            temporary.replace(marker)
        finally:
            temporary.unlink(missing_ok=True)

    @property
    def vad_path(self):
        shared = self.root / "_shared" / self.vad.name
        if shared.is_file():
            return shared
        # Compatibility only: do not move or delete the existing developer VAD.
        return self.root / self.vad.name

    def _vad_ready(self):
        try:
            signature = self._signature(self.vad_path)
            return (signature[0] == self.vad.size
                    and self.invalid.get((str(self.vad_path), self.vad.sha256)) != signature)
        except (OSError, ValueError):
            return False

    def _save_selection(self):
        self.root.mkdir(parents=True, exist_ok=True)
        target = self._safe_file(self.root / ".settings.json")
        temporary = self._safe_file(self.root / ".settings.json.part")
        temporary.write_text(json.dumps({"selected_id": self.selected_id}), encoding="utf-8")
        temporary.replace(target)

    def snapshot(self):
        with self.lock:
            notice = self.settings_error
            selected = self.catalog.get(self.selected_id)
            if self.selected_id and (not selected or not self._installed(selected)) and not self.busy:
                self.selected_id = None
                self.error = None
                notice = "当前模型已被删除或文件不完整，请重新选择模型。"
                self.settings_error = notice
                self._save_selection()
            models = []
            for model in self.catalog.values():
                if not model.downloadable and not (self.root / model.directory).is_dir():
                    continue
                installed = self._installed(model)
                job = dict(self.jobs.get(model.id, {}))
                state = "installed" if installed else "available"
                if not installed and (self.root / model.directory).exists():
                    state = "incomplete"
                if job.get("state") in ("downloading", "verifying", "cancelling", "failed", "cancelled"):
                    state = job["state"]
                size = model.size
                if not model.downloadable and installed:
                    try:
                        size = sum((self.root / model.directory / f.name).stat().st_size for f in model.files)
                    except OSError:
                        installed, state = False, "incomplete"
                models.append({"id": model.id, "name": model.name, "directory": model.directory,
                               "languages": model.languages, "description": model.description,
                               "label": model.label, "source": model.source,
                               "license_url": model.license_url, "revision": model.revision,
                               "size_bytes": size, "download_bytes": size,
                               "downloadable": model.downloadable, "installed": installed,
                               "selected": self.selected_id == model.id, "state": state, "job": job,
                               "files": [self._file_detail(self.directory(model) / asset.name, asset)
                                         for asset in model.files]})
            ready = bool(self.selected_id and self._installed(self.catalog[self.selected_id]) and self._vad_ready())
            return {"models": models, "selected_id": self.selected_id, "models_dir": str(self.root.resolve()),
                    "vad_ready": self._vad_ready(), "asr_ready": ready,
                    "busy": self.busy, "activity": self.activity, "engine_state": "loading" if self.loading else
                    "loaded" if self.engine else "failed" if self.error else "idle",
                    "error": self.error, "notice": notice,
                    "vad": self._file_detail(self.vad_path, self.vad)}

    def _file_detail(self, path, asset):
        try:
            signature = self._signature(path)
            cache_key = (str(path), asset.sha256)
            state = ('verified' if self.verified.get(cache_key) == signature else
                     'invalid' if self.invalid.get(cache_key) == signature else 'present')
            return {'name': asset.name, 'size_bytes': signature[0], 'expected_bytes': asset.size,
                    'sha256': asset.sha256, 'state': state}
        except (OSError, ValueError):
            return {'name': asset.name, 'size_bytes': 0, 'expected_bytes': asset.size,
                    'sha256': asset.sha256, 'state': 'missing'}

    def select(self, model_id):
        with self.lock:
            if self.busy:
                raise ValueError("试讲或 ASR 调试创建、进行或结束处理中，不能切换模型")
            if self.download_task and not self.download_task.done():
                raise ValueError('模型维护正在进行，请等待完成或取消后再切换模型')
            model = self.model(model_id)
            if not self._installed(model):
                raise ValueError("模型尚未安装完成，请先下载或重新下载")
            previous = self.selected_id
            self.selected_id = model_id
            try:
                self._save_selection()
            except OSError:
                self.selected_id = previous
                raise ValueError("模型选择无法保存，请检查 models 目录权限") from None
            self.settings_error = self.error = None
            return self.snapshot()

    def _verify(self, path, asset, cancel=None):
        signature = self._signature(path)
        cache_key = (str(path), asset.sha256)
        if signature[0] != asset.size:
            raise ValueError(f"{asset.name} 文件大小不正确，请重新下载")
        if self.verified.get(cache_key) == signature:
            return
        if self.invalid.get(cache_key) == signature:
            raise ValueError(f"{asset.name} 校验失败，请重新下载")
        digest = hashlib.sha256()
        with path.open("rb") as file:
            while chunk := file.read(1024 * 1024):
                if cancel and cancel.is_set():
                    raise DownloadCancelled()
                digest.update(chunk)
        if self._signature(path) != signature:
            raise ValueError(f"{asset.name} 校验失败，请重新下载")
        if digest.hexdigest() != asset.sha256:
            self.invalid[cache_key] = signature
            self.verified.pop(cache_key, None)
            raise ValueError(f"{asset.name} 校验失败，请重新下载")
        self.verified[cache_key] = signature
        self.invalid.pop(cache_key, None)

    def _create_engine(self, model):
        directory = self.directory(model)
        if model.downloadable:
            for asset in model.files:
                self._verify(directory / asset.name, asset)
        self._verify(self.vad_path, self.vad)
        return Recognizer(directory, self.threads, model=model)

    def _prepare_acquire(self, model_id, purpose):
        with self.lock:
            if self.busy:
                raise ValueError("已有试讲或 ASR 调试正在创建或运行，请先结束当前任务")
            if self.download_task and not self.download_task.done():
                raise ValueError("模型文件正在下载或修复，请等待完成或取消后再创建试讲")
            self.snapshot()
            chosen = model_id or self.selected_id
            if not chosen or not self._installed(self.model(chosen)) or not self._vad_ready():
                raise ValueError("请先在 ASR 模型页面下载并选择模型；如 VAD 缺失，请点击准备 VAD")
            self.busy = self.loading = True
            self.activity = purpose
            self.error = None
            model = self.model(chosen)
            return model

    async def acquire(self, model_id=None, purpose="lesson"):
        preparation = asyncio.create_task(asyncio.to_thread(self._prepare_acquire, model_id, purpose))
        try:
            model = await asyncio.shield(preparation)
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                try:
                    await preparation
                except Exception:
                    pass
                else:
                    with self.lock:
                        self.busy = self.loading = False
                        self.activity = None
            raise
        task = asyncio.create_task(asyncio.to_thread(self._create_engine, model))
        try:
            self.engine = await asyncio.shield(task)
            self.loading = False
            return self.engine, self.vad_path, model.id
        except asyncio.CancelledError:
            with anyio.CancelScope(shield=True):
                try:
                    engine = await task
                    await asyncio.to_thread(engine.close)
                finally:
                    self.busy = self.loading = False
                    self.activity = None
            raise
        except Exception as exc:
            self.busy = self.loading = False
            self.activity = None
            self.error = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else "本地 ASR 加载失败，请检查模型文件或重新下载"
            raise ValueError(self.error) from None

    async def release(self):
        # close() waits for any native decode to leave its lock before releasing files.
        engine = self.engine
        if engine:
            await asyncio.to_thread(engine.close)
        with self.lock:
            self.engine = None
            self.busy = self.loading = False
            self.activity = None

    def _update_job(self, model_id, **values):
        with self.lock:
            self.jobs[model_id].update(values)

    def _check_maintenance(self, model_id):
        model = self.model(model_id)
        if self.busy:
            raise ValueError('请先结束试讲或 ASR 调试再维护模型')
        if self.download_task and not self.download_task.done():
            raise ValueError('已有模型维护正在进行，请等待完成或取消')
        return model

    async def verify(self, model_id):
        """Explicitly hash local assets again; never download or load inference."""
        with self.lock:
            model = self._check_maintenance(model_id)
            self.cancel_event = threading.Event()
            self.jobs[model_id] = {'state': 'verifying', 'action': 'verify', 'model_id': model_id,
                                   'verified': False, 'error': None, 'files': []}
            self.download_task = asyncio.create_task(asyncio.to_thread(self._verify_local, model, self.cancel_event))
            task = self.download_task
        await asyncio.shield(task)
        with self.lock:
            return dict(self.jobs[model_id])

    def _verify_local(self, model, cancel):
        files = []
        error = None
        try:
            directory = self.directory(model)
            assets = [(directory / asset.name, asset) for asset in model.files]
            assets.append((self.vad_path, self.vad))
            for path, asset in assets:
                if cancel.is_set():
                    raise DownloadCancelled()
                try:
                    if asset.sha256:
                        # An explicit verification must not trust a cached signature.
                        self.verified.pop((str(path), asset.sha256), None)
                        self.invalid.pop((str(path), asset.sha256), None)
                        self._verify(path, asset, cancel)
                    elif self._signature(path)[0] <= 0:
                        raise ValueError(f'{asset.name} 文件无效')
                except (OSError, ValueError) as exc:
                    error = str(exc) if isinstance(exc, ValueError) else f'{asset.name} 缺失或无法读取'
                    files.append({**self._file_detail(path, asset), 'error': error})
                else:
                    files.append(self._file_detail(path, asset))
            if cancel.is_set():
                raise DownloadCancelled()
            # Valid loose/copied standard files become selectable without a download.
            if not error and model.downloadable:
                self._write_record(directory, {'revision': model.revision,
                    'files': {asset.name: self._signature(directory / asset.name) for asset in model.files}})
            self._update_job(model.id, state='failed' if error else 'verified' if model.downloadable else 'checked',
                             verified=not error and model.downloadable, files=files, error=error,
                             message=('开发兼容模型没有固定校验清单，仅检查文件存在' if not model.downloadable else None),
                             vad_ready=self._vad_ready())
        except DownloadCancelled:
            self._update_job(model.id, state='cancelled', verified=False, files=files)
        except (OSError, ValueError):
            self._update_job(model.id, state='failed', verified=False, files=files,
                             error='本地文件校验或校验记录保存失败，请检查文件与目录权限')

    async def prepare_vad(self, model_id):
        """Prepare the shared VAD only, retaining model assets and selection."""
        with self.lock:
            model = self._check_maintenance(model_id)
            self.root.mkdir(parents=True, exist_ok=True)
            shared = self._safe_file(self.root / '_shared')
            shared.mkdir(exist_ok=True)
            if shutil.disk_usage(self.root).free < self.vad.size + 10 * 1024 * 1024:
                raise ValueError('磁盘空间不足，无法准备 VAD')
            self.cancel_event = threading.Event()
            self.jobs[model_id] = {'state': 'downloading', 'action': 'prepare_vad', 'downloaded_bytes': 0,
                                   'total_bytes': self.vad.size, 'file': self.vad.name, 'error': None}
            self.download_task = asyncio.create_task(asyncio.to_thread(self._prepare_vad, model, self.cancel_event))
            return dict(self.jobs[model_id])

    def _prepare_vad(self, model, cancel):
        try:
            with httpx.Client(follow_redirects=True, transport=self.transport,
                              timeout=httpx.Timeout(15, connect=10)) as client:
                self._fetch(client, self.vad, self.root / '_shared' / self.vad.name, model.id, 0, cancel)
            if cancel.is_set():
                raise DownloadCancelled()
            self._update_job(model.id, state='prepared', file='')
        except DownloadCancelled:
            self._update_job(model.id, state='cancelled', file='')
        except (httpx.HTTPError, OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else (
                '官方 VAD 源连接失败，请检查网络或代理后重试' if isinstance(exc, httpx.HTTPError)
                else 'VAD 文件写入失败，请检查磁盘空间与目录权限')
            self._update_job(model.id, state='failed', error=message)

    async def download(self, model_id):
        with self.lock:
            model = self.model(model_id)
            if not model.downloadable:
                raise ValueError("现有开发模型不提供下载，请选择标准模型")
            if self.busy:
                raise ValueError("请先结束试讲或 ASR 调试再下载或修复模型")
            if self.download_task and not self.download_task.done():
                raise ValueError("已有下载正在进行，请等待完成或取消后再下载")
            self.root.mkdir(parents=True, exist_ok=True)
            self.directory(model).mkdir(exist_ok=True)
            shared = self._safe_file(self.root / "_shared")
            shared.mkdir(exist_ok=True)
            # No archives or duplicate precision variants. Allow room for a replacement
            # installation while the previous files still exist.
            required = model.size + self.vad.size + 100 * 1024 * 1024
            if shutil.disk_usage(self.root).free < required:
                raise ValueError(f"磁盘空间不足，至少需要 {required / 1e9:.2f} GB 可用空间")
            self.cancel_event = threading.Event()
            total = model.size + self.vad.size
            self.jobs[model_id] = {"state": "downloading", "downloaded_bytes": 0,
                                   "total_bytes": total, "file": "", "error": None}
            self.download_task = asyncio.create_task(asyncio.to_thread(self._install, model, self.cancel_event))
            return dict(self.jobs[model_id])

    def cancel(self, model_id):
        with self.lock:
            self.model(model_id)
            if self.jobs.get(model_id, {}).get("state") not in ("downloading", "verifying", "cancelling"):
                raise ValueError("这个模型没有正在进行的下载")
            self.cancel_event.set()
            self.jobs[model_id]["state"] = "cancelling"
            return dict(self.jobs[model_id])

    def _fetch(self, client, asset, target, model_id, offset, cancel):
        self._safe_file(target)
        if cancel.is_set():
            raise DownloadCancelled()
        if target.exists():
            try:
                self._verify(target, asset, cancel)
                self._update_job(model_id, downloaded_bytes=offset + asset.size)
                return
            except ValueError:
                pass
        partial = self._safe_file(target.with_name(target.name + ".part"))
        self._update_job(model_id, state="downloading", file=asset.name)
        try:
            with client.stream("GET", asset.url) as response:
                response.raise_for_status()
                size = 0
                with partial.open("wb") as file:
                    for chunk in response.iter_bytes(256 * 1024):
                        if cancel.is_set():
                            raise DownloadCancelled()
                        size += len(chunk)
                        if size > asset.size:
                            raise ValueError(f"{asset.name} 下载大小超过清单，请重试")
                        file.write(chunk)
                        self._update_job(model_id, downloaded_bytes=offset + size)
            self._update_job(model_id, state="verifying")
            self._verify(partial, asset, cancel)
            if cancel.is_set():
                raise DownloadCancelled()
            partial.replace(target)
        finally:
            partial.unlink(missing_ok=True)

    def _install(self, model, cancel):
        try:
            directory = self.directory(model)
            marker = self._safe_file(directory / ".installed.json")
            marker.unlink(missing_ok=True)
            offset = 0
            with httpx.Client(follow_redirects=True, transport=self.transport,
                              timeout=httpx.Timeout(15, connect=10)) as client:
                for asset in model.files:
                    self._fetch(client, asset, directory / asset.name, model.id, offset, cancel)
                    offset += asset.size
                self._fetch(client, self.vad, self.root / "_shared" / self.vad.name, model.id, offset, cancel)
            if cancel.is_set():
                raise DownloadCancelled()
            record = {"revision": model.revision,
                      "files": {f.name: self._signature(directory / f.name) for f in model.files}}
            self._write_record(directory, record)
            self._update_job(model.id, state="installed", file="")
        except DownloadCancelled:
            self._update_job(model.id, state="cancelled", file="")
        except (httpx.HTTPError, OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, ValueError) else (
                "官方模型源连接失败，请检查网络或代理后重试" if isinstance(exc, httpx.HTTPError)
                else "模型文件写入失败，请检查磁盘空间与目录权限后重试")
            self._update_job(model.id, state="failed", error=message)

    def open_folder(self):
        self.root.mkdir(parents=True, exist_ok=True)
        path = str(self.root.resolve())
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])

    async def shutdown(self):
        if self.download_task and not self.download_task.done():
            self.cancel_event.set()
            await self.download_task
        await self.release()
