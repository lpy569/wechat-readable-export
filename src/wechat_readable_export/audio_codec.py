"""Bounded, entirely local conversion of WeChat SILK voice into portable WAV.

The optional native SILK decoder runs in a short-lived isolated Python process.
Only validated PCM16 mono 24 kHz WAV is returned to the export renderer.
"""

from __future__ import annotations

import importlib.util
import io
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import time
import wave

MAX_VOICE_INPUT_BYTES = 8 * 1024 * 1024
MAX_VOICE_SECONDS = 120
VOICE_SAMPLE_RATE = 24_000
MAX_PCM_BYTES = MAX_VOICE_SECONDS * VOICE_SAMPLE_RATE * 2
MAX_WAV_BYTES = MAX_PCM_BYTES + 4096
_MAX_SILK_PACKETS = MAX_VOICE_SECONDS * 50
_MAX_SILK_PACKET_BYTES = 1250
_DECODE_SECONDS = 20
_POLL_SECONDS = 0.1
_SILK_HEADER = b"#!SILK_V3"

# A constant program rather than interpolated data keeps file names and audio
# bytes out of shell/code syntax. The writer bounds disk usage during decoding,
# and the parent independently enforces output size and elapsed time.
_SILK_WORKER = r'''
import struct
import sys
from pysilk.backends.cffi._silk import ffi, lib

# SILK's SDK returns one 20 ms frame per call. Draining the control flag is
# necessary for packets holding 40, 60, 80 or 100 ms; the binding's convenience
# decode() loop otherwise drops their later internal frames.
state_size = ffi.new("int32_t *")
if lib.SKP_Silk_SDK_Get_Decoder_Size(state_size) != 0 or not 0 < state_size[0] <= 16 * 1024 * 1024:
    raise ValueError("invalid decoder state size")
state = ffi.new("uint8_t[]", state_size[0])
if lib.SKP_Silk_SDK_InitDecoder(state) != 0:
    raise ValueError("decoder initialization failed")
control = ffi.new("SKP_SILK_SDK_DecControlStruct *")
control.API_sampleRate = 24000
samples = ffi.new("int16_t[480]")
limit, written = int(sys.argv[3]), 0

with open(sys.argv[1], "rb") as source, open(sys.argv[2], "wb") as target:
    header = source.read(9)
    if header == b"\x02#!SILK_V":
        if source.read(1) != b"3":
            raise ValueError("invalid SILK header")
    elif header != b"#!SILK_V3":
        raise ValueError("invalid SILK header")
    while True:
        length = source.read(2)
        if not length:
            break
        if len(length) != 2:
            raise ValueError("invalid packet length")
        length = struct.unpack("<h", length)[0]
        if not 0 < length <= 1250:
            raise ValueError("invalid packet size")
        packet = source.read(length)
        if len(packet) != length:
            raise ValueError("truncated packet")
        packet_buffer = ffi.from_buffer("uint8_t[]", packet)
        for frame in range(5):
            count = ffi.new("int16_t *", 480)
            code = lib.SKP_Silk_SDK_Decode(state, control, 0, packet_buffer, length, samples, count)
            if code != 0 or not 0 < count[0] <= 480:
                raise ValueError("invalid decoded samples")
            pcm = struct.pack("<%dh" % count[0], *samples[0:count[0]])
            if written + len(pcm) > limit:
                raise ValueError("decoded voice exceeds size limit")
            target.write(pcm)
            written += len(pcm)
            if not control.moreInternalDecoderFrames:
                break
        else:
            raise ValueError("too many internal frames")
'''


