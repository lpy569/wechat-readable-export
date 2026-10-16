"""Read-only Windows Weixin reader, with authenticated disposable snapshots.

The only process rights used by the vendored scanner are VM_READ and
QUERY_INFORMATION. No injection, UI automation, key files, account switching,
network calls, or changes to Weixin's files are part of this backend.
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import os
from pathlib import Path
import platform
import re
import shutil
import sqlite3
import struct
import subprocess
import tempfile
import time
from contextlib import contextmanager
from typing import Callable, Iterator

from .models import Contact, ExportError

PAGE_SIZE = 4096
RESERVE_SIZE = 80
WAL_HEADER_SIZE = 32
WAL_FRAME_SIZE = 24 + PAGE_SIZE
MESSAGE_FILE = re.compile(r"message_([0-9]+)\.db\Z")
WANTED_COLUMNS = (
    "local_id", "local_type", "server_id", "real_sender_id", "create_time",
    "message_content", "source", "compress_content", "packed_info_data", "sort_seq",
)


def _default_roots() -> list[Path]:
    roots = []
    profile = os.environ.get("USERPROFILE")
    if profile:
        roots.extend((Path(profile) / "Documents" / "xwechat_files",
                      Path(profile) / "Documents" / "WeChat Files"))
    # Documents may be redirected to OneDrive. Never crawl drives or other users.
    onedrive = os.environ.get("OneDrive")
    if onedrive:
        roots.append(Path(onedrive) / "Documents" / "xwechat_files")
    return roots


def _account_paths(data_dir: str | Path | None) -> list[Path]:
    roots = [Path(data_dir).expanduser()] if data_dir is not None else _default_roots()
    found: dict[str, Path] = {}
    for candidate in roots:
        candidate = candidate.resolve()
        if not candidate.exists():
            if data_dir is not None:
                raise ExportError(f"微信数据目录不存在：{candidate}")
            continue
        if not candidate.is_dir():
            raise ExportError(f"微信数据路径不是目录：{candidate}")
        if candidate.name == "db_storage":
            candidate = candidate.parent
        if (candidate / "db_storage").is_dir():
            choices = [candidate]
        else:
            try:
                choices = [p for p in candidate.iterdir()
                           if p.is_dir() and (p / "db_storage").is_dir()]
            except OSError as exc:
                raise ExportError(f"无法读取微信数据目录：{candidate}") from exc
        for account in choices:
            resolved = account.resolve()
            found[str(resolved).casefold()] = resolved
    return sorted(found.values(), key=lambda p: (p.name.casefold(), str(p)))


def list_accounts(data_dir: str | Path | None = None) -> list[dict]:
    """List immediate account directories; never inspect their chat contents."""
    return [{"account": p.name, "path": str(p)} for p in _account_paths(data_dir)]


def _select_account(data_dir: str | Path | None, account: str | None) -> Path:
    candidates = _account_paths(data_dir)
    if not candidates:
        raise ExportError("未找到微信 4.x 数据目录，请用 --data-dir 指定 xwechat_files 或账号目录。")
    if account is not None:
        # Match only an enumerated folder, not a path that could escape the root.
        matches = [p for p in candidates if p.name == account]
        if len(matches) != 1:
            raise ExportError("--account 必须是 accounts 中列出的完整账号目录名。")
        return matches[0]
    if len(candidates) != 1:
        raise ExportError("发现多个微信账号，请先运行 accounts，并用 --account 明确选择。")
    return candidates[0]


def _weixin_pids() -> list[int]:
    if os.name != "nt":
        return []
    try:
        result = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Weixin.exe", "/FO", "CSV", "/NH"],
            capture_output=True, check=True, timeout=15,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ExportError("无法只读枚举 Weixin.exe 进程，请确认 Windows tasklist 可用。") from exc
    # Names and PIDs are ASCII. Other localized columns are irrelevant.
    rows = csv.reader(io.StringIO(result.stdout.decode("utf-8", "replace")))
    return sorted({int(r[1]) for r in rows
                   if len(r) >= 2 and r[0].casefold() == "weixin.exe" and r[1].isdigit()})


def doctor(data_dir: str | Path | None = None) -> dict:
    return {"windows": os.name == "nt", "version": platform.version(),
            "python_bits": struct.calcsize("P") * 8,
            "pids": _weixin_pids(), "accounts": list_accounts(data_dir),
            "backend": "read-only Config.Cipher; authenticated local snapshots",
            "client_version_verified": "4.1.15.13 upstream; other builds require successful HMAC validation"}


def _crypto():
    try:
        from ._vendor import db_readonly
    except ImportError as exc:
        raise ExportError("缺少 cryptography，请安装本项目声明的依赖。") from exc
    return db_readonly


def _signature(path: Path) -> tuple[int, int] | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return stat.st_size, stat.st_mtime_ns


def _source_state(source: Path) -> tuple:
    return _signature(source), _signature(Path(str(source) + "-wal"))


def _mac_key(key: bytes, page1: bytes) -> bytes:
    salt = key[32:] if len(key) == 48 else page1[:16]
    return hashlib.pbkdf2_hmac("sha512", key[:32], bytes(b ^ 0x3A for b in salt), 2, dklen=32)


def _authenticated_page(key: bytes, mac_key: bytes, page: bytes, pgno: int) -> bytes:
    if len(page) != PAGE_SIZE or not 1 <= pgno <= 0x7FFFFFFF:
        raise ExportError("数据库或 WAL 包含不完整或无效的页。")
    start = 16 if pgno == 1 else 0
    expected = hmac.new(mac_key, page[start:PAGE_SIZE - 64] + struct.pack("<I", pgno),
                        hashlib.sha512).digest()
    if not hmac.compare_digest(expected, page[-64:]):
        raise ExportError(f"数据库或 WAL 第 {pgno} 页 HMAC 校验失败；已停止，未导出不完整记录。")
    plain = _crypto()._decrypt_page(key, page, pgno)
    if pgno == 1 and plain[:16] != b"SQLite format 3\x00":
        raise ExportError("解密后的数据库头不受支持；已停止。")
    return plain


def _wal_checksum(data: bytes, byteorder: str, s1: int = 0, s2: int = 0) -> tuple[int, int]:
    if len(data) % 8:
        raise ExportError("WAL 校验块长度不正确。")
    for a, b in struct.iter_unpack(byteorder + "II", data):
        s1 = (s1 + a + s2) & 0xFFFFFFFF
        s2 = (s2 + b + s1) & 0xFFFFFFFF
    return s1, s2


def _validate_preallocated_tail(wal, start: int, size: int, salt: bytes,
                               key: bytes, mac_key: bytes, byteorder: str) -> dict:
    """Prove that unused capacity is zero or authenticated older generations.

    SQLite increments salt-1 when restarting a WAL after checkpoint. The rest
    of a preallocated file may retain old full frames and a partial old frame.
    A partial nonzero frame is accepted ONLY when its complete frame header
    matches an older generation already established by an authenticated full
    frame. A partial current-generation frame is always an error.
    """
    current_generation = struct.unpack(">I", salt[:4])[0]
    proven_old_salts: set[bytes] = set()
    stale = zero = partial = 0
    previous_salt = None
    previous_checksum = None
    wal.seek(start)
    while wal.tell() < size:
        frame = wal.read(min(WAL_FRAME_SIZE, size - wal.tell()))
        if len(frame) < WAL_FRAME_SIZE:
            if not any(frame):
                partial = len(frame)
                break
            if len(frame) >= 24 and frame[8:16] in proven_old_salts:
                pgno, database_size = struct.unpack(">II", frame[:8])
                if 0 < pgno <= 0x7FFFFFFF and database_size <= 0x7FFFFFFF:
                    partial = len(frame)
                    break
            raise ExportError("WAL 尾部含无法证明为预分配空间的部分活动帧；请稍后重试。")
        if not any(frame):
            zero += 1
            previous_salt = previous_checksum = None
            continue
        frame_salt = frame[8:16]
        if frame_salt == salt:
            raise ExportError("WAL 旧世代或预分配尾部之后又出现当前世代活动帧，文件不完整。")
        generation = struct.unpack(">I", frame_salt[:4])[0]
        age = (current_generation - generation) & 0xFFFFFFFF
        if not 0 < age < 0x80000000:
            raise ExportError("WAL 尾部不是可确认的旧世代，已停止。")
        pgno, database_size = struct.unpack(">II", frame[:8])
        if not 0 < pgno <= 0x7FFFFFFF or database_size > 0x7FFFFFFF:
            raise ExportError("WAL 旧世代帧头无效，已停止。")
        _authenticated_page(key, mac_key, frame[24:], pgno)
        stored_checksum = struct.unpack(">II", frame[16:24])
        # The old header checksum is unavailable, but consecutive old frames
        # from the same generation still have a verifiable checksum chain.
        if frame_salt == previous_salt:
            calculated = _wal_checksum(frame[:8] + frame[24:], byteorder, *previous_checksum)
            if calculated != stored_checksum:
                raise ExportError("WAL 旧世代帧校验链损坏，无法证明尾部是预分配空间。")
        proven_old_salts.add(frame_salt)
        previous_salt, previous_checksum = frame_salt, stored_checksum
        stale += 1
    return {"wal_stale_frames_ignored": stale, "wal_zero_frames_ignored": zero,
            "wal_preallocated_tail_bytes": partial}


def _merge_authenticated_wal(plain_path: Path, wal_path: Path, key: bytes, mac_key: bytes) -> dict:
    """Apply only the last complete committed WAL prefix, never a stale fallback.

    Complete, valid uncommitted tail frames are intentionally excluded. A partial
    active frame, bad checksum, bad page HMAC, or invalid commit size fails.
    Authenticated old generations and zero preallocated capacity are excluded.
    """
    size = wal_path.stat().st_size
    info = {"wal_frames": 0, "wal_committed_frames": 0,
            "wal_uncommitted_frames_ignored": 0, "wal_stale_frames_ignored": 0,
            "wal_zero_frames_ignored": 0, "wal_preallocated_tail_bytes": 0}
    if size == 0:
        return info
    if size < WAL_HEADER_SIZE:
        raise ExportError("WAL 文件不完整；请稍后重试，未回落到旧数据库。")
    with wal_path.open("rb") as wal:
        header = wal.read(WAL_HEADER_SIZE)
        magic, version, page_size = struct.unpack(">III", header[:12])
        if magic not in (0x377F0682, 0x377F0683) or version != 3007000 or page_size != PAGE_SIZE:
            raise ExportError("WAL 格式不受支持；已停止，未忽略近期消息。")
        byteorder = "<" if magic == 0x377F0682 else ">"
        checksum = _wal_checksum(header[:24], byteorder)
        if checksum != struct.unpack(">II", header[24:32]):
            raise ExportError("WAL 文件头校验失败。")
        salt = header[16:24]
        frame_count = (size - WAL_HEADER_SIZE) // WAL_FRAME_SIZE
        info["wal_frames"] = frame_count
        frames: list[tuple[int, int, int]] = []
        last_commit = -1
        committed_size = 0
        unused_tail_start = WAL_HEADER_SIZE + frame_count * WAL_FRAME_SIZE
        for index in range(frame_count):
            frame_header = wal.read(24)
            page = wal.read(PAGE_SIZE)
            if frame_header[8:16] != salt:
                unused_tail_start = WAL_HEADER_SIZE + index * WAL_FRAME_SIZE
                break
            pgno, database_size = struct.unpack(">II", frame_header[:8])
            checksum = _wal_checksum(frame_header[:8] + page, byteorder, *checksum)
            if checksum != struct.unpack(">II", frame_header[16:24]):
                raise ExportError("WAL 帧校验失败；请稍后重试，未回落到旧快照。")
            _authenticated_page(key, mac_key, page, pgno)
            frames.append((index, pgno, database_size))
            if database_size:
                if database_size > 0x7FFFFFFF or pgno > database_size:
                    raise ExportError("WAL 提交记录中的数据库大小无效。")
                last_commit = len(frames) - 1
                committed_size = database_size
        if unused_tail_start < size:
            info.update(_validate_preallocated_tail(
                wal, unused_tail_start, size, salt, key, mac_key, byteorder))
        committed = frames[:last_commit + 1]
        info["wal_committed_frames"] = len(committed)
        info["wal_uncommitted_frames_ignored"] = len(frames) - len(committed)
        if not committed:
            return info
        with plain_path.open("r+b") as target:
            for index, pgno, _ in committed:
                if pgno > committed_size:
                    # A later committed transaction may shrink the database.
                    continue
                wal.seek(WAL_HEADER_SIZE + index * WAL_FRAME_SIZE + 24)
                plain = _authenticated_page(key, mac_key, wal.read(PAGE_SIZE), pgno)
                target.seek((pgno - 1) * PAGE_SIZE)
                target.write(plain)
            target.truncate(committed_size * PAGE_SIZE)
            target.seek(28)
            target.write(struct.pack(">I", committed_size))
    return info


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
    connection.row_factory = sqlite3.Row
    connection.text_factory = lambda value: _decode_text(value)
    connection.execute("PRAGMA query_only=ON")
    return connection


def _decode_text(value: bytes):
    try:
        return value.decode("utf-8")
    except UnicodeDecodeError:
        return value


def _snapshot_database(source: Path, key: bytes, destination: Path) -> dict:
    if len(key) not in (32, 48):
        raise ExportError("数据库密钥格式不受支持。")
    destination.parent.mkdir(parents=True, exist_ok=True)
    encrypted = destination.with_suffix(".encrypted")
    wal_copy = destination.with_suffix(".wal-copy")
    stable_state = None
    for attempt in range(3):
        before = _source_state(source)
        if before[0] is None:
            raise ExportError(f"数据库已不存在：{source.name}")
        try:
            shutil.copyfile(source, encrypted)
            if before[1] is not None:
                shutil.copyfile(Path(str(source) + "-wal"), wal_copy)
            elif wal_copy.exists():
                wal_copy.unlink()
            after = _source_state(source)
        except (FileNotFoundError, PermissionError):
            after = None
        if before == after:
            stable_state = before
            break
        time.sleep(0.05)
    if stable_state is None:
        raise ExportError("微信数据库正持续写入，无法取得稳定快照；请稍后重试。")
    size = encrypted.stat().st_size
    if size < PAGE_SIZE or size % PAGE_SIZE:
        raise ExportError(f"数据库 {source.name} 页长度不完整。")
    with encrypted.open("rb") as reader:
        first = reader.read(PAGE_SIZE)
    if not _crypto()._verify_enc_key(key, first):
        raise ExportError(f"数据库 {source.name} 密钥 HMAC 校验失败；微信版本可能尚未兼容。")
    mac_key = _mac_key(key, first)
    with encrypted.open("rb") as reader, destination.open("wb") as writer:
        for pgno in range(1, size // PAGE_SIZE + 1):
            writer.write(_authenticated_page(key, mac_key, reader.read(PAGE_SIZE), pgno))
    wal_info = {"wal_frames": 0, "wal_committed_frames": 0,
                "wal_uncommitted_frames_ignored": 0, "wal_stale_frames_ignored": 0,
                "wal_zero_frames_ignored": 0, "wal_preallocated_tail_bytes": 0}
    if wal_copy.exists():
        wal_info = _merge_authenticated_wal(destination, wal_copy, key, mac_key)
    connection = _connect_readonly(destination)
    try:
        checks = connection.execute("PRAGMA quick_check").fetchall()
        if not checks or any(row[0] != "ok" for row in checks):
            raise ExportError(f"数据库 {source.name} 的解密快照完整性校验失败。")
    except sqlite3.DatabaseError as exc:
        raise ExportError(f"数据库 {source.name} 的解密快照不可读。") from exc
    finally:
        connection.close()
    encrypted.unlink()
    if wal_copy.exists():
        wal_copy.unlink()
    return {"source_db": source.name, "source_size": size,
            "snapshot": "last_complete_commit", "authentication": "all_pages_hmac_sha512",
            **wal_info}


def _contact_database(storage: Path) -> Path:
    for path in (storage / "contact" / "contact.db", storage / "contact.db"):
        if path.is_file():
            return path
    raise ExportError("所选账号缺少 contact/contact.db，无法可靠识别联系人。")


def _message_databases(storage: Path) -> list[Path]:
    folder = storage / "message"
    if not folder.is_dir():
        raise ExportError("所选账号缺少 message 目录。")
    return sorted((p for p in folder.iterdir() if p.is_file() and MESSAGE_FILE.fullmatch(p.name)),
                  key=lambda p: int(MESSAGE_FILE.fullmatch(p.name).group(1)))


def _read_contacts(path: Path, search: str = "") -> list[Contact]:
    connection = _connect_readonly(path)
    try:
        columns = {r[1] for r in connection.execute('PRAGMA table_info("contact")')}
        if not {"username", "nick_name", "remark"}.issubset(columns):
            raise ExportError("contact.db 的联系人表结构不受支持。")
        values = connection.execute("SELECT username, nick_name, remark FROM contact").fetchall()
    except sqlite3.DatabaseError as exc:
        raise ExportError("读取联系人库失败。") from exc
    finally:
        connection.close()
    wanted = search.casefold()
    result = [Contact(r[0], r[1] if isinstance(r[1], str) else "",
                      r[2] if isinstance(r[2], str) else "")
              for r in values if _valid_identity(r[0])]
    return sorted((c for c in result if not wanted or any(wanted in s.casefold()
                   for s in (c.username, c.nickname, c.remark))),
                  key=lambda c: (c.display_name.casefold(), c.username))


def _valid_identity(value) -> bool:
    return isinstance(value, str) and bool(value) and not any(c.isspace() or c == "\0" for c in value)


def _sender_id(value) -> int | None:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        value = value.lstrip("0") or "0"
        if len(value) > 19:
            return None
        value = int(value)
    return value if type(value) is int and 0 < value <= 0x7FFFFFFFFFFFFFFF else None


def _read_message_shards(shards: list[tuple[str, Path]], contact_id: str) -> list[dict]:
    table = "Msg_" + hashlib.md5(contact_id.encode("utf-8")).hexdigest()
    rows: list[dict] = []
    for relative, path in shards:
        connection = _connect_readonly(path)
        try:
            exists = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                                        (table,)).fetchone()
            if not exists:
                continue
            columns = {r[1] for r in connection.execute(f'PRAGMA table_info("{table}")')}
            if not {"local_id", "local_type", "create_time", "message_content"}.issubset(columns):
                raise ExportError(f"消息分片 {relative} 表结构不受支持，已停止。")
            name_columns = {str(r[1]).lower() for r in connection.execute('PRAGMA table_info("Name2Id")')}
            if "user_name" in name_columns:
                sender_index = dict(connection.execute('SELECT rowid, user_name FROM "Name2Id"'))
                map_status = "resolved"
            else:
                sender_index = {}
                map_status = "unsupported_name2id" if name_columns else "missing_name2id"
            selected = [c for c in WANTED_COLUMNS if c in columns]
            query = "SELECT " + ", ".join(f'"{c}"' for c in selected) + f' FROM "{table}"'
            for raw in connection.execute(query):
                item = {name: raw[name] if name in selected else None for name in WANTED_COLUMNS}
                sid = _sender_id(item["real_sender_id"])
                username = sender_index.get(sid)
                if sid is None:
                    status = "invalid_sender_id"
                elif map_status != "resolved":
                    status = map_status
                elif sid not in sender_index:
                    status = "unmapped_sender_id"
                elif not _valid_identity(username):
                    status = "invalid_sender_username"
                else:
                    status = "resolved"
                item.update(_db_rel=relative, _sender_username=username if status == "resolved" else None,
                            _sender_status=status)
                rows.append(item)
        except sqlite3.DatabaseError as exc:
            raise ExportError(f"读取消息分片 {relative} 失败；未跳过该分片。") from exc
        finally:
            connection.close()
    def number(value):
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0
    rows.sort(key=lambda r: (number(r["create_time"]), number(r["sort_seq"]),
                             r["_db_rel"], number(r["local_id"])))
    return rows


def _read_image_resource_hashes(path: Path, contact_id: str) -> dict[int, list[str]]:
    """Resource IDs are scoped to this chat; ambiguous hashes stay ambiguous."""
    from .local_media import extract_resource_hashes
    connection = _connect_readonly(path)
    try:
        chat_columns = {str(r[1]).lower() for r in connection.execute('PRAGMA table_info("ChatName2Id")')}
        info_columns = {str(r[1]).lower() for r in connection.execute('PRAGMA table_info("MessageResourceInfo")')}
        if "user_name" not in chat_columns or not {"chat_id", "message_local_id", "packed_info"}.issubset(info_columns):
            raise ExportError("图片资源数据库结构不受支持。")
        chats = connection.execute('SELECT rowid FROM "ChatName2Id" WHERE user_name=?', (contact_id,)).fetchall()
        if len(chats) != 1:
            return {}
        result: dict[int, list[str]] = {}
        for row in connection.execute('SELECT message_local_id, packed_info FROM "MessageResourceInfo" WHERE chat_id=?',
                                      (chats[0][0],)):
            local_id = _sender_id(row[0])
            if local_id is not None:
                hashes = extract_resource_hashes(row[1])
                result[local_id] = list(dict.fromkeys(result.get(local_id, []) + hashes))
        return result
    except sqlite3.DatabaseError as exc:
        raise ExportError("图片资源数据库无法读取。") from exc
    finally:
        connection.close()


class _LocalReader:
    def __init__(self, account_path: Path, workdir: Path, progress: Callable | None = None):
        self.account_path = account_path
        self.storage = account_path / "db_storage"
        self.workdir = workdir
        self.progress = progress or (lambda _message: None)
        self._keys: dict[str, bytes] = {}
        self._snapshots: dict[str, Path] = {}
        self._snapshot_metadata: dict[str, dict] = {}
        self._all_contacts: list[Contact] | None = None
        self._closed = False

    def _ensure_open(self):
        if self._closed:
            raise ExportError("本地读取会话已经关闭，请重新进入 LocalReader。")

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.storage).as_posix()

    def _ensure_keys(self, paths: list[Path]):
        self._ensure_open()
        missing = [p for p in paths if self._relative(p) not in self._keys]
        if not missing:
            return
        pids = _weixin_pids()
        if not pids:
            raise ExportError("未检测到 Weixin.exe，请先在本机登录微信 4.x。")
        crypto = _crypto()
        crypto.initialize_windows_api()
        # Intentionally bypass upstream initialization/cache/self-healing.
        scanner = object.__new__(crypto.WeChatDB)
        scanner._db_files = [(self._relative(p), str(p), p.stat().st_size) for p in missing]
        tested: set = set()
        extracted: dict[str, bytes] = {}
        self.progress(f"正在只读读取 {len(missing)} 个数据库的密钥…")
        try:
            for pid in pids:
                extracted.update(scanner._extract_keys_pid(pid, tested))
                if len(extracted) == len(missing):
                    break
            if len(extracted) != len(missing):
                raise ExportError("无法取得所选账号全部所需数据库的有效密钥。请确认该账号已登录、Python 为 64 位且与微信权限一致；此微信版本也可能尚未兼容。")
            self._keys.update(extracted)
        finally:
            extracted.clear()
            tested.clear()
            scanner._db_files.clear()

    def _snapshot(self, source: Path) -> Path:
        relative = self._relative(source)
        if relative not in self._snapshots:
            self._ensure_keys([source])
            destination = self.workdir / hashlib.sha256(relative.encode()).hexdigest() / "snapshot.db"
            self.progress(f"正在校验并读取 {relative}…")
            metadata = _snapshot_database(source, self._keys[relative], destination)
            self._snapshots[relative] = destination
            self._snapshot_metadata[relative] = metadata
        return self._snapshots[relative]

    def contacts(self, search: str = "") -> list[Contact]:
        self._ensure_open()
        if self._all_contacts is None:
            self._all_contacts = _read_contacts(self._snapshot(_contact_database(self.storage)))
        wanted = search.casefold()
        return [c for c in self._all_contacts if not wanted or any(wanted in v.casefold()
                for v in (c.username, c.nickname, c.remark))]

    def read_conversation(self, contact_id: str) -> tuple[Contact, str, list[dict], dict]:
        self._ensure_open()
        matches = [c for c in self.contacts() if c.username == contact_id]
        if len(matches) != 1:
            raise ExportError("联系人 ID 不在所选账号的 contact.db 中，请先搜索联系人并使用其精确 ID。")
        files = _message_databases(self.storage)
        if not files:
            raise ExportError("本地没有 message_N.db 消息分片。")
        self._ensure_keys(files)
        shards = [(self._relative(p), self._snapshot(p)) for p in files]
        if files != _message_databases(self.storage):
            raise ExportError("读取过程中新增或删除了消息分片，请重新导出以免遗漏。")
        raw = _read_message_shards(shards, contact_id)
        own_candidate = re.sub(r"_[A-Za-z0-9]{4}$", "", self.account_path.name)
        own = [c.username for c in self.contacts() if c.username == own_candidate]
        self_username = own[0] if len(own) == 1 else ""
        metadata = {"account": self.account_path.name, "message_shards_checked": len(files),
                    "message_shards": [rel for rel, _ in shards],
                    "sender_mapping": "per-shard Name2Id.rowid; unknown identities preserved",
                    "snapshot_policy": "authenticated committed prefix per database",
                    "snapshots": dict(self._snapshot_metadata),
                    "self_identity_confirmed": bool(self_username)}
        return matches[0], self_username, raw, metadata

    def attach_media(self, conversation, rows: list[dict], *, download_stickers: bool = False):
        """Optional media failures remain visible without losing valid messages."""
        self._ensure_open()
        from .local_media import LocalMediaResolver
        resource_hashes = {}
        resource_status = "not_needed"
        if any(message.type_code & 0xFFFFFFFF == 3 for message in conversation.messages):
            resource_status = "missing"
            for resource_path in (self.storage / "message" / "message_resource.db",
                                  self.storage / "message_resource.db"):
                if resource_path.is_file():
                    try:
                        resource_hashes = _read_image_resource_hashes(self._snapshot(resource_path),
                                                                     conversation.contact.username)
                        resource_status = "read"
                    except (ExportError, OSError):
                        resource_status = "unavailable"
                        conversation.warnings.append("图片资源数据库无法校验或读取，图片仅按明确的消息线索和本地缩略图查找。")
                    break
        def keys(samples):
            from .image_codec import find_config_image_key, find_image_key
            self.progress("正在只读查找图片解密材料…")
            try:
                pids = _weixin_pids()
                own = re.sub(r"_[A-Za-z0-9]{4}$", "", self.account_path.name)
                if (conversation.metadata.get("self_identity_confirmed")
                        and any(contact.username == own for contact in (self._all_contacts or []))):
                    try:
                        derived = find_config_image_key(pids, own, samples, progress=self.progress)
                    except (OSError, ValueError):
                        derived = (None, None)
                    if derived[0] is not None:
                        return derived
                return find_image_key(pids, samples, progress=self.progress)
            except (ExportError, OSError, ValueError):
                return None, None
        self.progress("正在读取图片和表情…" + ("（允许从微信 CDN 获取缺失表情）" if download_stickers else "（仅本地文件）"))
        resolver = LocalMediaResolver(self.account_path, conversation.contact.username,
                                      resource_hashes=resource_hashes, key_provider=keys,
                                      download_stickers=download_stickers)
        try:
            resolver.attach(conversation, rows)
        finally:
            resolver.close()
        conversation.metadata["media_resource_status"] = resource_status
        conversation.metadata["snapshots"] = dict(self._snapshot_metadata)

    def close(self):
        self._keys.clear()
        self._snapshots.clear()
        self._snapshot_metadata.clear()
        self._all_contacts = None
        self._closed = True


@contextmanager
def LocalReader(data_dir: str | Path | None = None, account: str | None = None,
                progress: Callable | None = None) -> Iterator[_LocalReader]:
    """Keep all key material in memory and snapshots in one disposable directory."""
    if os.name != "nt":
        raise ExportError("本地微信读取仅支持 Windows；示例和已导出文件可在其他系统阅读。")
    if struct.calcsize("P") != 8:
        raise ExportError("读取 64 位微信需要 64 位 Python。")
    selected = _select_account(data_dir, account)
    reader = None
    try:
        with tempfile.TemporaryDirectory(prefix="wechat-readable-export-") as temporary:
            reader = _LocalReader(selected, Path(temporary), progress)
            try:
                yield reader
            finally:
                reader.close()
    except OSError as exc:
        raise ExportError(f"本地读取或敏感临时目录清理失败：{exc}") from exc
