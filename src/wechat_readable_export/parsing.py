"""Turn shard-resolved rows into readable messages without guessing identities.

This module operates on supplied rows only. It never opens WeChat files or
fetches media. XML is interpreted as data; ordinary text remains literal.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
import html
import io
import json
import math
import re
from typing import Any
import xml.etree.ElementTree as ET

from .models import CHINA_TIME, Contact, Conversation, ExportError, Message

try:
    import zstandard
except ImportError:  # Also gives an actionable warning in a minimal environment.
    zstandard = None


ZSTD_MAGIC = b"\x28\xb5\x2f\xfd"
TYPE_NAMES = {
    1: "文本", 3: "图片", 34: "语音", 43: "视频", 62: "视频",
    47: "动画表情", 48: "位置", 49: "文件/链接/卡片", 50: "通话",
    10000: "系统消息", 10002: "系统消息",
}
MEDIA_MARKERS = {3: "[图片]", 34: "[语音]", 43: "[视频]", 62: "[视频]", 47: "[动画表情]"}


def _decode(value: Any, issues: set[str], field: str) -> str:
    if value is None or value == "" or value == b"":
        return ""
    if isinstance(value, str):
        text = value
    elif isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        if data.startswith(ZSTD_MAGIC):
            if zstandard is None:
                issues.add(f"{field} 为 Zstandard 压缩内容，但未安装 zstandard")
                return ""
            try:
                # A stream reader also handles frames without a declared size.
                with zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data)) as stream:
                    data = stream.read(64 * 1024 * 1024 + 1)
                if len(data) > 64 * 1024 * 1024:
                    issues.add(f"{field} 解压后超过 64 MiB，未解析")
                    return ""
            except Exception:
                issues.add(f"{field} 的 Zstandard 解压失败")
                return ""
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            issues.add(f"{field} 不是有效 UTF-8，损坏字节已用替代符保留")
            text = data.decode("utf-8", errors="replace")
    else:
        issues.add(f"{field} 数据类型不受支持，已保留其文字表示")
        try:
            text = json.dumps(value, ensure_ascii=False)
        except (TypeError, ValueError):
            text = str(value)
    if "\x00" in text:
        issues.add(f"{field} 含 NUL，已移除 NUL 字符")
        text = text.replace("\x00", "")
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _xml(text: str, issues: set[str]) -> ET.Element | None:
    if re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", text, re.I):
        issues.add("XML 含文档类型或实体声明，已按原文保留")
        return None
    try:
        return ET.fromstring(text.strip())
    except (ET.ParseError, ValueError):
        issues.add("XML 解析失败，已按原文保留")
        return None


def _text(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path)
    return "" if found is None else "".join(found.itertext()).strip()


def _node(root: ET.Element, tag: str) -> ET.Element | None:
    return root if root.tag == tag else root.find(f".//{tag}")


def _joined(*parts: str) -> str:
    return "\n".join(dict.fromkeys(part for part in parts if part))


def _readable_leaves(root: ET.Element) -> str:
    return _joined(*(item.text.strip() for item in root.iter() if len(item) == 0 and item.text and item.text.strip()))


def _app_message(text: str, issues: set[str], depth: int = 0) -> str:
    root = _xml(text, issues)
    if root is None:
        return "[文件/链接/卡片：原文未解析]\n" + text
    app = _node(root, "appmsg")
    if app is None:
        issues.add("卡片缺少 appmsg，已按原文保留")
        return "[文件/链接/卡片：原文未解析]\n" + text
    kind = _text(app, "type")
    title, desc, url = (_text(app, path) for path in ("title", "des", "url"))
    if kind == "57":
        refer = app.find(".//refermsg")
        original = _text(refer, "content")
        original_type = _text(refer, "type")
        if not original:
            issues.add("引用回复缺少本地原引用文字")
            original = "[原引用内容未保存在本机]"
        elif original_type == "49" and original.lstrip().startswith("<") and depth < 2:
            original = _app_message(original, issues, depth + 1)
        elif original_type and original_type != "1" and original.lstrip().startswith("<"):
            # Preserve unfamiliar quoted payloads instead of silently discarding them.
            original = f"[被引用消息 type={original_type}]\n{original}"
        name = _text(refer, "displayname") or "发送人未记录"
        return (title or desc or "[引用回复]") + f"\n    ↳ 引用 {name}：{original}"
    if kind == "6":
        return _joined("[文件] " + (title or "名称未记录"), desc)
    if kind == "19":
        record = _text(app, "recorditem")
        lines = ["[合并聊天记录]", title, desc]
        if record:
            nested = _xml(record, issues)
            if nested is None:
                lines.append(record)
            else:
                for item in nested.findall(".//dataitem"):
                    body = _text(item, "datadesc") or _text(item, "datatitle")
                    if not body:
                        body = f"[合并记录中的消息 type={item.get('datatype', '未知')}]"
                    lines.append(" | ".join(filter(None, (_text(item, "sourcetime"), _text(item, "sourcename"), body))))
        return _joined(*lines)
    if kind in {"33", "36"}:
        return _joined("[小程序]", title, desc, url)
    if kind in {"5", "4"} or (url and kind not in {"51", "2000", "2001"}):
        return _joined("[链接]", title, desc, url)
    if kind == "51":
        finder = app.find(".//finderFeed")
        if finder is not None:
            return _joined("[视频号]", _text(finder, "nickname"), _text(finder, "desc"), title, desc)
        return _joined("[视频号分享]", title, desc)
    if kind in {"2000", "2001"}:
        label = "[转账]" if kind == "2000" else "[红包]"
        return _joined(label, title, desc, _text(app, ".//feedesc"), _text(app, ".//pay_memo"))
    issues.add(f"未识别的卡片类型 {kind or '空'}，已保留原文")
    return _joined(f"[卡片 type={kind or '未知'}]", text)


def _content(kind: int, text: str, issues: set[str]) -> str:
    if kind == 1:
        if not text.strip():
            issues.add("文本消息缺少可恢复正文")
            return "[文本正文未保存在本机或解码失败]"
        # Never HTML-unescape or interpret text messages as XML/HTML.
        return text
    if kind in MEDIA_MARKERS:
        return MEDIA_MARKERS[kind]
    if kind == 49:
        if not text.strip():
            issues.add("卡片消息缺少可恢复正文")
            return "[文件/链接/卡片：正文未保存在本机或解码失败]"
        return _app_message(text, issues)
    if kind in {10000, 10002}:
        if not text.strip():
            issues.add("系统消息缺少可恢复正文")
            return "[系统消息：正文未保存在本机]"
        if text.lstrip().startswith("<"):
            root = _xml(text, issues)
            if root is not None:
                revoked = root.find(".//revokemsg")
                if root.tag == "revokemsg":
                    revoked = root
                if revoked is not None:
                    body = _text(revoked, "replacemsg") or _text(revoked, "content")
                    return "[撤回] " + (body or "撤回了一条消息；原内容未保存在此提示中")
                body = _text(root, ".//content") or _text(root, ".//title") or _text(root, ".//tips")
                if body:
                    return "[系统] " + body
                issues.add("未识别的系统 XML，已保留原文")
        return "[系统] " + text
    if kind == 48:
        if text.lstrip().startswith("<"):
            root = _xml(text, issues)
            location = _node(root, "location") if root is not None else None
            if location is not None:
                coordinates = ", ".join(filter(None, (location.get("x"), location.get("y"))))
                return _joined("[位置]", location.get("poiname", ""), location.get("label", ""), coordinates)
        if not text.strip():
            issues.add("位置消息缺少本地描述")
        return _joined("[位置：原文未解析]", text)
    if kind == 50:
        if text.lstrip().startswith("<"):
            root = _xml(text, issues)
            if root is not None:
                body = _readable_leaves(root)
                return _joined("[通话]", body or text)
        return _joined("[通话]", text)
    issues.add(f"未识别的消息类型 {kind}，已保留可读原文")
    return _joined(f"[未知消息 type={kind}]", text or "[无可恢复文字]")


def _voice_transcript(row: dict, issues: set[str]) -> str | None:
    for field in ("message_content", "compress_content", "source", "packed_info_data"):
        value = row.get(field)
        if value is None:
            continue
        # Binary protobuf fields naturally contain non-UTF-8. Examine only those
        # containing a transcript marker; do not report unrelated media metadata.
        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            if not raw.startswith(ZSTD_MAGIC) and not re.search(rb"voicetrans|transtext|voice[_-]?trans", raw, re.I):
                continue
        text = _decode(value, issues, field)
        if not re.search(r"voicetrans|transtext|voice[_-]?trans", text, re.I):
            continue
        for candidate in dict.fromkeys((text, html.unescape(text))):
            if candidate.lstrip().startswith("<") and not re.search(r"<!\s*(?:DOCTYPE|ENTITY)\b", candidate, re.I):
                try:
                    root = ET.fromstring(candidate.strip())
                except (ET.ParseError, ValueError):
                    pass
                else:
                    for item in root.iter():
                        if item.tag.lower() == "voicetrans":
                            result = item.get("transtext") or item.get("text") or item.text
                            if result and result.strip():
                                return result.strip()
            # JSON strings are decoded by JSON only. Chinese characters are never
            # passed through unicode_escape, which would corrupt UTF-8 text.
            found = re.search(r'"(?:transtext|voice[_-]?trans(?:cript|text))"\s*:\s*("(?:\\.|[^"\\])*")', candidate, re.I)
            if found:
                try:
                    result = json.loads(found.group(1))
                except (ValueError, TypeError):
                    issues.add("原生语音转文字的 JSON 转义无效")
                else:
                    if result.strip():
                        return result.strip()
            found = re.search(r"(?:transtext|voice[_-]?trans(?:cript|text))\s*=\s*(['\"])(.*?)\1", candidate, re.I | re.S)
            if found and found.group(2).strip():
                return html.unescape(found.group(2)).strip()
        issues.add("检测到原生语音转文字标记，但未能恢复文字")
    return None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[+-]?\d+", value.strip()):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _timestamp(value: Any, context: str) -> int | float:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError
        number = float(value)
        if not math.isfinite(number) or number <= 0:
            raise ValueError
        if number >= 100_000_000_000:
            number /= 1000
        datetime.fromtimestamp(number, CHINA_TIME)
    except (ValueError, TypeError, OverflowError, OSError):
        raise ExportError(f"{context} 的 create_time 无效，无法可靠排序：{value!r}") from None
    return int(number) if number.is_integer() else number


def _bound(value: datetime | None) -> float | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ExportError("日期筛选必须使用 datetime。")
    return (value.replace(tzinfo=CHINA_TIME) if value.tzinfo is None else value).timestamp()


def _sender(row: dict, contact: Contact, self_username: str, kind: int, issues: set[str]):
    if kind in {10000, 10002}:
        return "系统消息", None, "system", None
    username = row.get("_sender_username")
    status = row.get("_sender_status") or "unmapped_sender_id"
    if not isinstance(username, str) or not username or any(c.isspace() or c == "\x00" for c in username):
        username = None
        status = status if status != "resolved" else "invalid_sender_username"
    if status != "resolved" or username is None:
        issues.add(f"发送者未能确认（{status}），未根据数字 ID 或正文猜测")
        return "未知发送者", username, str(status), None
    if not self_username:
        issues.add("本机账号 username 未确认，无法可靠判断发送人")
        return "未知发送者", username, "self_unknown", None
    is_self = username == self_username
    if is_self:
        return "我", username, "resolved", True
    if not contact.username.endswith("@chatroom"):
        if username != contact.username:
            issues.add("私聊消息映射到双方以外的账号，发送者需复查")
            return "未知发送者", username, "unexpected_private_sender", False
        return contact.display_name, username, "resolved", False
    return username, username, "resolved", False


def _message_id(value: Any) -> int | str | None:
    if value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "hex:" + bytes(value).hex()
    return str(value)


def build_conversation(
    contact: Contact,
    self_username: str,
    rows: list[dict],
    *,
    start: datetime | None = None,
    end: datetime | None = None,
    metadata: dict | None = None,
) -> Conversation:
    """Parse all selected rows, preserving duplicates and reporting uncertainty."""
    lower, upper = _bound(start), _bound(end)
    if lower is not None and upper is not None and lower >= upper:
        raise ExportError("开始时间必须早于结束时间；结束时间不包含在筛选区间内。")
    messages: list[Message] = []
    warning_counts: Counter[str] = Counter()
    source_counts: Counter[str] = Counter()
    excluded = 0
    for index, row in enumerate(rows, 1):
        source = str(row.get("_db_rel") or row.get("source_db") or "")
        context = f"第 {index} 行（来源 {source or '未记录'}，local_id={row.get('local_id')!r}）"
        timestamp = _timestamp(row.get("create_time"), context)
        if (lower is not None and timestamp < lower) or (upper is not None and timestamp >= upper):
            excluded += 1
            continue
        issues: set[str] = set()
        raw_type = _integer(row.get("local_type"))
        if raw_type is None or raw_type < 0:
            issues.add("local_type 无效，按未知消息保留")
            raw_type = -1
        kind = raw_type & 0xFFFFFFFF if raw_type > 0xFFFFFFFF else raw_type
        sender, sender_username, sender_status, is_self = _sender(row, contact, self_username, kind, issues)
        text = _decode(row.get("message_content"), issues, "message_content")
        if not text.strip() or any(item.startswith("message_content 不是有效 UTF-8") for item in issues):
            fallback = _decode(row.get("compress_content"), issues, "compress_content")
            if fallback.strip():
                text = fallback
        if contact.username.endswith("@chatroom") and sender_status == "resolved" and sender_username:
            prefix = re.match(r"^([^:\r\n]+):\s*", text)
            if prefix and prefix.group(1) == sender_username:
                text = text[prefix.end():]
        content = _content(kind, text, issues)
        transcript = _voice_transcript(row, issues) if kind == 34 else None
        moment = datetime.fromtimestamp(timestamp, CHINA_TIME)
        formatted = moment.strftime("%Y-%m-%d %H:%M:%S")
        if moment.microsecond:
            formatted += f".{moment.microsecond:06d}".rstrip("0")
        messages.append(Message(
            timestamp=timestamp, time=formatted, sender=sender,
            sender_username=sender_username, sender_status=sender_status,
            is_self=is_self, type=TYPE_NAMES.get(kind, "未知消息"),
            type_code=raw_type, content=content, source_db=source,
            local_id=_integer(row.get("local_id")),
            server_id=_message_id(row.get("server_id")),
            sort_seq=_integer(row.get("sort_seq")) or 0,
            transcript=transcript,
        ))
        warning_counts.update(issues)
        source_counts[source or "未记录"] += 1
    messages.sort(key=lambda item: (item.timestamp, item.sort_seq, item.source_db, item.local_id or 0))
    warnings = [f"{description}：{count} 条。" for description, count in sorted(warning_counts.items())]
    server_counts = Counter(str(m.server_id) for m in messages if m.server_id not in (None, "", 0, "0"))
    repeated = {key: count for key, count in server_counts.items() if count > 1}
    if repeated:
        warnings.append(f"发现重复 server_id {len(repeated)} 组、额外 {sum(count - 1 for count in repeated.values())} 条；全部消息均已保留。")
    local_counts = Counter((m.source_db, m.local_id) for m in messages if m.local_id is not None)
    local_repeated = sum(count - 1 for count in local_counts.values() if count > 1)
    if local_repeated:
        warnings.append(f"同一来源存在重复 local_id，额外 {local_repeated} 条；全部消息均已保留。")
    info = dict(metadata or {})
    info["parser_statistics"] = {
        "input_row_count": len(rows), "filtered_out_count": excluded,
        "source_message_counts": dict(source_counts),
        "native_voice_transcript_count": sum(bool(m.transcript) for m in messages),
        "duplicate_server_id_groups": len(repeated),
    }
    return Conversation(contact=contact, messages=messages, warnings=warnings, metadata=info)
