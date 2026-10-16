"""Synthetic voice only: no real account audio or database files."""

from __future__ import annotations

import importlib.util
import io
import math
from pathlib import Path
import struct
import subprocess
import unittest
from unittest.mock import patch
import wave

from wechat_readable_export import audio_codec as codec


SILK = b"\x02#!SILK_V3" + struct.pack("<h", 4) + b"test"


def wav(pcm=b"\0\0" * 480):
    with io.BytesIO() as output:
        with wave.open(output, "wb") as target:
            target.setnchannels(1)
            target.setsampwidth(2)
            target.setframerate(24000)
            target.writeframes(pcm)
        return output.getvalue()


class _Process:
    def __init__(self, command, *, output=None, status=0, running=False, **options):
        self.command, self.options = command, options
        self.returncode = None if running else status
        self.killed = self.waited = False
        self.output = Path(command[-2])
        self.directory = self.output.parent
        self.source = Path(command[-3]).read_bytes()
        if output is not None:
            self.output.write_bytes(output)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def poll(self):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.waited = True
        if self.returncode is None:
            raise subprocess.TimeoutExpired(self.command, timeout)
        return self.returncode


class AudioCodecTests(unittest.TestCase):
    def _run(self, *, output=None, status=0, running=False):
        processes = []

        def launch(command, **kwargs):
            process = _Process(command, output=output, status=status, running=running, **kwargs)
            processes.append(process)
            return process

        return processes, patch.object(codec.subprocess, "Popen", side_effect=launch)

    def test_portable_wav_has_exact_sample_rate_channels_duration(self):
        data = wav()
        self.assertEqual(codec.decode_voice(data), data)
        self.assertEqual(codec.validate_wav(data), 0.02)
        with wave.open(io.BytesIO(data)) as voice:
            self.assertEqual((voice.getframerate(), voice.getnchannels(), voice.getsampwidth()), (24000, 1, 2))

    def test_wav_chunk_lengths_format_duration_and_payload_are_strict(self):
        valid = wav()
        invalid = [b"", valid[:-1], valid + b"trailing", valid[:12],
                   valid[:12] + b"data" + valid[16:],
                   valid[:20] + struct.pack("<H", 3) + valid[22:],
                   valid[:22] + struct.pack("<H", 2) + valid[24:],
                   valid[:24] + struct.pack("<I", 48000) + valid[28:],
                   valid[:28] + struct.pack("<I", 24000) + valid[32:],
                   valid[:32] + struct.pack("<H", 4) + valid[34:],
                   valid[:34] + struct.pack("<H", 8) + valid[36:]]
        for data in invalid:
            with self.subTest(data=data[:36]), self.assertRaises(ValueError):
                codec.validate_wav(data)
        with patch.object(codec, "MAX_PCM_BYTES", 4), self.assertRaises(ValueError):
            codec.validate_wav(valid)
        with patch.object(codec, "MAX_WAV_BYTES", 44), self.assertRaises(ValueError):
            codec.validate_wav(valid)
        for pcm in (b"", b"\0", b"\0" * 10):
            with patch.object(codec, "MAX_PCM_BYTES", 4), self.assertRaises(ValueError):
                codec._pcm_to_wav(pcm)

    def test_wav_auxiliary_chunks_are_bounded_and_duplicate_data_rejected(self):
        valid = wav()
        for name, payload in [(b"JUNK", b"x"), (b"LIST", b"metadata"), (b"fact", b"\0" * 4)]:
            chunk = name + struct.pack("<I", len(payload)) + payload + (b"\0" if len(payload) % 2 else b"")
            data = valid[:12] + chunk + valid[12:]
            data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]
            self.assertEqual(codec.validate_wav(data), 0.02)
        for extra in (valid[12:36], valid[36:], b"evil\0\0\0\0", b"data\xff\xff\xff\xff"):
            data = valid + extra
            data = data[:4] + struct.pack("<I", len(data) - 8) + data[8:]
            with self.assertRaises(ValueError):
                codec.validate_wav(data)

    def test_silk_standard_tencent_and_terminator_framing(self):
        self.assertEqual(codec._silk_stream(SILK), SILK)
        self.assertEqual(codec._silk_stream(SILK[1:]), SILK[1:])
        self.assertEqual(codec._silk_stream(SILK + b"\xff\xff"), SILK)
        # A length above 255 exercises the complete little-endian 16-bit field.
        stream = SILK[:10] + struct.pack("<h", 300) + b"x" * 300
        self.assertEqual(codec._silk_stream(stream), stream)

    def test_invalid_or_oversized_silk_never_runs_native_decoder(self):
        invalid = [b"", b"#!AMR\n", b"https://invalid.test/audio", b"#!SILK_V3", SILK[:11],
                   SILK[:-1], SILK + b"\0", SILK[:10] + b"\0\0", SILK + b"\xff\xffx",
                   SILK[:10] + b"\xfe\xff", SILK[:10] + struct.pack("<h", 1251) + b"x" * 1251]
        with patch.object(codec.subprocess, "Popen") as launch:
            for data in invalid:
                with self.subTest(data=data[:12]), self.assertRaises(ValueError):
                    codec.decode_voice(data)
            with patch.object(codec, "MAX_VOICE_INPUT_BYTES", 4), self.assertRaises(ValueError):
                codec.decode_voice(SILK)
            with patch.object(codec, "_MAX_SILK_PACKETS", 1), self.assertRaises(ValueError):
                codec.decode_voice(SILK + SILK[10:])
        launch.assert_not_called()

    def test_missing_codec_has_actionable_failure_without_temp_files(self):
        with patch.object(codec.importlib.util, "find_spec", return_value=None), \
             patch.object(codec.subprocess, "Popen") as launch, \
             self.assertRaisesRegex(ValueError, "silk-python"):
            codec.decode_voice(SILK)
        launch.assert_not_called()

    def test_native_worker_is_isolated_and_cleans_up_decrypted_files(self):
        pcm = b"\0\0" * 480
        processes, launch = self._run(output=pcm)
        with patch.object(codec.importlib.util, "find_spec", return_value=object()), launch:
            output = codec.decode_voice(SILK + b"\xff\xff")
        self.assertEqual(output, wav(pcm))
        process = processes[0]
        self.assertEqual(process.source, SILK)
        self.assertFalse(process.directory.exists())
        self.assertIn("-I", process.command)
        self.assertEqual(process.command[-1], str(codec.MAX_PCM_BYTES))
        self.assertTrue(process.waited)
        self.assertFalse(process.options["shell"])
        for name in ("stdin", "stdout", "stderr"):
            self.assertEqual(process.options[name], subprocess.DEVNULL)

    def test_failed_odd_empty_or_oversized_output_rejected_and_removed(self):
        for pcm, status, limit in [(None, 0, codec.MAX_PCM_BYTES), (b"", 0, codec.MAX_PCM_BYTES),
                                   (b"x", 0, codec.MAX_PCM_BYTES), (b"\0\0", 2, codec.MAX_PCM_BYTES),
                                   (b"\0" * 10, 0, 4)]:
            processes, launch = self._run(output=pcm, status=status)
            with self.subTest(status=status, size=len(pcm) if pcm else 0), \
                 patch.object(codec.importlib.util, "find_spec", return_value=object()), \
                 patch.object(codec, "MAX_PCM_BYTES", limit), launch, self.assertRaises(ValueError):
                codec.decode_voice(SILK)
            self.assertFalse(processes[0].directory.exists())

    def test_timeout_or_growing_output_kills_worker_and_removes_files(self):
        for pcm, seconds, limit in [(None, 0, codec.MAX_PCM_BYTES), (b"\0" * 10, 20, 4)]:
            processes, launch = self._run(output=pcm, running=True)
            with patch.object(codec.importlib.util, "find_spec", return_value=object()), \
                 patch.object(codec, "_DECODE_SECONDS", seconds), \
                 patch.object(codec, "MAX_PCM_BYTES", limit), launch, self.assertRaises(ValueError):
                codec.decode_voice(SILK)
            self.assertTrue(processes[0].killed)
            self.assertTrue(processes[0].waited)
            self.assertFalse(processes[0].directory.exists())

    @unittest.skipUnless(importlib.util.find_spec("pysilk"), "local SILK codec not installed")
    def test_real_synthetic_sine_roundtrip_keeps_duration_and_nonzero_samples(self):
        import pysilk
        pcm = b"".join(struct.pack("<h", int(math.sin(i * 2 * math.pi * 440 / 24000) * 5000))
                       for i in range(24000))
        for tencent in (False, True):
            encoded = io.BytesIO()
            pysilk.encode(io.BytesIO(pcm), encoded, 24000, 24000, tencent=tencent)
            data = encoded.getvalue()
            with self.subTest(tencent=tencent):
                self.assertTrue(data.startswith(b"\x02#!SILK_V3" if tencent else b"#!SILK_V3"))
                result = codec.decode_voice(data)
                self.assertEqual(codec.validate_wav(result), 1.0)
                with wave.open(io.BytesIO(result)) as voice:
                    decoded = voice.readframes(voice.getnframes())
                self.assertTrue(any(decoded))
                self.assertEqual(len(decoded), len(pcm))
                # Lossy SILK preserves the generated tone's amplitude without
                # claiming byte equality with its original PCM source.
                samples = struct.unpack("<" + "h" * (len(decoded) // 2), decoded)
                self.assertGreater(max(samples), 1000)

    @unittest.skipUnless(importlib.util.find_spec("pysilk"), "local SILK codec not installed")
    def test_real_silk_20_to_100_ms_packets_drain_every_internal_frame(self):
        from pysilk.backends.cffi._silk import ffi, lib
        for packet_frames in range(1, 6):
            size = ffi.new("int32_t *")
            self.assertEqual(lib.SKP_Silk_SDK_Get_Encoder_Size(size), 0)
            state = ffi.new("uint8_t[]", size[0])
            status = ffi.new("SKP_SILK_SDK_EncControlStruct *")
            self.assertEqual(lib.SKP_Silk_SDK_InitEncoder(state, status), 0)
            control = ffi.new("SKP_SILK_SDK_EncControlStruct *")
            control.API_sampleRate = control.maxInternalSampleRate = 24000
            control.packetSize, control.bitRate, control.complexity = 480 * packet_frames, 24000, 2
            encoded = bytearray(b"\x02#!SILK_V3")
            for frame in range(60):
                samples = ffi.new("int16_t[]", [
                    int(math.sin((frame * 480 + i) * 2 * math.pi * 440 / 24000) * 5000)
                    for i in range(480)])
                packet, count = ffi.new("uint8_t[1250]"), ffi.new("int16_t *", 1250)
                code = lib.SKP_Silk_SDK_Encode(state, control, samples, 480, packet, count)
                self.assertEqual(code, 0)
                if count[0]:
                    encoded.extend(struct.pack("<h", count[0]))
                    encoded.extend(bytes(ffi.buffer(packet, count[0])))
            with self.subTest(packet_ms=packet_frames * 20):
                result = codec.decode_voice(bytes(encoded))
                self.assertEqual(codec.validate_wav(result), 1.2)
            if packet_frames == 3:
                # A real worker must fail instead of publishing a clipped voice.
                with patch.object(codec, "MAX_PCM_BYTES", 4800), self.assertRaises(ValueError):
                    codec.decode_voice(bytes(encoded))


if __name__ == "__main__":
    unittest.main()
