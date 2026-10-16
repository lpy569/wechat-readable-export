"""Fictional examples, safe to commit and share."""

from datetime import datetime

from .models import CHINA_TIME, Contact, Conversation, Message


def demo_conversation() -> Conversation:
    contact = Contact("demo_friend", "林同学", "林同学")
    samples = [
        ("2026-01-10 09:00:00", False, 1, "周末一起去图书馆吗？"),
        ("2026-01-10 09:01:15", True, 1, "好呀，上午十点见。"),
        ("2026-01-10 09:02:04", False, 3, "[图片]"),
        ("2026-01-10 09:03:10", True, 49, "[链接] 周末阅读清单"),
        ("2026-01-10 10:00:00", False, 34, "[语音]"),
        ("2026-01-11 14:30:12", True, 1, "这个版本保留了原文：<script>alert('hello')</script>"),
        ("2026-01-11 14:31:00", False, 1, "文字只作为文字显示，不会执行。🔎"),
        ("2026-01-11 15:10:00", False, 49, "带上这本书吧。\n    ↳ 引用 我：周末阅读清单"),
        ("2026-01-12 08:05:00", True, 47, "[动画表情]"),
        ("2026-01-12 08:06:00", False, 1, "下次见！\n这是一条多行消息。"),
    ]
    types = {1: "文本", 3: "图片", 34: "语音", 47: "动画表情", 49: "文件/链接/卡片"}
    messages = []
    for i, (time, mine, code, content) in enumerate(samples, 1):
        messages.append(Message(
            timestamp=int(datetime.strptime(time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA_TIME).timestamp()),
            time=time, sender="我" if mine else contact.display_name,
            sender_username="demo_self" if mine else contact.username,
            sender_status="resolved", is_self=mine, type=types[code], type_code=code,
            content=content, source_db="demo", local_id=i, server_id=f"demo-{i}", sort_seq=i,
        ))
    return Conversation(contact, messages, metadata={"demo": True, "note": "全部内容为虚构演示数据。"})
