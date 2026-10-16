"""Synthetic wxgf containers only; no real account files are used."""

from __future__ import annotations

import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, features

from wechat_readable_export import wxgf_codec as codec


def _png(size=(96, 64)):
    output = io.BytesIO()
    Image.new("RGB", size, "green").save(output, "PNG")
    return output.getvalue()


CONTAINER = b"wxgf" + bytes(8) + b"\x00\x00\x00\x01\x40\x01" + bytes(64)


class _Process:
    def __init__(self, command, *, output=None, status=0, running=False, **kwargs):
        self.command, self.options = command, kwargs
        self.returncode = None if running else status
        self.killed = False
        self.waited = False
        self.output = Path(command[-1])
        self.directory = self.output.parent
        self.source = Path(command[command.index("-i") + 1]).read_bytes()
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


class WxgfCodecTests(unittest.TestCase):
    def _run(self, *, output=None, status=0, running=False):
        processes = []

        def launch(command, **kwargs):
            process = _Process(command, output=output, status=status, running=running, **kwargs)
            processes.append(process)
            return process

        return processes, patch.object(codec.subprocess, "Popen", side_effect=launch)

    def test_valid_png_preserves_dimensions_and_runs_local_bounded_decoder(self):
        processes, launch = self._run(output=_png())
        with patch.object(codec, "_ffmpeg_executable", return_value="ffmpeg"), launch:
            result = codec.decode_wxgf(CONTAINER)
        process = processes[0]
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (96, 64))
        self.assertEqual(process.source, CONTAINER[12:])
        self.assertFalse(process.directory.exists())
        self.assertTrue(process.waited)
        self.assertFalse(process.options["shell"])
        self.assertEqual(process.options["stdin"], subprocess.DEVNULL)
        self.assertEqual(process.options["stdout"], subprocess.DEVNULL)
        self.assertEqual(process.options["stderr"], subprocess.DEVNULL)
        command = process.command
        input_index = command.index("-i")
        self.assertLess(command.index("-protocol_whitelist"), input_index)
        self.assertEqual(command[command.index("-protocol_whitelist") + 1], "file")
        self.assertLess(command.index("-max_pixels"), input_index)
        self.assertEqual(command[command.index("-max_pixels") + 1], "40000000")
        self.assertEqual(command[command.index("-frames:v") + 1], "1")
        self.assertNotIn("-vf", command)
        self.assertNotIn("-s", command)

    def test_missing_binary_fails_without_starting_process(self):
        with patch.dict("sys.modules", {"imageio_ffmpeg": None}), \
             patch.object(codec.shutil, "which", return_value=None), \
             patch.object(codec.subprocess, "Popen") as launch, \
             self.assertRaisesRegex(ValueError, "FFmpeg"):
            codec.decode_wxgf(CONTAINER)
        launch.assert_not_called()

    def test_malformed_and_oversized_containers_never_start_decoder(self):
        cases = [b"", b"jpeg", b"wxgf" + bytes(20),
                 b"wxgf\x00\x00\x00\x01\xc0\x01" + bytes(20),
                 b"wxgf\x00\x00\x00\x01\x40\x00" + bytes(20)]
        with patch.object(codec.subprocess, "Popen") as launch:
            for data in cases:
                with self.subTest(data=data[:10]), self.assertRaises(ValueError):
                    codec.decode_wxgf(data)
            with patch.object(codec, "MAX_WXGF_BYTES", 8), self.assertRaisesRegex(ValueError, "限制"):
                codec.decode_wxgf(CONTAINER)
        launch.assert_not_called()

    def test_header_search_has_a_fixed_bound(self):
        with patch.object(codec, "_MAX_HEADER_BYTES", 8), self.assertRaises(ValueError):
            codec._hevc_stream(CONTAINER)

    def test_three_byte_annex_b_start_marker_is_supported(self):
        stream = b"\x00\x00\x01\x40\x01" + bytes(20)
        self.assertEqual(codec._hevc_stream(b"wxgfheader!!" + stream), stream)

    def test_lossless_webp_optimization_preserves_every_decoded_pixel(self):
        if not features.check("webp"):
            self.skipTest("Pillow was built without WebP support")
        original = _png()
        result = codec._smaller_lossless(original)
        self.assertLess(len(result), len(original))
        with Image.open(io.BytesIO(original)) as before, Image.open(io.BytesIO(result)) as after:
            self.assertEqual(after.size, before.size)
            self.assertEqual(after.tobytes(), before.tobytes())

    def test_pillow_without_webp_encoder_retains_validated_png(self):
        original = _png()
        with patch.object(Image.Image, "save", side_effect=KeyError("WEBP")):
            self.assertEqual(codec._smaller_lossless(original), original)

    def test_corrupt_failed_and_oversized_output_is_rejected_and_cleaned(self):
        for output, status, limit in [(b"not an image", 0, codec.MAX_MEDIA_BYTES),
                                      (_png(), 2, codec.MAX_MEDIA_BYTES),
                                      (_png(), 0, 4), (None, 0, codec.MAX_MEDIA_BYTES)]:
            processes, launch = self._run(output=output, status=status)
            with self.subTest(status=status, limit=limit), \
                 patch.object(codec, "_ffmpeg_executable", return_value="ffmpeg"), \
                 patch.object(codec, "MAX_MEDIA_BYTES", limit), launch, self.assertRaises(ValueError):
                codec.decode_wxgf(CONTAINER)
            self.assertFalse(processes[0].directory.exists())

    def test_timeout_kills_decoder_and_removes_decrypted_temporary_files(self):
        processes, launch = self._run(running=True)
        with patch.object(codec, "_ffmpeg_executable", return_value="ffmpeg"), \
             patch.object(codec, "_DECODE_SECONDS", 0), launch, self.assertRaisesRegex(ValueError, "超时"):
            codec.decode_wxgf(CONTAINER)
        self.assertTrue(processes[0].killed)
        self.assertTrue(processes[0].waited)
        self.assertFalse(processes[0].directory.exists())

    def test_oversized_in_progress_output_kills_decoder(self):
        processes, launch = self._run(output=b"too large", running=True)
        with patch.object(codec, "_ffmpeg_executable", return_value="ffmpeg"), \
             patch.object(codec, "MAX_MEDIA_BYTES", 4), launch, self.assertRaisesRegex(ValueError, "限制"):
            codec.decode_wxgf(CONTAINER)
        self.assertTrue(processes[0].killed)
        self.assertFalse(processes[0].directory.exists())

    def test_real_synthetic_hevc_roundtrip_preserves_source_pixel_dimensions(self):
        try:
            executable = codec._ffmpeg_executable()
        except ValueError:
            self.skipTest("local FFmpeg is not installed")
        with tempfile.TemporaryDirectory(prefix="wechat-test-hevc-") as directory:
            target = Path(directory) / "synthetic.hevc"
            # A small generated solid-color frame is the complete test fixture.
            command = [executable, "-nostdin", "-hide_banner", "-loglevel", "error",
                       "-f", "lavfi", "-i", "color=c=green:s=96x64:r=1",
                       "-frames:v", "1", "-c:v", "libx265", "-threads", "1",
                       "-x265-params", "log-level=error:pools=none:frame-threads=1",
                       "-pix_fmt", "yuv420p", "-f", "hevc", str(target)]
            subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, check=True, timeout=15,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            container = b"wxgf" + bytes(8) + target.read_bytes()
            result = codec.decode_wxgf(container)
            with patch.object(codec, "MAX_IMAGE_PIXELS", 100), self.assertRaises(ValueError):
                codec.decode_wxgf(container)
            with self.assertRaises(ValueError):
                codec.decode_wxgf(container[:len(container) // 2])
        with Image.open(io.BytesIO(result)) as image:
            self.assertEqual(image.size, (96, 64))
            red, green, blue = image.getpixel((48, 32))
            self.assertGreater(green, 100)
            self.assertLess(red + blue, 20)


if __name__ == "__main__":
    unittest.main()
