"""Bounded, immutable local screenshot files; no model calls or document parsing."""
import base64
from dataclasses import dataclass
import hashlib
from io import BytesIO
from pathlib import Path
import re
import uuid

from PIL import Image, UnidentifiedImageError

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_CLASSROOM_BYTES = 64 * 1024 * 1024
MAX_IMAGE_PIXELS = 16_000_000
FORMATS = {'PNG': ('png', 'image/png'), 'JPEG': ('jpg', 'image/jpeg'), 'WEBP': ('webp', 'image/webp')}


def image_directory(root, session_id):
    if not re.fullmatch(r'[0-9a-f]{12}', session_id):
        raise ValueError('课堂编号无效')
    logs = (Path(root) / 'logs').resolve()
    directory = (logs / (session_id + '.images')).resolve()
    if not directory.is_relative_to(logs):
        raise ValueError('图片目录无效')
    return directory


@dataclass(frozen=True)
class ClassroomImage:
    id: str
    path: Path
    mime: str
    width: int
    height: int
    size: int

    def public(self, session_id):
        return {'id': self.id, 'url': f'/api/sessions/{session_id}/images/{self.id}',
                'width': self.width, 'height': self.height, 'size': self.size, 'mime': self.mime}

    def data_url(self):
        try:
            data = self.path.read_bytes()
        except OSError:
            raise ValueError('已保存图片不可用，请重新上传') from None
        if len(data) > MAX_IMAGE_BYTES or hashlib.sha256(data).hexdigest() != self.id:
            raise ValueError('已保存图片不可用，请重新上传')
        return f'data:{self.mime};base64,' + base64.b64encode(data).decode('ascii')


def save_image(root, session_id, data):
    if not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError('请选择不超过 5 MiB 的 PNG、JPEG 或 WebP 截图')
    try:
        with Image.open(BytesIO(data), formats=list(FORMATS)) as image:
            width, height = image.size
            if width > 8192 or height > 8192 or width * height > MAX_IMAGE_PIXELS:
                raise ValueError('图片尺寸过大，请裁剪到当前讲授区域后再上传')
            if getattr(image, 'is_animated', False):
                raise ValueError('请上传静态截图，不支持动画图片')
            extension, mime = FORMATS[image.format]
            image.verify()
        # Header identification is insufficient: reject truncated pixel streams.
        with Image.open(BytesIO(data), formats=list(FORMATS)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, Image.DecompressionBombError) as exc:
        raise ValueError('图片无效或损坏，请重新截图；支持 PNG、JPEG、WebP') from None
    digest = hashlib.sha256(data).hexdigest()
    directory = image_directory(root, session_id)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (digest + '.' + extension)
    if not path.exists():
        used = sum(p.stat().st_size for p in directory.iterdir() if p.suffix in ('.png', '.jpg', '.webp'))
        if used + len(data) > MAX_CLASSROOM_BYTES:
            raise ValueError('本次课堂截图已达到 64 MiB，请结束课堂后开启新试讲')
        temporary = directory / (uuid.uuid4().hex + '.tmp')
        try:
            temporary.write_bytes(data)
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)
    return ClassroomImage(digest, path, mime, width, height, len(data))


def find_image(root, session_id, image_id):
    if not re.fullmatch(r'[0-9a-f]{64}', image_id):
        raise ValueError('图片编号无效')
    directory = image_directory(root, session_id)
    for extension, mime in FORMATS.values():
        path = directory / (image_id + '.' + extension)
        if path.is_file() and path.resolve().parent == directory:
            return path, mime
    raise FileNotFoundError('图片不存在')
