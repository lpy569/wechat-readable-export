"""Read selected voice blobs from authenticated, temporary SQLite snapshots.

WeChat 4.x storage layout reference: wechatauto, revision
91dc0e1a601013b759b261061d8f7642f736af90, Apache-2.0. Every media
shard has its own Name2Id rowids. A server id alone never identifies a chat.
This module neither opens encrypted account files nor accesses the network.
"""

from __future__ import annotations

from contextlib import closing
from pathlib import Path
import re
import sqlite3
import time
from typing import Iterable


MAX_VOICE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_VOICE_BYTES = 256 * 1024 * 1024
MAX_CONTACT_ROWS = 1024
MAX_MATCHED_ROWS = 100_000
SHARD_QUERY_SECONDS = 20
_DECIMAL = re.compile(r"-?[0-9]{1,20}\Z")


def canonical_server_id(value: object) -> str | None:
    """Return a nonzero signed-64 decimal id, accepting unsigned-64 spelling.

    Integer precision must survive JSON and SQLite round trips: floats, booleans,
    scientific notation, and out-of-range numbers are deliberately rejected.
    """
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    if isinstance(value, str):
        if not _DECIMAL.fullmatch(value):
            return None
        value = int(value)
    if not -(1 << 63) <= value < (1 << 64) or value == 0:
        return None
    if value >= 1 << 63:
        value -= 1 << 64
    return str(value)


def _query_spellings(server_id: str) -> tuple[int | str, ...]:
    number = int(server_id)
    # INTEGER and untyped/TEXT schemas must both work, without CAST coercing
    # unrelated malformed values into a selected message id.
    result: tuple[int | str, ...] = (number, server_id)
    if number < 0:
        result += (str(number + (1 << 64)),)
    return result


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in connection.execute(f'PRAGMA table_info("{table}")')}


def _stage_blob(staged: dict[str, bytes], staged_issues: dict[str, str],
                blobs: dict[str, bytes], issues: dict[str, str], server_id: str,
                storage: object, size: object, data: object, remaining: int) -> int:
    """Return the change in staged byte ownership, without copying duplicates."""
    if storage == "null" or (storage == "blob" and size == 0):
        return 0
    if storage != "blob" or not isinstance(size, int) or size < 0:
        staged_issues[server_id] = "invalid_blob"
    elif size > MAX_VOICE_BYTES:
        staged_issues[server_id] = "oversized_blob"
    elif not isinstance(data, bytes) or len(data) != size:
        staged_issues[server_id] = "invalid_blob"
    elif server_id not in issues and server_id not in staged_issues:
        previous = blobs.get(server_id, staged.get(server_id))
        if previous is not None and previous != data:
            staged_issues[server_id] = "ambiguous_blob"
        elif previous is None:
            if size > remaining:
                staged_issues[server_id] = "resource_limit"
            else:
                staged[server_id] = data
                return size
    if server_id in staged_issues:
        previous = staged.pop(server_id, None)
        return -len(previous) if previous is not None else 0
    return 0


