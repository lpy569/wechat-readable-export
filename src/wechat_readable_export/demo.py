"""Fictional examples, safe to commit and share."""

from datetime import datetime
from functools import lru_cache
import io

from .media_payload import make_media
from .models import CHINA_TIME, Contact, Conversation, Message


@lru_cache(maxsize=1)
def _demo_image_bytes() -> tuple[bytes, bytes]:
    """Draw original, fictional artwork without files or external resources."""
    from PIL import Image, ImageDraw

    picture = Image.new("RGB", (480, 280), "#eef4e9")
    drawing = ImageDraw.Draw(picture)
    drawing.rounded_rectangle((24, 24, 456, 256), radius=25, fill="#dbe8cf")
    drawing.ellipse((344, 44, 408, 108), fill="#f7d478")
    drawing.rounded_rectangle((80, 192, 380, 216), radius=8, fill="#b18d67")
    drawing.polygon(((112, 94), (232, 112), (232, 188), (112, 170)), fill="#fff9de")
    drawing.polygon(((232, 112), (352, 94), (352, 170), (232, 188)), fill="#fffdf1")
    drawing.line(((232, 112), (232, 187)), fill="#7b9c74", width=4)
    for offset in (0, 18, 36):
        drawing.line(((132, 116 + offset), (211, 128 + offset)), fill="#b4c5a4", width=3)
        drawing.line(((252, 128 + offset), (331, 116 + offset)), fill="#b4c5a4", width=3)
    png = io.BytesIO()
    picture.save(png, format="PNG")

    frames = []
    for index in range(6):
        frame = Image.new("RGB", (112, 112), "#fff6e8")
        drawing = ImageDraw.Draw(frame)
        drawing.polygon(((24, 43), (27, 19), (47, 34)), fill="#eab76b")
        drawing.polygon(((65, 34), (85, 19), (88, 43)), fill="#eab76b")
        drawing.ellipse((22, 28, 90, 94), fill="#efc785")
        drawing.ellipse((38, 53, 43, 60), fill="#5f4534")
        if index in (2, 3):
            drawing.line(((68, 57), (76, 57)), fill="#5f4534", width=3)
        else:
            drawing.ellipse((69, 53, 74, 60), fill="#5f4534")
        drawing.polygon(((52, 65), (60, 65), (56, 70)), fill="#b87360")
        drawing.arc((44, 63, 68, 81), start=15, end=165, fill="#5f4534", width=2)
        drawing.ellipse((80, 72 - index * 3, 103, 93 - index * 3), fill="#efc785")
        frames.append(frame)
    gif = io.BytesIO()
    frames[0].save(gif, format="GIF", save_all=True, append_images=frames[1:], duration=130, loop=0)
    return png.getvalue(), gif.getvalue()


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
    picture, sticker = _demo_image_bytes()
    messages = []
    for i, (time, mine, code, content) in enumerate(samples, 1):
        messages.append(Message(
            timestamp=int(datetime.strptime(time, "%Y-%m-%d %H:%M:%S").replace(tzinfo=CHINA_TIME).timestamp()),
            time=time, sender="我" if mine else contact.display_name,
            sender_username="demo_self" if mine else contact.username,
            sender_status="resolved", is_self=mine, type=types[code], type_code=code,
            content=content, source_db="demo", local_id=i, server_id=f"demo-{i}", sort_seq=i,
            media=make_media(picture) if code == 3 else make_media(sticker, "sticker") if code == 47 else None,
        ))
    return Conversation(contact, messages, metadata={"demo": True, "note": "全部内容为虚构演示数据。"})
