"""Synthetic raster/cache fixtures; no real WeChat files or processes are read."""

from __future__ import annotations

import base64
import ctypes
import io
import struct
import unittest
from unittest.mock import patch
import zlib

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from PIL import Image

from wechat_readable_export import image_codec as codec


KEY = b"0123456789abcdef"
XOR = 0x5A


def _png() -> bytes:
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(b"\x00\xff\x00\x00"))
            + chunk(b"IEND", b""))


PNG = _png()
GIF = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==")


def _pillow_image(format):
    stream = io.BytesIO()
    Image.new("RGB", (16, 16), "red").save(stream, format=format)
    return stream.getvalue()


WEBP = _pillow_image("WEBP")
JPEG = _pillow_image("JPEG")


def _v2(plain=PNG, aes_size=32, xor_size=12, xor_key=XOR):
    head = plain[:aes_size]
    pad = 16 - len(head) % 16
    cipher = Cipher(algorithms.AES(KEY), modes.ECB()).encryptor()
    encrypted = cipher.update(head + bytes([pad]) * pad) + cipher.finalize()
    return (codec.V2_MAGIC + struct.pack("<II", aes_size, xor_size) + b"\x00"
            + encrypted + plain[aes_size:len(plain) - xor_size if xor_size else None]
            + (bytes(byte ^ xor_key for byte in plain[-xor_size:]) if xor_size else b""))