def read_voice_blobs(
    shards: Iterable[tuple[str, Path]], contact_id: str, server_ids: Iterable[object],
) -> tuple[dict[str, bytes], dict]:
    """Return unambiguous blobs and JSON-safe diagnostics for one conversation.

    ``shards`` contains caller-authenticated *temporary* plain SQLite paths; its
    labels are unused in diagnostics to avoid leaking account paths. Keys in the
    result and ``issues`` are canonical_server_id values. Identical duplicates
    are accepted, conflicting blobs are withheld. Empty records may coexist
    with an otherwise valid blob. A malformed/oversized matching record is
    withheld even when another shard has a valid candidate.

    Retained source blobs across completed and in-progress shards are bounded
    to 256 MiB. At most one additional 8 MiB matching row is materialized while
    deciding whether to retain it. Equal duplicates never consume the budget
    twice. Excess ids receive resource_limit, preserving other valid blobs.

    Schema/read errors do not raise or discard successfully read shards. The
    caller must inspect shard counts before claiming all missing audio was
    absent locally. Reason codes are missing_session, missing_blob, empty_blob,
    invalid_blob, oversized_blob, ambiguous_blob, resource_limit, and
    unavailable_shards.
    """
    selected = sorted({value for item in server_ids
                       if (value := canonical_server_id(item)) is not None}, key=int)
    snapshots = list(shards)
    diagnostics = {
        "shards_requested": len(snapshots), "shards_read": 0,
        "shards_unsupported": 0, "shards_unreadable": 0, "known_shards": 0,
        "selected": len(selected), "available": 0, "source_bytes": 0,
        "resource_limited": 0, "issues": {}, "warnings": [],
    }
    if not selected:
        return {}, diagnostics
    if not isinstance(contact_id, str) or not contact_id:
        diagnostics["issues"] = {value: "missing_session" for value in selected}
        return {}, diagnostics

    selected_set = set(selected)
    blobs: dict[str, bytes] = {}
    issues: dict[str, str] = {}
    seen: set[str] = set()
    retained_bytes = 0
    for _, raw_path in snapshots:
        # Candidate records are staged per shard so an interrupted/corrupt
        # SELECT cannot leave a successful-looking partial shard behind.
        staged_blobs: dict[str, bytes] = {}
        staged_issues: dict[str, str] = {}
        staged_seen: set[str] = set()
        staged_bytes = 0
        matching_rows = 0
        found_contact = False
        try:
            path = Path(raw_path).resolve(strict=True)
            if not path.is_file():
                raise OSError("not a file")
            with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True)) as connection:
                connection.execute("PRAGMA query_only=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                deadline = time.monotonic() + SHARD_QUERY_SECONDS
                connection.set_progress_handler(lambda: int(time.monotonic() > deadline), 10_000)
                if (not {"user_name"} <= _columns(connection, "Name2Id")
                        or not {"chat_name_id", "svr_id", "voice_data"} <= _columns(connection, "VoiceInfo")):
                    diagnostics["shards_unsupported"] += 1
                    continue
                contact_rows = connection.execute(
                    "SELECT rowid FROM Name2Id WHERE user_name = ? COLLATE BINARY LIMIT ?",
                    (contact_id, MAX_CONTACT_ROWS + 1),
                ).fetchall()
                if len(contact_rows) > MAX_CONTACT_ROWS:
                    raise sqlite3.DatabaseError("too many contact mappings")
                found_contact = bool(contact_rows)
                for (chat_id,) in contact_rows:
                    for start in range(0, len(selected), 200):
                        spellings = [item for value in selected[start:start + 200]
                                     for item in _query_spellings(value)]
                        placeholders = ",".join("?" for _ in spellings)
                        # Never materialize an oversized BLOB. typeof also
                        # keeps arbitrary TEXT from being decoded as audio.
                        query = (
                            "SELECT svr_id, typeof(voice_data), length(voice_data), "
                            "CASE WHEN typeof(voice_data) = 'blob' AND length(voice_data) "
                            "BETWEEN 1 AND ? THEN voice_data ELSE NULL END "
                            f"FROM VoiceInfo WHERE chat_name_id = ? AND svr_id IN ({placeholders})"
                        )
                        for raw_id, storage, size, data in connection.execute(
                            query, (MAX_VOICE_BYTES, chat_id, *spellings),
                        ):
                            server_id = canonical_server_id(raw_id)
                            if server_id not in selected_set:
                                data = None
                                continue
                            matching_rows += 1
                            if matching_rows > MAX_MATCHED_ROWS:
                                raise sqlite3.DatabaseError("too many matching records")
                            staged_seen.add(server_id)
                            staged_bytes += _stage_blob(
                                staged_blobs, staged_issues, blobs, issues, server_id,
                                storage, size, data,
                                MAX_TOTAL_VOICE_BYTES - retained_bytes - staged_bytes,
                            )
                            # Drop a rejected/duplicate row before fetching the
                            # next one, rather than keeping two extra blobs.
                            data = None
                connection.set_progress_handler(None, 0)
        except (OSError, sqlite3.Error, ValueError, TypeError):
            data = None
            diagnostics["shards_unreadable"] += 1
            continue
        diagnostics["shards_read"] += 1
        diagnostics["known_shards"] += int(found_contact)
        seen.update(staged_seen)
        for server_id, reason in staged_issues.items():
            issues[server_id] = reason
            previous = blobs.pop(server_id, None)
            if previous is not None:
                retained_bytes -= len(previous)
            previous = None
        blobs.update(staged_blobs)
        retained_bytes += staged_bytes

    failed = diagnostics["shards_unsupported"] + diagnostics["shards_unreadable"]
    for server_id in selected:
        if server_id in blobs or server_id in issues:
            continue
        if failed:
            issues[server_id] = "unavailable_shards"
        elif server_id in seen:
            issues[server_id] = "empty_blob"
        elif not diagnostics["known_shards"]:
            issues[server_id] = "missing_session"
        else:
            issues[server_id] = "missing_blob"
    if failed:
        diagnostics["warnings"].append("部分语音数据库无法读取或结构尚不支持，缺失语音不能据此认定未保存在本机。")
    diagnostics["resource_limited"] = sum(reason == "resource_limit" for reason in issues.values())
    if diagnostics["resource_limited"]:
        diagnostics["warnings"].append("所选会话的语音源数据达到 256 MiB 总量限制；超出部分保留原因提示。")
    diagnostics["available"] = len(blobs)
    diagnostics["source_bytes"] = retained_bytes
    diagnostics["issues"] = issues
    return blobs, diagnostics
