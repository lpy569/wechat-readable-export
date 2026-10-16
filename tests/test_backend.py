"""Synthetic databases only: these tests never enumerate live Weixin processes."""

from __future__ import annotations

import hashlib
import hmac
import os
from pathlib import Path
import sqlite3
import struct
import tempfile
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from wechat_readable_export import backend
from wechat_readable_export.models import ExportError


TARGET = "wxid_test_friend"


def _encrypted_page(key: bytes, salt: bytes, plain: bytes, pgno: int) -> bytes:
    iv = bytes([pgno % 256]) * 16
    start = 16 if pgno == 1 else 0
    cipher = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    content = cipher.update(plain[start:4016]) + cipher.finalize()
    prefix = salt if pgno == 1 else b""
    mac_key = hashlib.pbkdf2_hmac("sha512", key, bytes(x ^ 0x3A for x in salt), 2, 32)
    mac = hmac.new(mac_key, content + iv + struct.pack("<I", pgno), hashlib.sha512).digest()
    return prefix + content + iv + mac


def _wal(frames: list[tuple[int, int, bytes]], salt: bytes = b"12345678") -> bytes:
    header = struct.pack(">IIII", 0x377F0682, 3007000, 4096, 0) + salt
    checksum = backend._wal_checksum(header, "<")
    header += struct.pack(">II", *checksum)
    output = bytearray(header)
    for pgno, commit_size, page in frames:
        prefix = struct.pack(">II", pgno, commit_size) + salt
        checksum = backend._wal_checksum(prefix[:8] + page, "<", *checksum)
        output += prefix + struct.pack(">II", *checksum) + page
    return bytes(output)


def _contact_db(path: Path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE contact (username TEXT, nick_name TEXT, remark TEXT)")
    connection.executemany("INSERT INTO contact VALUES (?, ?, ?)", [
        ("wxid_me", "本人", ""), (TARGET, "林同学", "林同学♡"),
        ("wxid_same_name", "林同学", "另一个人"),
    ])
    connection.commit()
    connection.close()


def _shard(path: Path, senders: list[tuple[int, str]], messages: list[tuple], name2id: bool = True):
    connection = sqlite3.connect(path)
    if name2id:
        connection.execute("CREATE TABLE Name2Id (user_name TEXT)")
        connection.executemany("INSERT INTO Name2Id (rowid,user_name) VALUES (?,?)", senders)
    table = "Msg_" + hashlib.md5(TARGET.encode()).hexdigest()
    connection.execute(f'CREATE TABLE "{table}" (local_id, local_type, server_id, '
                       'real_sender_id, create_time, message_content, sort_seq)')
    connection.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?,?,?)', messages)
    connection.commit()
    connection.close()


class AccountPathTests(unittest.TestCase):
    def test_account_and_root_paths_agree_and_multiple_accounts_require_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first = root / "wxid_one_abcd"
            (first / "db_storage").mkdir(parents=True)
            self.assertEqual(backend.list_accounts(root), backend.list_accounts(first))
            self.assertEqual(backend._select_account(first, None), first.resolve())
            second = root / "wxid_two_efgh"
            (second / "db_storage").mkdir(parents=True)
            with self.assertRaisesRegex(ExportError, "多个"):
                backend._select_account(root, None)
            self.assertEqual(backend._select_account(root, first.name), first.resolve())
            with self.assertRaises(ExportError):
                backend._select_account(root, "../wxid_one_abcd")

    def test_missing_and_file_paths_are_actionable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ExportError, "不存在"):
                backend.list_accounts(root / "missing")
            file = root / "file"
            file.write_text("not a folder")
            with self.assertRaisesRegex(ExportError, "不是目录"):
                backend.list_accounts(file)

    def test_only_numeric_message_shards_are_discovered(self):
        with tempfile.TemporaryDirectory() as temporary:
            storage = Path(temporary)
            folder = storage / "message"
            folder.mkdir()
            for name in ("message_10.db", "message_2.db", "message_resource.db",
                         "message_3.db-wal", "message_foo.db"):
                (folder / name).touch()
            self.assertEqual([p.name for p in backend._message_databases(storage)],
                             ["message_2.db", "message_10.db"])

    @unittest.skipUnless(os.name == "nt", "LocalReader is intentionally Windows-only")
    def test_context_error_clears_memory_and_removes_disposable_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            account = Path(temporary) / "wxid_fake_abcd"
            (account / "db_storage").mkdir(parents=True)
            saved_reader = None
            with self.assertRaisesRegex(RuntimeError, "synthetic failure"):
                with backend.LocalReader(account) as reader:
                    saved_reader = reader
                    working = reader.workdir
                    reader._keys["synthetic"] = b"fake-in-memory-key"
                    (working / "synthetic-plaintext.db").write_bytes(b"test data")
                    raise RuntimeError("synthetic failure")
            self.assertFalse(working.exists())
            self.assertEqual(saved_reader._keys, {})
            with self.assertRaisesRegex(ExportError, "已经关闭"):
                saved_reader.contacts()


