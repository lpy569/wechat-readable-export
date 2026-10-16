"""Render locally cached native emoji without altering original message text.

This module contains no artwork and performs no network requests. Cache assets
are validated raster PNGs, and exported metadata carries only the used tiles.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import asdict
import html
import io
import json
import os
from pathlib import Path
import re
import struct

from PIL import Image, UnidentifiedImageError

from .media_payload import parse_media
from .models import Conversation

MAX_NATIVE_ASSETS = 256
MAX_NATIVE_BYTES = 64 * 1024
MAX_NATIVE_TOTAL_BYTES = 4 * 1024 * 1024
MAX_NATIVE_CACHE_BYTES = 6 * 1024 * 1024
_TOKEN = re.compile(r"\[[^\[\]\x00-\x1f\x7f]{1,16}\]")
_DATA_URI = re.compile(r"data:image/png;base64,[A-Za-z0-9+/]+={0,2}\Z")
_FIELDS = frozenset({"kind", "status", "mime_type", "data_base64", "reason"})
_CACHE_WARNING = "本机原生表情缓存无效，已保留文字标签。"


def native_tokens(text: str) -> list[str]:
    """Return distinct bracket candidates in their original order."""
    return list(dict.fromkeys(_TOKEN.findall(text)))


def default_cache_path() -> Path:
    """Return the per-user cache location without creating any directories."""
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        root = Path(os.environ["LOCALAPPDATA"])
    elif os.environ.get("XDG_CACHE_HOME"):
        root = Path(os.environ["XDG_CACHE_HOME"])
    else:
        root = Path.home() / ".cache"
    return root / "wechat-readable-export" / "native-emoji-v1.json"


def _validate_assets(value: object) -> dict[str, dict]:
    if not isinstance(value, dict) or len(value) > MAX_NATIVE_ASSETS:
        raise ValueError("原生表情资源必须为不超过 256 项的对象")
    result = {}
    total = 0
    for token, payload in value.items():
        if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
            raise ValueError("原生表情标签无效")
        if not isinstance(payload, dict) or set(payload) - _FIELDS:
            raise ValueError("原生表情资源字段无效")
        if (payload.get("kind") != "sticker" or payload.get("status") != "available"
                or payload.get("mime_type") != "image/png" or payload.get("reason") is not None):
            raise ValueError("原生表情仅接受可用的 PNG 表情图片")
        encoded = payload.get("data_base64")
        if not isinstance(encoded, str) or not encoded or len(encoded) > 4 * ((MAX_NATIVE_BYTES + 2) // 3):
            raise ValueError("原生表情图片为空或超过 64 KiB")
        try:
            decoded = base64.b64decode(encoded, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("原生表情 Base64 无效") from exc
        if not decoded or len(decoded) > MAX_NATIVE_BYTES:
            raise ValueError("原生表情图片为空或超过 64 KiB")
        if base64.b64encode(decoded).decode("ascii") != encoded:
            raise ValueError("原生表情 Base64 必须为规范编码")
        total += len(decoded)
        if total > MAX_NATIVE_TOTAL_BYTES:
            raise ValueError("原生表情资源总大小超过 4 MiB")
        if len(decoded) < 24 or not decoded.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("原生表情必须为 PNG 图片")
        width, height = struct.unpack(">II", decoded[16:24])
        if not 0 < width <= 128 or not 0 < height <= 128:
            raise ValueError("原生表情尺寸不能超过 128 × 128")
        try:
            with Image.open(io.BytesIO(decoded)) as image:
                if (image.format != "PNG" or not 0 < image.width <= 128
                        or not 0 < image.height <= 128 or getattr(image, "n_frames", 1) != 1):
                    raise ValueError("原生表情必须为不超过 128 × 128 的静态 PNG")
        except (UnidentifiedImageError, OSError, SyntaxError, EOFError,
                Image.DecompressionBombError) as exc:
            raise ValueError("原生表情图片损坏") from exc
        # This also verifies PNG integrity and fully decodes the raster.
        media = parse_media(payload)
        result[token] = asdict(media)
    return result


def _read_cache(path: Path) -> dict[str, dict]:
    with path.open("rb") as handle:
        data = handle.read(MAX_NATIVE_CACHE_BYTES + 1)
    if len(data) > MAX_NATIVE_CACHE_BYTES:
        raise ValueError("原生表情缓存文件过大")
    value = json.loads(data.decode("utf-8"))
    if isinstance(value, dict) and "assets" in value:
        if (set(value) - {"schema_version", "source", "assets"}
                or type(value.get("schema_version")) is not int or value["schema_version"] != 1
                or not isinstance(value.get("source"), str)
                or len(value["source"]) > 2048):
            raise ValueError("原生表情缓存格式无效")
        value = value["assets"]
    return _validate_assets(value)


def prepare_native_assets(conversation: Conversation, cache_path: Path | None = None) -> None:
    """Validate, select and embed used local assets before staging exports.

    Imported metadata takes precedence over the optional local cache. A missing
    cache leaves text labels readable. A corrupt implicit cache adds a warning;
    invalid supplied metadata or an explicit corrupt cache fails validation.
    """
    embedded = _validate_assets(conversation.metadata.get("native_emoji_assets", {}))
    used = dict.fromkeys(
        token
        for message in conversation.messages
        for text in (message.content, message.transcript or "")
        for token in native_tokens(text)
    )
    cached = {}
    if used:
        path = Path(cache_path) if cache_path is not None else default_cache_path()
        try:
            cached = _read_cache(path)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError, UnicodeError) as exc:
            if cache_path is not None:
                raise ValueError("无法读取指定的原生表情缓存") from exc
            if _CACHE_WARNING not in conversation.warnings:
                conversation.warnings.append(_CACHE_WARNING)
    selected = {token: embedded.get(token, cached.get(token)) for token in used if token in embedded or token in cached}
    if selected:
        # A combined cache/metadata selection must obey the same global limits.
        conversation.metadata["native_emoji_assets"] = _validate_assets(selected)
    else:
        conversation.metadata.pop("native_emoji_assets", None)


def get_render_assets(conversation: Conversation) -> dict[str, str]:
    """Return validated inline PNG sources; never read files or remote URLs."""
    assets = _validate_assets(conversation.metadata.get("native_emoji_assets", {}))
    return {token: "data:image/png;base64," + payload["data_base64"] for token, payload in assets.items()}


def render_native_text(text: str, assets: dict[str, str]) -> str:
    """Escape all text, replacing only exact locally available token labels."""
    pieces = []
    position = 0
    for match in _TOKEN.finditer(text):
        token = match.group()
        source = assets.get(token)
        if not isinstance(source, str) or _DATA_URI.fullmatch(source) is None:
            continue
        pieces.append(html.escape(text[position:match.start()], quote=True))
        label = html.escape(token, quote=True)
        pieces.append(
            f'<span class="native-emoji" role="img" aria-label="{label}" title="{label}">'
            f'<img class="native-emoji-image" src="{html.escape(source, quote=True)}" alt="" aria-hidden="true" decoding="async">'
            f'<span class="native-emoji-label">{label}</span></span>'
        )
        position = match.end()
    pieces.append(html.escape(text[position:], quote=True))
    return ''.join(pieces)
