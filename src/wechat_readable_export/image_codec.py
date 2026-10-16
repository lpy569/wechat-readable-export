"""Bounded, read-only decoding of locally cached WeChat images.

Format and candidate scanning reference: fanyuantaier/wechatauto-replica,
Apache-2.0, tag v1.2.4.4, commit 91dc0e1a601013b759b261061d8f7642f736af90,
https://github.com/fanyuantaier/wechatauto-replica/blob/91dc0e1a601013b759b261061d8f7642f736af90/wechatauto/media.py
Adaptation: standalone bytes API, strict bounds/padding/raster validation,
single bounded process scan, no UI, downloading, logging, key caching or writes.
The Windows API ABI is reused from the reviewed read-only vendor module.
"""

from __future__ import annotations

from collections import Counter
import ctypes
import os
import re
import struct
import time
from typing import Callable

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .media_payload import _validate_image, sniff_mime

V1_MAGIC = b"\x07\x08\x05\x56\x02\x05"
V2_MAGIC = b"\x07\x08\x56\x32\x08\x07"
MAX_IMAGE_BYTES = 64 * 1024 * 1024
_PNG = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_ASCII_KEY = re.compile(rb"(?<![a-zA-Z0-9])([a-zA-Z0-9]{16}|[a-zA-Z0-9]{32})(?![a-zA-Z0-9])")
_UTF16_KEY = re.compile(rb"(?<![a-zA-Z0-9]\x00)((?:[a-zA-Z0-9]\x00){16}|(?:[a-zA-Z0-9]\x00){32})(?![a-zA-Z0-9]\x00)")
_READ_CHUNK = 1024 * 1024
_SCAN_BYTE_LIMIT = 1024 * 1024 * 1024
_SCAN_CANDIDATE_LIMIT = 200_000
_SCAN_SECONDS = 20


def _raster(data: bytes) -> bytes:
    """Trim small cache footers, then fully decode the selected raster."""
    if data.startswith(b"wxgf"):
        raise ValueError("微信 wxgf/HEVC 图片暂不支持直接展示，可回退本地缩略图。")
    end = -1
    if data.startswith(b"\xff\xd8\xff"):
        marker = data.rfind(b"\xff\xd9")
        end = marker + 2 if marker >= 3 else -1
    elif data.startswith(_PNG):
        marker = data.rfind(_PNG_END)
        end = marker + len(_PNG_END) if marker >= 8 else -1
    elif data.startswith((b"GIF87a", b"GIF89a")):
        marker = data.rfind(b";")
        end = marker + 1 if len(data) >= 14 and marker >= 13 else -1
    elif len(data) >= 20 and data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        end = struct.unpack_from("<I", data, 4)[0] + 8
        if data[12:16] not in (b"VP8 ", b"VP8L", b"VP8X"):
            end = -1
    if not 0 < end <= len(data) or len(data) - end > 32:
        raise ValueError("图片格式不支持、数据不完整或解密密钥不正确。")
    output = data[:end]
    mime_type = sniff_mime(output)
    if mime_type is None:
        raise ValueError("图片格式不受支持。")
    _validate_image(output, mime_type)
    return output