class SyntheticSQLiteTests(unittest.TestCase):
    def test_contact_search_keeps_same_named_accounts_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contacts.db"
            _contact_db(path)
            results = backend._read_contacts(path, "林同学")
            self.assertEqual({c.username for c in results}, {TARGET, "wxid_same_name"})
            self.assertEqual(next(c for c in results if c.username == TARGET).display_name, "林同学♡")

    def test_each_shard_maps_its_own_sender_ids_and_keeps_duplicate_local_ids(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            first, second = root / "first.db", root / "second.db"
            _shard(first, [(2, "wxid_me"), (3, TARGET)], [
                (1, 1, 100, 2, 1000, "我发的", 1),
                (2, 1, 101, 3, 1001, "她发的", 2),
            ])
            # IDs restart and mean the reverse in the other shard.
            _shard(second, [(2, TARGET), (3, "wxid_me")], [
                (1, 1, 102, 2, 1002, "下一分片", 3),
                (2, 1, 103, 99, 1003, "未知发送者", 4),
            ])
            rows = backend._read_message_shards([
                ("message/message_0.db", first), ("message/message_1.db", second)], TARGET)
            self.assertEqual(len(rows), 4)
            self.assertEqual([r["_sender_username"] for r in rows],
                             ["wxid_me", TARGET, TARGET, None])
            self.assertEqual(rows[-1]["_sender_status"], "unmapped_sender_id")
            self.assertEqual(rows[2]["_db_rel"], "message/message_1.db")
            self.assertEqual([r["local_id"] for r in rows], [1, 2, 1, 2])

    def test_missing_sender_mapping_does_not_invent_self(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "shard.db"
            _shard(path, [], [(1, 1, 10, 2, 1000, "unknown", 1)], name2id=False)
            rows = backend._read_message_shards([("message/message_0.db", path)], TARGET)
            self.assertEqual(rows[0]["_sender_status"], "missing_name2id")
            self.assertIsNone(rows[0]["_sender_username"])

    def test_corrupt_shard_is_not_silently_skipped(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "broken.db"
            path.write_bytes(b"not sqlite")
            with self.assertRaisesRegex(ExportError, "未跳过"):
                backend._read_message_shards([("message/message_0.db", path)], TARGET)

    def test_sqlite_connections_reject_writes(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "contacts.db"
            _contact_db(path)
            connection = backend._connect_readonly(path)
            try:
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM contact")
            finally:
                connection.close()


class AuthenticatedSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.salt = bytes(range(16))
        # Empty, valid one-page SQLite database with SQLCipher's 80 reserve bytes.
        self.plain = bytearray(4096)
        self.plain[:16] = b"SQLite format 3\0"
        self.plain[16:18] = struct.pack(">H", 4096)
        self.plain[18:24] = bytes([1, 1, 80, 64, 32, 32])
        self.plain[28:32] = struct.pack(">I", 1)
        self.plain[44:48] = struct.pack(">I", 4)
        self.plain[56:60] = struct.pack(">I", 1)
        self.plain[100] = 13
        self.plain[105:107] = struct.pack(">H", 4016)
        self.encrypted = _encrypted_page(self.key, self.salt, bytes(self.plain), 1)

    def test_every_page_and_its_page_number_are_authenticated(self):
        mac_key = backend._mac_key(self.key, self.encrypted)
        self.assertEqual(backend._authenticated_page(self.key, mac_key, self.encrypted, 1), bytes(self.plain))
        damaged = bytearray(self.encrypted)
        damaged[200] ^= 1
        with self.assertRaisesRegex(ExportError, "HMAC"):
            backend._authenticated_page(self.key, mac_key, bytes(damaged), 1)
        with self.assertRaisesRegex(ExportError, "HMAC"):
            backend._authenticated_page(self.key, mac_key, self.encrypted, 2)

    def test_snapshot_is_valid_and_original_is_unchanged(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "encrypted.db", root / "scratch" / "plain.db"
            source.write_bytes(self.encrypted)
            before = source.read_bytes()
            metadata = backend._snapshot_database(source, self.key, destination)
            self.assertEqual(source.read_bytes(), before)
            self.assertEqual(metadata["authentication"], "all_pages_hmac_sha512")
            connection = backend._connect_readonly(destination)
            try:
                self.assertEqual(connection.execute("PRAGMA quick_check").fetchone()[0], "ok")
            finally:
                connection.close()

    def test_committed_wal_prefix_is_used_and_valid_uncommitted_tail_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain, wal = root / "plain.db", root / "source-wal"
            plain.write_bytes(self.plain)
            committed = bytearray(self.plain)
            committed[60:64] = struct.pack(">I", 17)
            tail = bytearray(self.plain)
            tail[60:64] = struct.pack(">I", 99)
            wal.write_bytes(_wal([
                (1, 1, _encrypted_page(self.key, self.salt, bytes(committed), 1)),
                (1, 0, _encrypted_page(self.key, self.salt, bytes(tail), 1)),
            ]))
            metadata = backend._merge_authenticated_wal(plain, wal, self.key,
                                                        backend._mac_key(self.key, self.encrypted))
            self.assertEqual(struct.unpack(">I", plain.read_bytes()[60:64])[0], 17)
            self.assertEqual(metadata["wal_committed_frames"], 1)
            self.assertEqual(metadata["wal_uncommitted_frames_ignored"], 1)

    def test_partial_or_corrupt_wal_never_falls_back(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain, wal = root / "plain.db", root / "source-wal"
            plain.write_bytes(self.plain)
            valid = _wal([(1, 1, self.encrypted)])
            for damaged in (valid[:-1], valid[:100] + bytes([valid[100] ^ 1]) + valid[101:]):
                wal.write_bytes(damaged)
                with self.assertRaises(ExportError):
                    backend._merge_authenticated_wal(plain, wal, self.key,
                                                    backend._mac_key(self.key, self.encrypted))

    def test_four_megabyte_preallocation_with_authenticated_old_generation_is_ignored(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain, wal = root / "plain.db", root / "source-wal"
            plain.write_bytes(self.plain)
            # A checkpoint restarted the WAL and only rewrote its header.
            # 4 MiB retains 1018 whole old frames and 112 old partial bytes.
            old_salt = struct.pack(">II", 27, 1234)
            new_salt = struct.pack(">II", 28, 5678)
            stale_capacity = _wal([(1, 1, self.encrypted)] * 1019, old_salt)[32:]
            current_header = _wal([], new_salt)
            preallocated = (current_header + stale_capacity)[:4 * 1024 * 1024]
            self.assertEqual((len(preallocated) - 32) % backend.WAL_FRAME_SIZE, 112)
            self.assertTrue(any(preallocated[-112:]))
            wal.write_bytes(preallocated)
            metadata = backend._merge_authenticated_wal(plain, wal, self.key,
                                                        backend._mac_key(self.key, self.encrypted))
            self.assertEqual(plain.read_bytes(), bytes(self.plain))
            self.assertEqual(metadata["wal_committed_frames"], 0)
            self.assertEqual(metadata["wal_stale_frames_ignored"], 1018)
            self.assertEqual(metadata["wal_preallocated_tail_bytes"], 112)

    def test_zero_preallocated_capacity_is_not_an_incomplete_activity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain, wal = root / "plain.db", root / "source-wal"
            plain.write_bytes(self.plain)
            prefix = _wal([(1, 1, self.encrypted)])
            wal.write_bytes(prefix + bytes(4 * 1024 * 1024 - len(prefix)))
            metadata = backend._merge_authenticated_wal(plain, wal, self.key,
                                                        backend._mac_key(self.key, self.encrypted))
            self.assertEqual(metadata["wal_committed_frames"], 1)
            self.assertEqual(metadata["wal_zero_frames_ignored"], 1017)
            self.assertEqual(metadata["wal_preallocated_tail_bytes"], 112)

    def test_partial_current_generation_or_arbitrary_nonzero_tail_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain, wal = root / "plain.db", root / "source-wal"
            plain.write_bytes(self.plain)
            prefix = _wal([(1, 1, self.encrypted)])
            current_partial = _wal([(1, 1, self.encrypted)] * 2)[len(prefix):len(prefix) + 112]
            for tail in (current_partial, b"A" * 112):
                wal.write_bytes(prefix + tail)
                with self.assertRaisesRegex(ExportError, "部分活动帧"):
                    backend._merge_authenticated_wal(plain, wal, self.key,
                                                    backend._mac_key(self.key, self.encrypted))

    def test_old_generation_must_still_authenticate_and_current_frames_cannot_follow_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            plain, wal = root / "plain.db", root / "source-wal"
            plain.write_bytes(self.plain)
            old_salt, current_salt = struct.pack(">II", 27, 100), struct.pack(">II", 28, 200)
            old_frame = _wal([(1, 1, self.encrypted)], old_salt)[32:]
            current_frame = _wal([(1, 1, self.encrypted)], current_salt)[32:]
            wal.write_bytes(_wal([], current_salt) + old_frame + current_frame)
            with self.assertRaisesRegex(ExportError, "又出现当前世代"):
                backend._merge_authenticated_wal(plain, wal, self.key,
                                                backend._mac_key(self.key, self.encrypted))
            corrupted = bytearray(old_frame)
            corrupted[200] ^= 1
            wal.write_bytes(_wal([], current_salt) + corrupted)
            with self.assertRaisesRegex(ExportError, "HMAC"):
                backend._merge_authenticated_wal(plain, wal, self.key,
                                                backend._mac_key(self.key, self.encrypted))

    def test_non_initial_page_authentication_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "encrypted.db"
            second = bytearray(_encrypted_page(self.key, self.salt, bytes(4096), 2))
            second[20] ^= 1
            source.write_bytes(self.encrypted + second)
            with self.assertRaisesRegex(ExportError, "第 2 页 HMAC"):
                backend._snapshot_database(source, self.key, root / "scratch" / "plain.db")


if __name__ == "__main__":
    unittest.main()