def validate_wav(data: bytes) -> float:
    """Validate bounded PCM16 mono 24 kHz RIFF/WAVE and return its duration.

    Chunk sizes, padding, RIFF length and the PCM byte rate must agree. This
    intentionally excludes compressed WAVE, playlists, RF64 and remote paths.
    """
    if not isinstance(data, bytes) or not 44 <= len(data) <= MAX_WAV_BYTES:
        raise ValueError("语音 WAV 为空或超过大小限制。")
    if data[:4] != b"RIFF" or data[8:12] != b"WAVE" or struct.unpack_from("<I", data, 4)[0] != len(data) - 8:
        raise ValueError("语音 WAV 文件头或长度无效。")
    offset, chunks, pcm_size = 12, 0, None
    fmt_seen = False
    while offset < len(data):
        chunks += 1
        if chunks > 32 or offset + 8 > len(data):
            raise ValueError("语音 WAV 块结构无效。")
        name = data[offset:offset + 4]
        size = struct.unpack_from("<I", data, offset + 4)[0]
        start, end = offset + 8, offset + 8 + size
        padded_end = end + (size & 1)
        if padded_end > len(data):
            raise ValueError("语音 WAV 数据不完整。")
        if name == b"fmt ":
            if fmt_seen or size != 16 or pcm_size is not None:
                raise ValueError("语音 WAV 格式块无效。")
            fmt_seen = True
            expected = (1, 1, VOICE_SAMPLE_RATE, VOICE_SAMPLE_RATE * 2, 2, 16)
            if struct.unpack_from("<HHIIHH", data, start) != expected:
                raise ValueError("语音仅支持 PCM16 单声道 24000 Hz WAV。")
        elif name == b"data":
            if not fmt_seen or pcm_size is not None or not 0 < size <= MAX_PCM_BYTES or size % 2:
                raise ValueError("语音 WAV 样本为空、过长或不完整。")
            pcm_size = size
        elif name not in (b"JUNK", b"LIST", b"fact"):
            raise ValueError("语音 WAV 含不支持的块。")
        offset = padded_end
    if not fmt_seen or pcm_size is None:
        raise ValueError("语音 WAV 缺少格式或样本。")
    return pcm_size / (VOICE_SAMPLE_RATE * 2)


def _silk_stream(data: bytes) -> bytes:
    """Validate packet framing before passing any bytes to native code."""
    if not isinstance(data, bytes) or not 0 < len(data) <= MAX_VOICE_INPUT_BYTES:
        raise ValueError("本地语音为空或超过 8 MiB 限制。")
    offset = 1 if data.startswith(b"\x02" + _SILK_HEADER) else 0
    if data[offset:offset + 9] != _SILK_HEADER:
        raise ValueError("本地语音格式不受支持，需要 SILK V3 或 PCM WAV。")
    offset += 9
    packets = 0
    while offset < len(data):
        if offset + 2 > len(data):
            raise ValueError("本地 SILK 语音包长度不完整。")
        size = struct.unpack_from("<h", data, offset)[0]
        offset += 2
        if size == -1 and offset == len(data):
            # The standard SILK terminator is not a packet. Strip it because
            # the binding's file-like loop expects EOF after the final packet.
            data = data[:-2]
            break
        if not 0 < size <= _MAX_SILK_PACKET_BYTES or offset + size > len(data):
            raise ValueError("本地 SILK 语音包损坏或不完整。")
        packets += 1
        if packets > _MAX_SILK_PACKETS:
            raise ValueError("本地语音超过 120 秒限制。")
        offset += size
    if not packets:
        raise ValueError("本地 SILK 语音没有样本。")
    return data


def _pcm_to_wav(pcm: bytes) -> bytes:
    if not pcm or len(pcm) > MAX_PCM_BYTES or len(pcm) % 2:
        raise ValueError("本地语音解码结果为空、过长或不完整。")
    with io.BytesIO() as buffer:
        with wave.open(buffer, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(VOICE_SAMPLE_RATE)
            output.writeframes(pcm)
        result = buffer.getvalue()
    validate_wav(result)
    return result


def _stop(process: subprocess.Popen) -> None:
    if process.poll() is None:
        process.kill()
    process.wait()


def decode_voice(data: bytes) -> bytes:
    """Return a validated portable WAV; never upload audio or invoke a shell."""
    if isinstance(data, bytes) and data.startswith(b"RIFF"):
        validate_wav(data)
        return data
    stream = _silk_stream(data)
    if importlib.util.find_spec("pysilk") is None:
        raise ValueError("解码本地微信语音需要 silk-python；请安装项目依赖后重试。")
    try:
        with tempfile.TemporaryDirectory(prefix="wechat-voice-") as directory:
            source = Path(directory) / "voice.silk"
            output = Path(directory) / "voice.pcm"
            source.write_bytes(stream)
            command = [sys.executable, "-I", "-B", "-c", _SILK_WORKER,
                       str(source), str(output), str(MAX_PCM_BYTES)]
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
                            raise ValueError("本地微信语音解码超时。")
                        if output.exists() and output.stat().st_size > MAX_PCM_BYTES:
                            raise ValueError("本地语音解码结果超过 120 秒限制。")
                        try:
                            process.wait(timeout=min(_POLL_SECONDS, remaining))
                        except subprocess.TimeoutExpired:
                            continue
                    if process.returncode != 0 or not output.is_file():
                        raise ValueError("本地微信语音解码失败或缓存不完整。")
                finally:
                    _stop(process)
            if not 0 < output.stat().st_size <= MAX_PCM_BYTES:
                raise ValueError("本地语音解码结果为空或超过 120 秒限制。")
            return _pcm_to_wav(output.read_bytes())
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("无法运行本地微信语音解码器。") from exc
