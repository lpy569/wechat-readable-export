"""Reviewed, reduced read-only subset of wechatauto-replica db.py.

Upstream: fanyuantaier/wechatauto-replica, Apache-2.0.
Tag v1.2.4.4, commit 91dc0e1a601013b759b261061d8f7642f736af90.
Source: https://github.com/fanyuantaier/wechatauto-replica/blob/91dc0e1a601013b759b261061d8f7642f736af90/wechatauto/db.py

Changes: retained ONLY Config.Cipher scanning, key verification, page decryption;
removed package initialization, UI, constructor, account detection/self-healing,
all cache writes and fallback paths. Standard logging replaces upstream logger.
Windows API binding is lazy and CloseHandle ABI is explicitly declared.
Strict full-page authentication and WAL validation live in backend.py.
"""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import hashlib
import hmac as hmac_mod
import logging
import re
import struct
from typing import Dict, List, Optional, Tuple
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

wxlog = logging.getLogger(__name__)
PAGE_SZ = 4096
RESERVE_SZ = 80
CONFIG_CIPHER_NAME = b"com.Tencent.WCDB.Config.Cipher"
CONFIG_XOR_MASK = bytes.fromhex(
    "d2c7442458020000004889442450488b"
    "450048844c2448488944254048584c24"
)
HEX_LITERAL_RE = re.compile(rb"[xX]'([0-9a-fA-F]{64,192})'")
_k32 = None

def initialize_windows_api():
    global _k32
    if _k32 is not None:
        return
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.VirtualQueryEx.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.POINTER(_MBI), ctypes.c_size_t,
    ]
    k32.VirtualQueryEx.restype = ctypes.c_size_t
    k32.ReadProcessMemory.argtypes = [
        wintypes.HANDLE, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_size_t),
    ]
    k32.ReadProcessMemory.restype = wintypes.BOOL
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL
    _k32 = k32


class _MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("__alignment1", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
        ("__alignment2", wintypes.DWORD),
    ]