def _header(data: bytes) -> tuple[int, int, int]:
    if len(data) < 31:
        raise ValueError("V2 图片头或 AES 数据不完整。")
    aes_size, xor_size = struct.unpack_from("<II", data, 6)
    aes_block_size = (aes_size // 16 + 1) * 16
    if aes_size > MAX_IMAGE_BYTES or xor_size > MAX_IMAGE_BYTES or 15 + aes_block_size + xor_size > len(data):
        raise ValueError("V2 图片分段长度无效或缓存不完整。")
    return aes_size, aes_block_size, xor_size


def _tail_keys(data: bytes, xor_size: int) -> list[int]:
    """Known JPEG/PNG endings provide candidates, never an unvalidated key."""
    if xor_size <= 0:
        return []
    tail = data[-min(xor_size, 44):]
    keys: list[int] = []
    for marker in (_PNG_END, b"\xff\xd9"):
        for footer in range(min(32, len(tail) - len(marker)) + 1):
            at = len(tail) - len(marker) - footer
            candidate = tail[at] ^ marker[0]
            if all(tail[at + i] ^ candidate == byte for i, byte in enumerate(marker)):
                keys.append(candidate)
    return list(dict.fromkeys(keys))


def _v2_parts(data: bytes, aes_key: bytes | None) -> tuple[bytes, bytes, bytes]:
    aes_size, aes_block_size, xor_size = _header(data)
    if not isinstance(aes_key, bytes) or len(aes_key) != 16:
        raise ValueError("V2 图片缺少有效 AES 密钥；请在微信中查看一张大图后重试。")
    decryptor = Cipher(algorithms.AES(aes_key), modes.ECB()).decryptor()
    padded = decryptor.update(data[15:15 + aes_block_size]) + decryptor.finalize()
    pad = padded[-1]
    if not 1 <= pad <= 16 or padded[-pad:] != bytes([pad]) * pad or len(padded) - pad != aes_size:
        raise ValueError("图片 AES 密钥不正确或填充数据已损坏。")
    end = len(data) - xor_size
    return padded[:-pad], data[15 + aes_block_size:end], data[end:]


def decode_image(data: bytes, aes_key: bytes | None = None, xor_key: int | None = None) -> bytes:
    """Return a browser-readable raster or fail without returning partial data."""
    if not isinstance(data, bytes) or not data or len(data) > MAX_IMAGE_BYTES:
        raise ValueError("图片为空或超过 64 MiB 限制。")
    if xor_key is not None and (isinstance(xor_key, bool) or not isinstance(xor_key, int) or not 0 <= xor_key <= 255):
        raise ValueError("图片 XOR 密钥必须是 0 至 255 的整数。")
    if data.startswith((b"\xff\xd8\xff", _PNG, b"GIF87a", b"GIF89a", b"RIFF", b"wxgf")):
        return _raster(data)
    if data.startswith(V2_MAGIC):
        plain, raw, xor_data = _v2_parts(data, aes_key)
        xor_size = len(xor_data)
        if xor_size == 0:
            return _raster(plain + raw)
        keys = [xor_key] if xor_key is not None else _tail_keys(data, xor_size)
        # A GIF trailer has one byte; use it only after validating the decrypted header.
        if xor_key is None and plain.startswith((b"GIF87a", b"GIF89a")):
            keys.append(data[-1] ^ 0x3B)
        for key in dict.fromkeys(keys):
            try:
                return _raster(plain + raw + bytes(byte ^ key for byte in xor_data))
            except ValueError:
                continue
        if plain.startswith(b"wxgf"):
            return _raster(plain)
        raise ValueError("无法验证图片 XOR 密钥或图片尾部不完整。")
    body = data[22:] if data.startswith(V1_MAGIC) else data
    if not body:
        raise ValueError("图片缓存不完整。")
    keys = [xor_key] if xor_key is not None else []
    if xor_key is None:
        for signature in (b"\xff\xd8\xff", _PNG, b"GIF87a", b"GIF89a", b"RIFF"):
            if len(body) >= len(signature):
                key = body[0] ^ signature[0]
                if bytes(byte ^ key for byte in body[:len(signature)]) == signature:
                    keys.append(key)
    for key in dict.fromkeys(keys):
        try:
            return _raster(bytes(byte ^ key for byte in body))
        except ValueError:
            continue
    raise ValueError("无法识别图片缓存格式或验证解密密钥。")


def _key_candidates(data: bytes):
    for match in _ASCII_KEY.finditer(data):
        yield match.group(1)[:16]
    for match in _UTF16_KEY.finditer(data):
        yield match.group(1)[::2][:16]


def _probe_matches(data: bytes, key: bytes) -> bool:
    """Reject candidates using one AES block before decoding a whole image."""
    aes_size, _, _ = _header(data)
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    first = decryptor.update(data[15:31]) + decryptor.finalize()
    if aes_size == 0:
        return first == bytes([16]) * 16
    return first.startswith((b"\xff\xd8\xff", _PNG, b"GIF87a", b"GIF89a", b"RIFF", b"wxgf"))


def _readable_regions(api, handle, windows, deadline: float) -> list[tuple[int, int]]:
    """Prioritize private heap pages before large mapped/DLL image regions.

    This ordering is our optimization for a bounded scan. The upstream scanner
    visits readable committed regions in virtual-address order.
    """
    regions = []
    address = 0
    while address < 0x800000000000 and len(regions) < 100_000 and time.monotonic() < deadline:
        info = windows._MBI()
        if not api.VirtualQueryEx(handle, ctypes.c_void_p(address), ctypes.byref(info), ctypes.sizeof(info)):
            break
        base = info.BaseAddress or 0
        size = info.RegionSize
        next_address = base + size
        if next_address <= address:
            break
        protection = info.Protect & 0xFF
        if info.State == 0x1000 and protection in (0x02, 0x04, 0x08, 0x20, 0x40, 0x80) and not (info.Protect & 0x100):
            private = info.Type == 0x20000  # MEM_PRIVATE, typical heap allocations
            priority = (0 if private and protection == 0x04 else 1 if private else 3 if info.Type == 0x1000000 else 2)
            regions.append((priority, base, size))
        address = next_address
    regions.sort()
    return [(base, size) for _, base, size in regions]


def find_image_key(pids: list[int], samples: list[bytes], progress: Callable[[str], None] | None = None) -> tuple[bytes | None, int | None]:
    """Scan supplied Windows PIDs once, keeping validated keys in memory only.

    Supplied samples must be complete V2 cache contents from the selected chat.
    This function never enumerates processes or reads account files itself.
    """
    probes: list[bytes] = []
    votes: Counter[int] = Counter()
    for sample in samples[:16]:
        if not isinstance(sample, bytes) or not sample.startswith(V2_MAGIC) or len(sample) > MAX_IMAGE_BYTES:
            continue
        try:
            _, _, xor_size = _header(sample)
        except ValueError:
            continue
        probes.append(sample)
        votes.update(_tail_keys(sample, xor_size))
    xor_candidate = votes.most_common(1)[0][0] if votes else None
    if not probes or os.name != "nt" or not pids:
        return None, None
    from ._vendor import db_readonly as windows
    windows.initialize_windows_api()
    api = windows._k32
    if progress:
        progress("正在只读查找图片解密材料；只扫描一次，密钥不会写入文件。")
    tested: set[bytes] = set()
    bytes_read = 0
    deadline = time.monotonic() + _SCAN_SECONDS
    for pid in dict.fromkeys(pids):
        if time.monotonic() >= deadline:
            break
        handle = api.OpenProcess(0x0010 | 0x0400, False, pid)
        if not handle:
            continue
        try:
            for base, size in _readable_regions(api, handle, windows, deadline):
                if bytes_read >= _SCAN_BYTE_LIMIT or len(tested) >= _SCAN_CANDIDATE_LIMIT or time.monotonic() >= deadline:
                    break
                offset = 0
                carry = b""
                while offset < size and bytes_read < _SCAN_BYTE_LIMIT and len(tested) < _SCAN_CANDIDATE_LIMIT and time.monotonic() < deadline:
                    count = min(_READ_CHUNK, size - offset, _SCAN_BYTE_LIMIT - bytes_read)
                    buffer = ctypes.create_string_buffer(count)
                    actual = ctypes.c_size_t()
                    ok = api.ReadProcessMemory(handle, ctypes.c_void_p(base + offset), buffer, count, ctypes.byref(actual))
                    bytes_read += count
                    offset += count
                    chunk = buffer.raw[:actual.value] if ok and actual.value else b""
                    combined = carry + chunk
                    for candidate in _key_candidates(combined):
                        if time.monotonic() >= deadline:
                            break
                        if candidate in tested:
                            continue
                        tested.add(candidate)
                        for probe in probes:
                            if not _probe_matches(probe, candidate):
                                continue
                            # wxgf is not browser-readable, but its known header
                            # plus strict AES padding can still validate AES.
                            try:
                                decrypted, _, _ = _v2_parts(probe, candidate)
                            except ValueError:
                                continue
                            if decrypted.startswith(b"wxgf"):
                                return candidate, None
                            try:
                                decode_image(probe, candidate, xor_candidate)
                            except ValueError:
                                # A different image may supply a stronger tail-derived XOR key.
                                try:
                                    decode_image(probe, candidate)
                                except ValueError:
                                    continue
                            # Return only after all raster frames decode.
                            _, _, tail_size = _header(probe)
                            for key in _tail_keys(probe, tail_size):
                                try:
                                    decode_image(probe, candidate, key)
                                except ValueError:
                                    continue
                                return candidate, key
                            return candidate, xor_candidate if tail_size else None
                        if len(tested) >= _SCAN_CANDIDATE_LIMIT:
                            break
                    carry = combined[-66:] if chunk else b""
        finally:
            api.CloseHandle(handle)
    return None, None
