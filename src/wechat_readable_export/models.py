"""Stable, serializable types shared by the reader, parsers, and renderers."""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

CHINA_TIME = timezone(timedelta(hours=8), name="Asia/Shanghai")


class ExportError(Exception):
    """An actionable failure which can be displayed without a traceback."""


@dataclass(frozen=True)
class Contact:
    username: str
    nickname: str = ""
    remark: str = ""

    @property
    def display_name(self) -> str:
        return self.remark or self.nickname or self.username


@dataclass
class MediaAttachment:
    """Optional embedded raster media, independent of the original account files."""

    kind: str
    status: str = "missing"
    mime_type: str | None = None
    data_base64: str | None = None
    reason: str | None = None


@dataclass
class Message:
    timestamp: int | float
    time: str
    sender: str
    sender_username: str | None
    sender_status: str
    is_self: bool | None
    type: str
    type_code: int
    content: str
    source_db: str = ""
    local_id: int | None = None
    server_id: int | str | None = None
    sort_seq: int = 0
    transcript: str | None = None
    media: MediaAttachment | None = None


@dataclass
class Conversation:
    contact: Contact
    messages: list[Message]
    exported_at: str = field(default_factory=lambda: datetime.now(CHINA_TIME).isoformat(timespec="seconds"))
    warnings: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "chat_name": self.contact.display_name,
            "chat_type": "group" if self.contact.username.endswith("@chatroom") else "private",
            "contact": asdict(self.contact),
            "timezone": "Asia/Shanghai",
            "exported_at": self.exported_at,
            "message_count": len(self.messages),
            "first_time": self.messages[0].time if self.messages else None,
            "last_time": self.messages[-1].time if self.messages else None,
            "type_counts": dict(Counter(m.type for m in self.messages)),
            "sender_resolution_counts": dict(Counter(m.sender_status for m in self.messages)),
            "warnings": list(self.warnings),
            "metadata": dict(self.metadata),
            "messages": [asdict(m) for m in self.messages],
        }
