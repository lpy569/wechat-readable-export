"""Resolve only the selected conversation's local raster media.

Resource-hash and attachment-layout references: wechat-chat-export,
revision 2c49469a91e2f3b0c73a581e15a8ad2db25b7923, Apache-2.0.
No WeChat UI actions, downloads, or process access happen implicitly here.
The optional sticker download mode contacts an allowlisted WeChat CDN only.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import os
from pathlib import Path
import re
from typing import Callable
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import xml.etree.ElementTree as ET

from .image_codec import decode_image
from .media_payload import make_media
from .models import CHINA_TIME, Conversation, MediaAttachment, Message
from .parsing import _decode, _integer, _message_id, _timestamp


MAX_FILE_BYTES = 32 * 1024 * 1024 + 4096
HASH = re.compile(r"[0-9a-fA-F]{32}\Z")
RESOURCE_HASH = re.compile(rb"\x12\x22\x0a\x20([0-9a-fA-F]{32})")
IMAGE_SUFFIXES = ("_h.dat", ".dat", "_t.dat", ".png", ".jpg", ".jpeg", ".gif", ".webp")


def extract_resource_hashes(value) -> list[str]:
    """Accept the known resource protobuf field, never an arbitrary filename."""
    if isinstance(value, str):
        value = value.encode("utf-8")
    if not isinstance(value, (bytes, bytearray, memoryview)):
        return []
    return list(dict.fromkeys(match.decode("ascii").lower()
                             for match in RESOURCE_HASH.findall(bytes(value))))


def _xml_nodes(row: dict, tag: str) -> list[ET.Element]:
    result = []
    for field in ("message_content", "compress_content", "source"):
        text = _decode(row.get(field), set(), field)
        # Group messages can carry the resolved sender prefix before their XML.
        start = text.find("<")
        if start < 0:
            continue
        text = text[start:]
        if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
            continue
        try:
            root = ET.fromstring(text.strip())
        except (ET.ParseError, ValueError):
            continue
        result.extend(node for node in root.iter() if node.tag == tag)
    return result


def _message_hashes(row: dict, sticker: bool = False) -> list[str]:
    result = []
    for node in _xml_nodes(row, "emoji" if sticker else "img"):
        value = node.get("md5", "")
        if HASH.fullmatch(value):
            result.append(value.lower())
    if not sticker:
        result = extract_resource_hashes(row.get("packed_info_data")) + result
    return list(dict.fromkeys(result))


def _safe_cdn_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 for c in value):
        raise ValueError("表情 CDN 地址无效")
    parts = urlsplit(value)
    host = (parts.hostname or "").lower()
    allowed = (host == "qpic.cn" or host.endswith(".qpic.cn")
               or host == "qlogo.cn" or host.endswith(".qlogo.cn")
               or host == "wxapp.tc.qq.com")
    if (not allowed or parts.scheme not in {"http", "https"}
            or parts.username is not None or parts.password is not None
            or parts.port not in {None, 80, 443}):
        raise ValueError("表情 CDN 地址不受支持")
    # Older XML often stores HTTP. Never send a signed URL over cleartext.
    return urlunsplit(("https", host, parts.path, parts.query, ""))


class _CdnRedirects(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, _safe_cdn_url(newurl))


def download_sticker(url: str) -> bytes:
    """Explicit opt-in only; validate redirects as well as the starting host."""
    request = Request(_safe_cdn_url(url), headers={"User-Agent": "wechat-readable-export"})
    with build_opener(_CdnRedirects()).open(request, timeout=15) as response:
        data = response.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError("表情文件超过 32 MiB 限制")
    return data


def _read_file(account: Path, path: Path) -> bytes:
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(account) or not resolved.is_file():
        raise ValueError("媒体路径超出所选账号目录")
    with resolved.open("rb") as handle:
        before = os.fstat(handle.fileno())
        if not 0 < before.st_size <= MAX_FILE_BYTES:
            raise ValueError("媒体文件为空或超过 32 MiB 限制")
        data = handle.read(MAX_FILE_BYTES + 1)
        after = os.fstat(handle.fileno())
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns) or len(data) != before.st_size:
        raise ValueError("媒体文件正在变化，请稍后重试")
    return data


def _index_files(account: Path, roots: list[Path]) -> dict[str, list[Path]]:
    result: dict[str, list[Path]] = defaultdict(list)
    for root in roots:
        if not root.is_dir() or not root.resolve().is_relative_to(account):
            continue
        for current, directories, names in os.walk(root, followlinks=False):
            folder = Path(current)
            directories[:] = sorted(name for name in directories
                                    if not (folder / name).is_symlink()
                                    and (folder / name).resolve().is_relative_to(account))
            for name in sorted(names):
                if re.fullmatch(r"[0-9a-fA-F]{32}(?:_[ht])?(?:\.(?:dat|png|jpe?g|gif|webp))?", name, re.I):
                    path = folder / name
                    if path.resolve().is_relative_to(account):
                        result[name.lower()].append(path)
    return dict(result)


def _row_key(row: dict):
    return (str(row.get("_db_rel") or row.get("source_db") or ""),
            _integer(row.get("local_id")), str(_message_id(row.get("server_id"))),
            _timestamp(row.get("create_time"), "媒体消息"), _integer(row.get("sort_seq")) or 0)


def _message_key(message: Message):
    return (message.source_db, message.local_id, str(message.server_id), message.timestamp, message.sort_seq)


class LocalMediaResolver:
    def __init__(self, account: Path, contact_id: str, *,
                 resource_hashes: dict[int, list[str]] | None = None,
                 key_provider: Callable | None = None, download_stickers: bool = False):
        self.account = account.resolve()
        self.chat_hash = hashlib.md5(contact_id.encode("utf-8")).hexdigest()
        self.resource_hashes = resource_hashes or {}
        self.key_provider = key_provider
        self.allow_downloads = download_stickers
        self.image_index = None
        self.sticker_index = None
        self.aes_key = None
        self.xor_key = None
        self.keys_attempted = False
        self.cache: dict[tuple, MediaAttachment] = {}
        self.selected_image_hashes: list[str] = []

    def close(self):
        self.aes_key = None
        self.xor_key = None
        self.cache.clear()
        self.selected_image_hashes.clear()

    def _samples(self, first: bytes) -> list[bytes]:
        """A damaged/HEVC original must not consume the only key lookup."""
        samples = [first]
        total = len(first)
        seen = {hashlib.sha256(first).digest()}
        # Small preview files more often contain a browser-readable raster than
        # originals, which can be wxgf/HEVC. Only selected message hashes qualify.
        for suffix in ("_t.dat", ".dat", "_h.dat"):
            for digest in self.selected_image_hashes:
                for path in (self.image_index or {}).get(digest + suffix, []):
                    if len(samples) >= 8:
                        return samples
                    try:
                        candidate = _read_file(self.account, path)
                    except (OSError, ValueError):
                        continue
                    checksum = hashlib.sha256(candidate).digest()
                    if (candidate.startswith(b"\x07\x08\x56\x32\x08\x07") and len(candidate) >= 31
                            and checksum not in seen and total + len(candidate) <= 64 * 1024 * 1024):
                        samples.append(candidate)
                        total += len(candidate)
                        seen.add(checksum)
        return samples

    def _file(self, path: Path, kind: str, *, thumbnail: bool = False) -> MediaAttachment:
        cache_key = (str(path), kind)
        if cache_key in self.cache:
            return self.cache[cache_key]
        data = _read_file(self.account, path)
        try:
            plain = decode_image(data, self.aes_key, self.xor_key)
        except ValueError:
            if data.startswith(b"\x07\x08\x56\x32\x08\x07") and self.key_provider and not self.keys_attempted:
                self.keys_attempted = True
                self.aes_key, self.xor_key = self.key_provider(self._samples(data))
                plain = decode_image(data, self.aes_key, self.xor_key)
            else:
                raise
        result = make_media(plain, kind=kind, reason="本地缩略图，非原图" if thumbnail else None)
        self.cache[cache_key] = result
        return result

    def _image(self, message: Message, row: dict, unique_local_id: bool,
               unique_thumbnail: bool = True) -> MediaAttachment:
        if self.image_index is None:
            self.image_index = _index_files(self.account, [self.account / "msg" / "attach" / self.chat_hash])
        hashes = []
        if unique_local_id:
            resource = self.resource_hashes.get(message.local_id, [])
            if len(resource) == 1:
                hashes.extend(resource)
        hashes.extend(_message_hashes(row))
        reasons = []
        month = datetime.fromtimestamp(message.timestamp, CHINA_TIME).strftime("%Y-%m")
        for digest in dict.fromkeys(hashes):
            for suffix in IMAGE_SUFFIXES:
                paths = sorted(self.image_index.get(digest + suffix, []),
                               key=lambda path: (month not in path.parts, str(path)))
                for path in paths:
                    try:
                        return self._file(path, "image", thumbnail=suffix == "_t.dat")
                    except (OSError, ValueError) as exc:
                        reasons.append(str(exc) if isinstance(exc, ValueError) else "本地图片无法读取")
        # Match both ID and exact timestamp. IDs alone can repeat across shards.
        if message.local_id is not None and unique_thumbnail:
            thumbnail = (self.account / "cache" / month / "Message" / self.chat_hash / "Thumb"
                         / f"{message.local_id}_{int(message.timestamp)}_thumb.jpg")
            if thumbnail.is_file():
                try:
                    return self._file(thumbnail, "image", thumbnail=True)
                except (OSError, ValueError):
                    reasons.append("本地缩略图无法读取或解码")
        reason = reasons[-1] if reasons else "本机未找到与此消息对应的图片文件，请先在微信中打开图片后重新导出"
        return MediaAttachment("image", "unsupported" if reasons else "missing", reason=reason)

    def _sticker(self, row: dict) -> MediaAttachment:
        hashes = _message_hashes(row, sticker=True)
        if self.sticker_index is None:
            self.sticker_index = _index_files(self.account, [
                self.account / "msg" / "emoji", self.account / "cache" / "emoji",
                self.account / "emoticon", self.account / "custom_emotion",
                self.account / "FileStorage" / "CustomEmotion",
            ])
        reasons = []
        for digest in hashes:
            for suffix in ("", ".gif", ".webp", ".png", ".jpg", ".jpeg", ".dat"):
                for path in self.sticker_index.get(digest + suffix, []):
                    try:
                        return self._file(path, "sticker")
                    except (OSError, ValueError):
                        reasons.append("本地表情文件无法读取或解码")
        if self.allow_downloads:
            urls = [(node.get(attribute, ""), attribute in {"thumburl", "thumb_url"})
                    for node in _xml_nodes(row, "emoji")
                    for attribute in ("cdnurl", "cdn_url", "thumburl", "thumb_url")
                    if node.get(attribute)]
            for url, thumbnail in dict.fromkeys(urls):
                try:
                    cache_key = (url, "sticker", tuple(sorted(hashes)), thumbnail)
                    if cache_key not in self.cache:
                        data = download_sticker(url)
                        if hashes and not thumbnail and hashlib.md5(data).hexdigest() not in hashes:
                            raise ValueError("表情文件 MD5 校验失败")
                        self.cache[cache_key] = make_media(data, kind="sticker",
                                                          reason="表情缩略图" if thumbnail else None)
                    return self.cache[cache_key]
                except (OSError, ValueError):
                    reasons.append("无法从微信 CDN 获取或校验表情，地址可能已失效")
        reason = (reasons[-1] if reasons else "本机未找到此表情；可用 --download-stickers 尝试从微信 CDN 获取")
        return MediaAttachment("sticker", "unsupported" if reasons else "missing", reason=reason)

    def attach(self, conversation: Conversation, rows: list[dict]) -> None:
        raw: dict[tuple, list[dict]] = defaultdict(list)
        for row in rows:
            raw[_row_key(row)].append(row)
        occurrences: Counter[tuple] = Counter()
        message_rows = []
        for message in conversation.messages:
            key = _message_key(message)
            index = occurrences[key]
            message_rows.append(raw[key][index] if index < len(raw[key]) else None)
            occurrences[key] += 1
        image_ids = Counter(_integer(row.get("local_id")) for row in rows
                            if (_integer(row.get("local_type")) or 0) & 0xFFFFFFFF == 3)
        thumbnail_ids = Counter((_integer(row.get("local_id")), int(_timestamp(row.get("create_time"), "媒体消息")))
                                for row in rows if (_integer(row.get("local_type")) or 0) & 0xFFFFFFFF == 3)
        self.selected_image_hashes = []
        for message, row in zip(conversation.messages, message_rows):
            if message.type_code & 0xFFFFFFFF != 3:
                continue
            if image_ids[message.local_id] == 1:
                hashes = self.resource_hashes.get(message.local_id, [])
                if len(hashes) == 1:
                    self.selected_image_hashes.extend(hashes)
            self.selected_image_hashes.extend(_message_hashes(row or {}))
        self.selected_image_hashes = list(dict.fromkeys(self.selected_image_hashes))
        counts: Counter[str] = Counter()
        issues: Counter[str] = Counter()
        for message, row in zip(conversation.messages, message_rows):
            kind = message.type_code & 0xFFFFFFFF
            if kind not in {3, 47}:
                continue
            if row is None:
                media = MediaAttachment("image" if kind == 3 else "sticker", reason="未找到对应的媒体消息元数据")
            else:
                try:
                    media = (self._image(message, row, image_ids[message.local_id] == 1,
                                         thumbnail_ids[message.local_id, int(message.timestamp)] == 1)
                             if kind == 3 else self._sticker(row))
                except OSError:
                    media = MediaAttachment("image" if kind == 3 else "sticker", reason="无法读取本地媒体目录")
            message.media = media
            counts[f"{media.kind}_{media.status}"] += 1
            if media.status != "available":
                issues[media.reason or "媒体不可用"] += 1
        conversation.metadata["media_statistics"] = {
            **dict(counts), "sticker_downloads_enabled": self.allow_downloads,
            "storage": "embedded raster bytes; no remote URLs in reading files",
        }
        conversation.warnings.extend(f"{reason}：{count} 条。" for reason, count in sorted(issues.items()))
