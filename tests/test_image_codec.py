"""Synthetic raster/cache fixtures; no real WeChat files or processes are read."""

from __future__ import annotations

import base64
import ctypes
import hashlib
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


def _v2(plain=PNG, aes_size=32, xor_size=12, xor_key=XOR, aes_key=KEY):
    head = plain[:aes_size]
    pad = 16 - len(head) % 16
    cipher = Cipher(algorithms.AES(aes_key), modes.ECB()).encryptor()
    encrypted = cipher.update(head + bytes([pad]) * pad) + cipher.finalize()
    return (codec.V2_MAGIC + struct.pack("<II", aes_size, xor_size) + b"\x00"
            + encrypted + plain[aes_size:len(plain) - xor_size if xor_size else None]
            + (bytes(byte ^ xor_key for byte in plain[-xor_size:]) if xor_size else b""))


class _FakeConfigProcess:
    """Only synthetic module/configuration bytes; no OS API calls."""

    def __init__(self, wxid="wxid_test", dword=123456789, back=0x138):
        self.wxid, self.dword = wxid, dword
        self.base, self.anchor, self.cfg, self.identity = 0x100000, 0x200000, 0x300000, 0x400000
        module = bytearray(1024)
        # The landmark and its SSO metadata cross a 512-byte read boundary.
        at = 500
        module[at:at + 13] = b"global_config"
        struct.pack_into("<QQ", module, at + 16, 13, 15)
        struct.pack_into("<Q", module, at + 16 - back, self.anchor)
        anchor = bytearray(0x70)
        struct.pack_into("<Q", anchor, 0x68, self.cfg)
        cfg = bytearray(0x80)
        struct.pack_into("<I", cfg, 0x40, dword)
        raw_identity = wxid.encode("utf-8")
        capacity = 15 if len(raw_identity) <= 15 else max(31, len(raw_identity))
        if capacity == 15:
            cfg[0x48:0x48 + len(raw_identity)] = raw_identity
        else:
            struct.pack_into("<Q", cfg, 0x48, self.identity)
        struct.pack_into("<QQ", cfg, 0x58, len(raw_identity), capacity)
        self.memory = {self.base: module, self.anchor: anchor, self.cfg: cfg, self.identity: raw_identity}
        self.snapshot_requests, self.accesses, self.reads, self.closed = [], [], [], []
        self.module_index = 0

    def CreateToolhelp32Snapshot(self, flags, pid):
        self.snapshot_requests.append((flags, pid))
        return 77

    def _module(self, target):
        entry = target._obj
        if self.module_index == 0:
            entry.szModule = "other.dll"
            entry.modBaseAddr, entry.modBaseSize = 0x90000, 32
            return True
        if self.module_index == 1:
            entry.szModule = "Weixin.dll"
            entry.modBaseAddr, entry.modBaseSize = self.base, len(self.memory[self.base])
            return True
        return False

    def Module32FirstW(self, handle, target):
        self.module_index = 0
        return self._module(target)

    def Module32NextW(self, handle, target):
        self.module_index += 1
        return self._module(target)

    def OpenProcess(self, access, inherit, pid):
        self.accesses.append((access, inherit, pid))
        return 99

    def VirtualQueryEx(self, handle, address, target, size):
        info = target._obj
        info.BaseAddress, info.RegionSize = self.base, len(self.memory[self.base])
        info.State, info.Protect, info.Type = 0x1000, 0x02, 0x1000000
        return size

    def ReadProcessMemory(self, handle, address, target, size, count):
        self.reads.append((address.value, size))
        for base, memory in self.memory.items():
            if base <= address.value and address.value + size <= base + len(memory):
                begin = address.value - base
                payload = bytes(memory[begin:begin + size])
                ctypes.memmove(target, payload, len(payload))
                count._obj.value = len(payload)
                return True
        return False

    def CloseHandle(self, handle):
        self.closed.append(handle)


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

    def test_wxgf_passes_complete_decrypted_body_including_xor_tail(self):
        image = b"wxgf" + bytes(range(80))
        for xor_size in (0, 16):
            with self.subTest(xor_size=xor_size), patch.object(codec, "decode_wxgf", return_value=PNG) as decode:
                self.assertEqual(codec.decode_image(_v2(image, aes_size=32, xor_size=xor_size), KEY, XOR), PNG)
            decode.assert_called_once_with(image)

    def test_wxgf_never_decodes_partial_aes_prefix_without_xor_key(self):
        image = b"wxgf" + bytes(range(80))
        with patch.object(codec, "decode_wxgf") as decode, self.assertRaisesRegex(ValueError, "XOR"):
            codec.decode_image(_v2(image, aes_size=32, xor_size=16), KEY)
        decode.assert_not_called()

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

    def test_config_scan_derives_and_validates_exact_account_for_both_pointer_offsets(self):
        from wechat_readable_export._vendor import db_readonly

        for wxid, dword, back, plain in (("wxid_test", 123456789, 0x138, PNG),
                                       ("wxid_synthetic_account", 0xFEDCBA98, 0x130, PNG),
                                       ("wxid_zero", 0, 0x138, b"wxgf" + bytes(80))):
            with self.subTest(back=back, dword=dword):
                api = _FakeConfigProcess(wxid, dword, back)
                key = hashlib.md5((str(dword) + wxid).encode()).hexdigest()[:16].encode("ascii")
                sample = _v2(plain, aes_key=key, xor_key=dword & 0xFF)
                with patch.object(codec.os, "name", "nt"), patch.object(codec, "_READ_CHUNK", 512), \
                     patch.object(codec, "_config_windows_api", return_value=(api, db_readonly)):
                    self.assertEqual(codec.find_config_image_key([123], wxid, [sample]), (key, dword & 0xFF))
                self.assertEqual(api.snapshot_requests, [(0x18, 123)])
                self.assertEqual(api.accesses, [(0x0010 | 0x0400, False, 123)])
                self.assertEqual(api.closed, [77, 99])
                self.assertTrue(all(size <= 512 for _, size in api.reads))
                # The database cipher/master-key offset is never requested.
                self.assertNotIn(api.cfg + 0x2B8, [address for address, _ in api.reads])

    def test_config_scan_rejects_wrong_account_and_corrupted_probe_and_closes_handles(self):
        from wechat_readable_export._vendor import db_readonly

        for wrong_account in (True, False):
            api = _FakeConfigProcess()
            key = hashlib.md5((str(api.dword) + api.wxid).encode()).hexdigest()[:16].encode("ascii")
            sample = bytearray(_v2(aes_key=key, xor_key=api.dword & 0xFF))
            if not wrong_account:
                sample[15 + 47] ^= 1
            with patch.object(codec.os, "name", "nt"), patch.object(codec, "_READ_CHUNK", 512), \
                 patch.object(codec, "_config_windows_api", return_value=(api, db_readonly)):
                self.assertEqual(codec.find_config_image_key([123], "wxid_else" if wrong_account else api.wxid,
                                                            [bytes(sample)]), (None, None))
            self.assertEqual(api.closed, [77, 99])
            if wrong_account:
                self.assertNotIn(api.cfg + 0x40, [address for address, _ in api.reads])

    def test_config_scan_honors_global_read_budget_and_invalid_snapshot(self):
        from wechat_readable_export._vendor import db_readonly

        api = _FakeConfigProcess()
        with patch.object(codec.os, "name", "nt"), patch.object(codec, "_READ_CHUNK", 512), \
             patch.object(codec, "_CONFIG_BYTE_LIMIT", 1024), \
             patch.object(codec, "_config_windows_api", return_value=(api, db_readonly)):
            self.assertEqual(codec.find_config_image_key([123], api.wxid, [_v2()]), (None, None))
        self.assertEqual(sum(size for _, size in api.reads), 1024)
        self.assertEqual(api.closed, [77, 99])

        api = _FakeConfigProcess()
        with patch.object(codec.os, "name", "nt"), \
             patch.object(api, "CreateToolhelp32Snapshot", return_value=ctypes.c_void_p(-1).value), \
             patch.object(codec, "_config_windows_api", return_value=(api, db_readonly)):
            self.assertEqual(codec.find_config_image_key([123], api.wxid, [_v2()]), (None, None))
        self.assertEqual(api.accesses, [])
        self.assertEqual(api.closed, [])

    def test_config_scan_requires_valid_explicit_account_and_sample_before_os_access(self):
        with patch.object(codec, "_config_windows_api", side_effect=AssertionError("must not scan")), \
             patch.object(codec.os, "name", "nt"):
            for wxid, pids, samples in (("", [123], [_v2()]), ("bad id", [123], [_v2()]),
                                       ("x" * 129, [123], [_v2()]), ("\ud800", [123], [_v2()]),
                                       ("wxid_test", [], [_v2()]), ("wxid_test", [123], [PNG])):
                self.assertEqual(codec.find_config_image_key(pids, wxid, samples), (None, None))
            with patch.object(codec.os, "name", "posix"):
                self.assertEqual(codec.find_config_image_key([123], "wxid_test", [_v2()]), (None, None))

    def test_config_scan_bounds_remote_identity_and_stops_at_deadline(self):
        from wechat_readable_export._vendor import db_readonly

        api = _FakeConfigProcess()
        struct.pack_into("<Q", api.memory[api.cfg], 0x58, 0x7FFFFFFF)
        with patch.object(codec.os, "name", "nt"), patch.object(codec, "_READ_CHUNK", 512), \
             patch.object(codec, "_config_windows_api", return_value=(api, db_readonly)):
            self.assertEqual(codec.find_config_image_key([123], api.wxid, [_v2()]), (None, None))
        self.assertTrue(all(size <= 512 for _, size in api.reads))
        self.assertNotIn(api.identity, [address for address, _ in api.reads])
        self.assertEqual(api.closed, [77, 99])

        api = _FakeConfigProcess()
        with patch.object(codec.os, "name", "nt"), patch.object(codec, "_SCAN_SECONDS", 0), \
             patch.object(codec, "_config_windows_api", return_value=(api, db_readonly)):
            self.assertEqual(codec.find_config_image_key([123], api.wxid, [_v2()]), (None, None))
        self.assertEqual(api.snapshot_requests, [])
        self.assertEqual(api.reads, [])

    def test_config_scan_closes_snapshot_and_process_handles_on_errors(self):
        from wechat_readable_export._vendor import db_readonly

        for failing_method, expected_closed in (("Module32NextW", [77]), ("ReadProcessMemory", [77, 99])):
            api = _FakeConfigProcess()
            with patch.object(codec.os, "name", "nt"), \
                 patch.object(api, failing_method, side_effect=OSError("synthetic failure")), \
                 patch.object(codec, "_config_windows_api", return_value=(api, db_readonly)):
                with self.assertRaisesRegex(OSError, "synthetic"):
                    codec.find_config_image_key([123], api.wxid, [_v2()])
            self.assertEqual(api.closed, expected_closed)

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

    def test_scan_shares_budget_across_process_heaps_before_mapped_pages(self):
        from wechat_readable_export._vendor import db_readonly

        class FakeProcesses:
            def __init__(self, first_region_type):
                self.regions = {
                    100: [(0x1000, b"!" * 96, first_region_type)],
                    200: [(0x2000, b"!" * 8 + KEY + b"!" * 8, 0x20000)],
                }
                self.accesses = []
                self.reads = []
                self.closed = []

            def OpenProcess(self, access, inherit, pid):
                self.accesses.append((access, inherit, pid))
                return pid

            def VirtualQueryEx(self, handle, address, target, size):
                at = address.value or 0
                for base, memory, region_type in self.regions[handle]:
                    if at < base + len(memory):
                        info = target._obj
                        info.BaseAddress = base
                        info.RegionSize = len(memory)
                        info.State = 0x1000
                        info.Protect = 0x04
                        info.Type = region_type
                        return size
                return 0

            def ReadProcessMemory(self, handle, address, target, size, count):
                self.reads.append((handle, address.value, size))
                for base, memory, _ in self.regions[handle]:
                    if base <= address.value < base + len(memory):
                        begin = address.value - base
                        payload = memory[begin:begin + size]
                        ctypes.memmove(target, payload, len(payload))
                        count._obj.value = len(payload)
                        return True
                return False

            def CloseHandle(self, handle):
                self.closed.append(handle)

        for first_type, expected_read_pids in ((0x1000000, [200]), (0x20000, [100, 200])):
            with self.subTest(first_type=first_type):
                api = FakeProcesses(first_type)
                with patch.object(codec.os, "name", "nt"), patch.object(codec, "_READ_CHUNK", 32), \
                     patch.object(codec, "_SCAN_BYTE_LIMIT", 64), \
                     patch.object(db_readonly, "initialize_windows_api"), patch.object(db_readonly, "_k32", api):
                    self.assertEqual(codec.find_image_key([100, 200], [_v2()]), (KEY, XOR))
                self.assertEqual([pid for pid, _, _ in api.reads], expected_read_pids)
                self.assertLessEqual(sum(size for _, _, size in api.reads), 64)
                self.assertEqual(api.accesses, [(0x0010 | 0x0400, False, 100), (0x0010 | 0x0400, False, 200)])
                self.assertEqual(api.closed, [100, 200])

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