def _pbkdf2(passwd: bytes, salt: bytes, iters: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha512", passwd, salt, iters, dklen=32)


def _aes_cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    dec = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
    return dec.update(data) + dec.finalize()


def _split_key(key: bytes) -> Tuple[bytes, Optional[bytes]]:
    """拆分密钥形态：32 字节 = 标准裸 key（salt 用文件头前 16 字节）；
    48 字节 = SQLCipher 4 "Raw Key with Explicit Salt"（前 32B key + 后 16B salt，
    用于 cipher_plaintext_header_size 明文头模式）。返回 (enc_key, salt_or_None)。"""
    if len(key) == 48:
        return key[:32], key[32:]
    return key, None


def _verify_enc_key(enc_key: bytes, page1: bytes, salt: Optional[bytes] = None) -> bool:
    """验证 enc_key 是否为 page1 的 SQLCipher 4 密钥。

    - enc_key 为 32 字节裸 key：默认用文件头前 16 字节作 salt（标准形式）；
    - enc_key 为 48 字节 key+salt：显式 salt 优先于文件头（明文头模式）；
    - 也允许单独传 salt 参数覆盖（供提取逻辑按 96hex 拆分尝试）。
    """
    if len(page1) < PAGE_SZ:
        return False
    if len(enc_key) == 48 and salt is None:
        enc_key, salt = enc_key[:32], enc_key[32:]
    if salt is None:
        salt = page1[:16]
    elif len(salt) != 16:
        return False
    mac_salt = bytes(b ^ 0x3A for b in salt)
    mac_key = _pbkdf2(enc_key, mac_salt, 2)
    hmac_data = page1[16: PAGE_SZ - RESERVE_SZ + 16]
    stored_hmac = page1[PAGE_SZ - 64: PAGE_SZ]
    hm = hmac_mod.new(mac_key, hmac_data, hashlib.sha512)
    hm.update(struct.pack("<I", 1))
    return hm.digest() == stored_hmac


def _decrypt_page(enc_key: bytes, page: bytes, pgno: int) -> bytes:
    iv = page[PAGE_SZ - RESERVE_SZ: PAGE_SZ - RESERVE_SZ + 16]
    if pgno == 1:
        # 48 字节 key（key+salt）= cipher_plaintext_header_size 明文头模式：
        # 页 1 前 16 字节是明文头（非加密 salt），加密数据从 offset 16 开始；
        # 解密后拼接明文头 + 明文数据 + reserve。
        if len(enc_key) == 48:
            enc = page[16: PAGE_SZ - RESERVE_SZ]
            return page[:16] + _aes_cbc_decrypt(enc_key[:32], iv, enc) + b"\x00" * RESERVE_SZ
        enc = page[16: PAGE_SZ - RESERVE_SZ]
        return b"SQLite format 3\x00" + _aes_cbc_decrypt(enc_key, iv, enc) + b"\x00" * RESERVE_SZ
    enc = page[: PAGE_SZ - RESERVE_SZ]
    return _aes_cbc_decrypt(enc_key[:32] if len(enc_key) == 48 else enc_key, iv, enc) + b"\x00" * RESERVE_SZ


class WeChatDB:
    """No initializer or persistent state; backend supplies an explicit target."""

    def _extract_keys_pid(self, pid: int, tested: set) -> Dict[str, bytes]:
        h = _k32.OpenProcess(0x0010 | 0x0400, False, pid)
        if not h:
            return {}
        try:
            def read(addr: int, n: int):
                buf = ctypes.create_string_buffer(n)
                br = ctypes.c_size_t(0)
                if _k32.ReadProcessMemory(h, ctypes.c_void_p(addr), buf, n, ctypes.byref(br)) and br.value:
                    return buf.raw[: br.value]
                return None

            needles = self._find_bytes(h, read, CONFIG_CIPHER_NAME)
            pairs = [
                struct.pack("<Q", addr) + struct.pack("<Q", len(CONFIG_CIPHER_NAME))
                for addr in needles
            ]
            keys: Dict[str, bytes] = {}
            for pair in pairs:
                for qaddr in self._find_bytes(h, read, pair):
                    node = read(qaddr - 0x10, 0x50)
                    if not node or len(node) < 0x40:
                        continue
                    if struct.unpack_from("<Q", node, 0x10)[0] not in needles:
                        continue
                    if struct.unpack_from("<Q", node, 0x18)[0] != len(CONFIG_CIPHER_NAME):
                        continue
                    config_ptr = struct.unpack_from("<Q", node, 0x28)[0]
                    if not (0x10000 <= config_ptr < 0x800000000000):
                        continue
                    obj = read(config_ptr + 0x88, 0x28)
                    if not obj or len(obj) < 0x18:
                        continue
                    data_ptr = struct.unpack_from("<Q", obj, 0x8)[0]
                    data_len = struct.unpack_from("<Q", obj, 0x10)[0]
                    if not (0 < data_len <= 1024 and 0x10000 <= data_ptr < 0x800000000000):
                        continue
                    blob = read(data_ptr, int(data_len))
                    if not blob or len(blob) != data_len:
                        continue
                    decoded = bytes(
                        v ^ CONFIG_XOR_MASK[i % len(CONFIG_XOR_MASK)]
                        for i, v in enumerate(blob)
                    )
                    for m in HEX_LITERAL_RE.finditer(decoded):
                        run = m.group(1).decode().lower()
                        starts = [0]
                        if len(run) > 96:
                            starts += list(range(0, len(run) - 63, 32))
                            starts.append(len(run) - 64)
                        for s in dict.fromkeys(starts):
                            if s + 64 > len(run):
                                continue
                            cand = bytes.fromhex(run[s:s + 64])
                            if cand in tested or not self._probable_key(cand):
                                continue
                            tested.add(cand)
                            # 96hex 形式：后 32 hex 是显式 salt（Raw Key with
                            # Explicit Salt），与文件头 salt 都要尝试
                            explicit = None
                            if s + 96 <= len(run):
                                explicit = bytes.fromhex(run[s + 64: s + 96])
                            salt_choices = [None]
                            if explicit:
                                salt_choices.append(explicit)
                            for salt_opt in salt_choices:
                                for rel, path, _ in self._db_files:
                                    if rel in keys:
                                        continue
                                    with open(path, "rb") as f:
                                        page1 = f.read(PAGE_SZ)
                                    if _verify_enc_key(cand, page1, salt=salt_opt):
                                        # 显式 salt 通过 → 存 key+salt（明文头模式）；
                                        # 文件头 salt 通过 → 存裸 key
                                        keys[rel] = cand + (salt_opt or b"")
                                        break
        finally:
            _k32.CloseHandle(h)
        return keys

    @staticmethod
    def _probable_key(b: bytes) -> bool:
        return (
            len(b) == 32
            and len(set(b)) >= 15
            and b not in {b"\x00" * 32, b"\xff" * 32}
        )

    @staticmethod
    def _find_bytes(h, read, needle: bytes) -> List[int]:
        hits = []
        addr = 0
        while True:
            mbi = _MBI()
            r = _k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
            if r == 0:
                break
            if (
                mbi.State == 0x1000
                and (mbi.Protect & 0xFF) & 0xE6
                and not (mbi.Protect & 0x100)
                and 0 < mbi.RegionSize < 0x10000000
            ):
                buf = read(mbi.BaseAddress or 0, mbi.RegionSize)
                if buf:
                    base = mbi.BaseAddress or 0
                    pos = 0
                    while True:
                        pos = buf.find(needle, pos)
                        if pos < 0:
                            break
                        hits.append(base + pos)
                        pos += 1
            addr = (mbi.BaseAddress or 0) + mbi.RegionSize
        return hits