class ImageCodecTests(unittest.TestCase):
    def test_plain_rasters_and_small_cache_footer(self):
        for image in (PNG, GIF, WEBP):
            with self.subTest(image=image[:8]):
                self.assertEqual(codec.decode_image(image), image)
                self.assertEqual(codec.decode_image(image + b"footer"), image)

    def test_legacy_xor_is_derived_from_signature_and_validated(self):
        for image in (PNG, GIF, WEBP):
            for key in (0, 1, 0x88, 0xFF):
                with self.subTest(image=image[:8], key=key):
                    encoded = bytes(byte ^ key for byte in image)
                    self.assertEqual(codec.decode_image(encoded), image)
                    self.assertEqual(codec.decode_image(encoded, xor_key=key), image)
        with self.assertRaises(ValueError):
            codec.decode_image(bytes(byte ^ XOR for byte in PNG), xor_key=XOR + 1)

    def test_upstream_v1_single_byte_xor_body(self):
        cache = codec.V1_MAGIC + b"unused-header-16" + bytes(byte ^ XOR for byte in PNG)
        self.assertEqual(len(b"unused-header-16"), 16)
        self.assertEqual(codec.decode_image(cache), PNG)

    def test_v2_mixed_segments_and_tail_key_recovery(self):
        for aes_size in (0, 16, 17, 32, 48):
            with self.subTest(aes_size=aes_size):
                data = _v2(aes_size=aes_size)
                self.assertEqual(codec.decode_image(data, KEY, XOR), PNG)
                self.assertEqual(codec.decode_image(data, KEY), PNG)

    def test_ambiguous_jpeg_footer_does_not_accept_wrong_xor_candidate(self):
        image = JPEG + b"\x00\x26"
        data = _v2(image, aes_size=32, xor_size=len(image) - 32)
        candidates = codec._tail_keys(data, len(image) - 32)
        self.assertEqual(candidates[:2], [0xA5, XOR])
        with self.assertRaises(ValueError):
            codec.decode_image(data, KEY, 0xA5)
        self.assertEqual(codec.decode_image(data, KEY), JPEG)

    def test_v2_no_xor_segment_requires_no_xor_key(self):
        self.assertEqual(codec.decode_image(_v2(xor_size=0), KEY), PNG)

    def test_v2_gif_trailer_can_recover_xor_after_header_validation(self):
        self.assertEqual(codec.decode_image(_v2(GIF, aes_size=16, xor_size=1), KEY), GIF)

    def test_v2_webp_with_explicit_xor_key(self):
        self.assertEqual(codec.decode_image(_v2(WEBP, aes_size=16, xor_size=4), KEY, XOR), WEBP)
        with self.assertRaisesRegex(ValueError, "XOR"):
            codec.decode_image(_v2(WEBP, aes_size=16, xor_size=4), KEY)

    def test_wrong_or_missing_aes_key_and_corrupt_padding_fail(self):
        for key in (None, b"short", b"wrongwrongwrong12"):
            with self.subTest(key=key), self.assertRaises(ValueError):
                codec.decode_image(_v2(), key)
        data = bytearray(_v2())
        data[15 + 47] ^= 0x01
        with self.assertRaisesRegex(ValueError, "AES"):
            codec.decode_image(bytes(data), KEY, XOR)

    def test_truncated_or_overlapping_v2_segments_fail(self):
        for data in (codec.V2_MAGIC, _v2()[:30], _v2()[:40],
                     codec.V2_MAGIC + struct.pack("<II", 0xFFFFFFFF, 0) + bytes(32),
                     codec.V2_MAGIC + struct.pack("<II", 16, 0xFFFFFFFF) + bytes(32)):
            with self.subTest(size=len(data)), self.assertRaises(ValueError):
                codec.decode_image(data, KEY, XOR)

    def test_unknown_incomplete_and_oversized_inputs_fail(self):
        for data in (b"", b"arbitrary private bytes", PNG[:-1], GIF[:-1], b"wxgf" + bytes(40)):
            with self.subTest(data=data[:8]), self.assertRaises(ValueError):
                codec.decode_image(data)
        with patch.object(codec, "MAX_IMAGE_BYTES", 4), self.assertRaisesRegex(ValueError, "限制"):
            codec.decode_image(PNG)
        for key in (True, -1, 256, "1"):
            with self.subTest(key=key), self.assertRaisesRegex(ValueError, "XOR"):
                codec.decode_image(PNG, xor_key=key)

    def test_candidate_extraction_ascii_utf16_and_chunk_boundary(self):
        self.assertEqual(list(codec._key_candidates(b"!" + KEY + b"!")), [KEY])
        self.assertEqual(list(codec._key_candidates(b"!" + KEY * 2 + b"!")), [KEY])
        self.assertEqual(list(codec._key_candidates(b"!" + (KEY * 2).decode().encode("utf-16le") + b"!")), [KEY])
        self.assertEqual(list(codec._key_candidates(b"X" + KEY + b"Y")), [])

    def test_no_scanning_without_valid_v2_samples_or_on_other_platforms(self):
        from wechat_readable_export._vendor import db_readonly
        with patch.object(db_readonly, "initialize_windows_api", side_effect=AssertionError("must not scan")):
            self.assertEqual(codec.find_image_key([123], [PNG]), (None, None))
            self.assertEqual(codec.find_image_key([], [_v2()]), (None, None))
            with patch.object(codec.os, "name", "posix"):
                self.assertEqual(codec.find_image_key([123], [_v2()]), (None, None))

    def test_mocked_windows_scan_is_read_only_closes_handle_and_validates_full_image(self):
        from wechat_readable_export._vendor import db_readonly

        class FakeWindows:
            def __init__(self, memory):
                self.memory = memory
                self.accesses = []
                self.closed = []

            def OpenProcess(self, access, inherit, pid):
                self.accesses.append((access, inherit, pid))
                return 99

            def VirtualQueryEx(self, handle, address, target, size):
                if address.value:
                    return 0
                info = target._obj
                info.BaseAddress = 0x1000
                info.RegionSize = len(self.memory)
                info.State = 0x1000
                info.Protect = 0x04
                info.Type = 0x20000
                return size

            def ReadProcessMemory(self, handle, address, target, size, count):
                begin = address.value - 0x1000
                payload = self.memory[begin:begin + size]
                ctypes.memmove(target, payload, len(payload))
                count._obj.value = len(payload)
                return True

            def CloseHandle(self, handle):
                self.closed.append(handle)

        # The valid candidate crosses two reads; an invalid candidate precedes it.
        api = FakeWindows(b"!badbadbadbadbad1!" + b"!" * 8 + KEY + b"!end")
        with patch.object(codec.os, "name", "nt"), patch.object(codec, "_READ_CHUNK", 32), \
             patch.object(db_readonly, "initialize_windows_api"), patch.object(db_readonly, "_k32", api):
            self.assertEqual(codec.find_image_key([123], [_v2()]), (KEY, XOR))
        self.assertEqual(api.accesses, [(0x0010 | 0x0400, False, 123)])
        self.assertEqual(api.closed, [99])

        wxgf = _v2(b"wxgf" + bytes(80), aes_size=32, xor_size=12)
        with patch.object(codec.os, "name", "nt"), patch.object(codec, "_READ_CHUNK", 32), \
             patch.object(db_readonly, "initialize_windows_api"), patch.object(db_readonly, "_k32", api):
            self.assertEqual(codec.find_image_key([123], [wxgf]), (KEY, None))
            bad_padding = bytearray(wxgf)
            bad_padding[15 + 47] ^= 1
            self.assertEqual(codec.find_image_key([123], [bytes(bad_padding)]), (None, None))
        self.assertEqual(api.closed, [99, 99, 99])

    def test_readable_region_order_prioritizes_private_heap_over_image_pages(self):
        from wechat_readable_export._vendor import db_readonly

        class FakeRegions:
            def VirtualQueryEx(self, handle, address, target, size):
                if address.value is None:
                    entry = (0x1000, 16, 0x1000000, 0x02)  # MEM_IMAGE
                elif address.value == 0x1010:
                    entry = (0x2000, 16, 0x20000, 0x04)  # MEM_PRIVATE RW
                elif address.value == 0x2010:
                    entry = (0x3000, 16, 0x20000, 0x02)  # MEM_PRIVATE RO
                else:
                    return 0
                info = target._obj
                info.BaseAddress, info.RegionSize, info.Type, info.Protect = entry
                info.State = 0x1000
                return size

        self.assertEqual(codec._readable_regions(FakeRegions(), 99, db_readonly, float("inf")),
                         [(0x2000, 16), (0x3000, 16), (0x1000, 16)])

    def test_wxgf_aes_probe_requires_valid_pkcs7_but_still_does_not_display(self):
        data = _v2(b"wxgf" + bytes(80), aes_size=32, xor_size=12)
        self.assertTrue(codec._probe_matches(data, KEY))
        self.assertTrue(codec._v2_parts(data, KEY)[0].startswith(b"wxgf"))
        corrupted = bytearray(data)
        corrupted[15 + 47] ^= 1
        with self.assertRaises(ValueError):
            codec._v2_parts(bytes(corrupted), KEY)
        with self.assertRaisesRegex(ValueError, "wxgf"):
            codec.decode_image(data, KEY, XOR)


if __name__ == "__main__":
    unittest.main()
