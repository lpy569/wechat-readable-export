"""Decode a bounded local wxgf HEVC still image without resizing it.

The wxgf Annex-B extraction convention is also used by the Apache-2.0
wechatauto-replica media module pinned in image_codec.py. This adapter runs
only a local FFmpeg binary, validates a single PNG, and discards temporary files.
"""

from __future__ import annotations

import io
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import time

from PIL import Image

from .media_payload import MAX_IMAGE_PIXELS, MAX_MEDIA_BYTES, _validate_image

MAX_WXGF_BYTES = 64 * 1024 * 1024
_MAX_HEADER_BYTES = 1024 * 1024
_DECODE_SECONDS = 30
_POLL_SECONDS = 0.1
_NAL_START = re.compile(rb"\x00\x00\x00?\x01")


def _ffmpeg_executable() -> str:
    # Prefer the installed wheel's known local binary over PATH. Neither path
    # downloads anything; imageio_ffmpeg ships FFmpeg inside its platform wheel.
    try:
        import imageio_ffmpeg
        executable = imageio_ffmpeg.get_ffmpeg_exe()
        if executable and Path(executable).is_file():
            return executable
    except (ImportError, OSError, RuntimeError):
        pass
    executable = shutil.which("ffmpeg")
    if executable:
        return executable
    raise ValueError("解码微信高清图片需要本地 FFmpeg；请安装 imageio-ffmpeg 后重试。")


def _hevc_stream(data: bytes) -> bytes:
    if not isinstance(data, bytes) or not data.startswith(b"wxgf") or len(data) > MAX_WXGF_BYTES:
        raise ValueError("微信 wxgf 图片为空、格式无效或超过 64 MiB 限制。")
    # Look only inside a bounded container header. A parameter-set NAL (VPS,
    # SPS, PPS) or leading AUD/SEI has a nonzero temporal_id_plus1 and no
    # forbidden bit. Rejecting arbitrary start-code-like header integers also
    # avoids passing a guessed encrypted prefix as an image to the decoder.
    limit = min(len(data), _MAX_HEADER_BYTES)
    for marker in _NAL_START.finditer(data, 4, limit):
        if marker.end() + 2 <= len(data):
            first, second = data[marker.end():marker.end() + 2]
            nal_type = (first >> 1) & 0x3F
            if not first & 0x80 and second & 7 and nal_type in (32, 33, 34, 35, 39, 40):
                return data[marker.start():]
    raise ValueError("微信 wxgf 图片缺少有效 HEVC 数据或本地缓存不完整。")


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def _smaller_lossless(png: bytes) -> bytes:
    """Reduce portable export size without changing any decoded RGB pixels."""
    try:
        with Image.open(io.BytesIO(png)) as image, io.BytesIO() as buffer:
            image.save(buffer, format="WEBP", lossless=True, exact=True, method=3)
            if buffer.tell() >= len(png):
                return png
            result = buffer.getvalue()
        _validate_image(result, "image/webp")
        return result
    except (KeyError, OSError, ValueError):
        # A Pillow build without lossless WebP support can retain the PNG.
        return png


def decode_wxgf(data: bytes) -> bytes:
    """Return a validated PNG/lossless WebP without changing pixel dimensions."""
    stream = _hevc_stream(data)
    executable = _ffmpeg_executable()
    try:
        with tempfile.TemporaryDirectory(prefix="wechat-image-") as directory:
            source = Path(directory) / "image.hevc"
            output = Path(directory) / "image.png"
            source.write_bytes(stream)
            command = [
                executable, "-nostdin", "-hide_banner", "-loglevel", "error", "-xerror",
                "-max_alloc", str(256 * 1024 * 1024),
                "-protocol_whitelist", "file", "-f", "hevc", "-c:v", "hevc",
                "-threads", "2", "-max_pixels", str(MAX_IMAGE_PIXELS), "-err_detect", "explode",
                "-i", str(source), "-map", "0:v:0", "-frames:v", "1",
                "-an", "-sn", "-dn", "-threads", "2", "-c:v", "png",
                "-pix_fmt", "rgb24", "-f", "image2", "-update", "1", str(output),
            ]
            with subprocess.Popen(
                command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, shell=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ) as process:
                deadline = time.monotonic() + _DECODE_SECONDS
                try:
                    while process.poll() is None:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise ValueError("微信高清图片解码超时，可回退本地缩略图。")
                        if output.exists() and output.stat().st_size > MAX_MEDIA_BYTES:
                            raise ValueError("微信高清图片解码结果超过 32 MiB 限制。")
                        try:
                            process.wait(timeout=min(_POLL_SECONDS, remaining))
                        except subprocess.TimeoutExpired:
                            continue
                    if process.returncode != 0 or not output.is_file():
                        raise ValueError("微信高清图片解码失败或本地缓存不完整。")
                finally:
                    _stop(process)
            size = output.stat().st_size
            if not 0 < size <= MAX_MEDIA_BYTES:
                raise ValueError("微信高清图片解码结果为空或超过 32 MiB 限制。")
            result = output.read_bytes()
            _validate_image(result, "image/png")
            return _smaller_lossless(result)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("无法运行本地微信高清图片解码器。") from exc
