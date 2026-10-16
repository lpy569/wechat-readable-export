"""Validate portable image payloads without opening files or using the network."""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
import io
import warnings

from PIL import Image, UnidentifiedImageError

from .models import MediaAttachment

MAX_MEDIA_BYTES = 32 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
MAX_ANIMATION_FRAMES = 500
MAX_ANIMATION_PIXELS = 160_000_000
_KINDS = frozenset({"image", "sticker"})
_STATUSES = frozenset({"available", "missing", "unsupported"})
_FORMATS = {"image/jpeg": "JPEG", "image/png": "PNG", "image/gif": "GIF", "image/webp": "WEBP"}
_FIELDS = frozenset({"kind", "status", "mime_type", "data_base64", "reason"})


def sniff_mime(data: bytes) -> str | None:
    """Recognize only supported raster signatures; this alone is not validation."""
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def _validate_image(data: bytes, mime_type: str) -> None:
    if not data or len(data) > MAX_MEDIA_BYTES:
        raise ValueError("媒体文件为空或超过 32 MiB")
    if mime_type not in _FORMATS or sniff_mime(data) != mime_type:
        raise ValueError("媒体类型与受支持的图片内容不一致")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                if image.format != _FORMATS[mime_type]:
                    raise ValueError("媒体格式与 MIME 不一致")
                pixels = image.width * image.height
                if not pixels or pixels > MAX_IMAGE_PIXELS:
                    raise ValueError("图片尺寸超过安全上限（4000 万像素）")
                frames = getattr(image, "n_frames", 1)
                if frames > MAX_ANIMATION_FRAMES or pixels * frames > MAX_ANIMATION_PIXELS:
                    raise ValueError("动画帧数或总像素超过安全上限")
                image.verify()
            # verify() checks container integrity; decoding also rejects truncated
            # image data and malformed later animation frames before publication.
            with Image.open(io.BytesIO(data)) as image:
                decoded_pixels = 0
                for frame in range(frames):
                    image.seek(frame)
                    frame_pixels = image.width * image.height
                    decoded_pixels += frame_pixels
                    if frame_pixels > MAX_IMAGE_PIXELS or decoded_pixels > MAX_ANIMATION_PIXELS:
                        raise ValueError("图片尺寸超过安全上限")
                    image.load()
    except (UnidentifiedImageError, OSError, SyntaxError, EOFError,
            Image.DecompressionBombWarning, Image.DecompressionBombError) as exc:
        raise ValueError("媒体文件损坏或图片解码不受支持") from exc


def make_media(data: bytes, kind: str = "image", reason: str | None = None) -> MediaAttachment:
    """Embed a supported, verified raster; invalid input raises ValueError."""
    if not isinstance(kind, str) or kind not in _KINDS or not isinstance(data, bytes):
        raise ValueError("媒体 kind 或二进制数据无效")
    if reason is not None and not isinstance(reason, str):
        raise ValueError("媒体 reason 必须为字符串或 null")
    mime_type = sniff_mime(data)
    if mime_type is None:
        raise ValueError("仅支持 JPEG、PNG、GIF 和 WebP 图片")
    _validate_image(data, mime_type)
    return MediaAttachment(kind, "available", mime_type, base64.b64encode(data).decode("ascii"), reason)


def parse_media(value: dict | MediaAttachment | None) -> MediaAttachment | None:
    """Validate imported or in-memory media; never accept a path or remote URL."""
    if value is None:
        return None
    if isinstance(value, MediaAttachment):
        value = asdict(value)
    if not isinstance(value, dict) or set(value) - _FIELDS:
        raise ValueError("media 必须为受支持的媒体对象或 null")
    if not isinstance(value.get("kind"), str) or value["kind"] not in _KINDS:
        raise ValueError("media.kind 必须为 image 或 sticker")
    status = value.get("status", "missing")
    if not isinstance(status, str) or status not in _STATUSES:
        raise ValueError("media.status 无效")
    for field in ("mime_type", "data_base64", "reason"):
        if value.get(field) is not None and not isinstance(value[field], str):
            raise ValueError(f"media.{field} 必须为字符串或 null")
    media = MediaAttachment(**value)
    if status != "available":
        if media.mime_type is not None or media.data_base64 is not None:
            raise ValueError("缺失或不受支持的媒体不能附带图片数据")
        return media
    if media.mime_type not in _FORMATS or not media.data_base64:
        raise ValueError("可用媒体需要受支持的 MIME 和 Base64 图片数据")
    # Bound encoded size before decoding; strict base64 excludes attribute
    # delimiters, URL prefixes, whitespace, and non-ASCII input.
    if len(media.data_base64) > 4 * ((MAX_MEDIA_BYTES + 2) // 3):
        raise ValueError("媒体文件超过 32 MiB")
    try:
        decoded = base64.b64decode(media.data_base64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("media.data_base64 无效") from exc
    if base64.b64encode(decoded).decode("ascii") != media.data_base64:
        raise ValueError("media.data_base64 不是规范 Base64")
    _validate_image(decoded, media.mime_type)
    return media
