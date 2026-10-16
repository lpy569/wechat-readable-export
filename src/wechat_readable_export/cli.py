"""Command-line interface. Import the Windows reader only for live commands."""

from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import datetime, timedelta
import json
import math
from pathlib import Path
import sys

from . import __version__
from .models import CHINA_TIME, Contact, Conversation, ExportError, Message


def select_contact(contacts: list[Contact], keyword: str) -> Contact:
    """Resolve an unambiguous name; never silently select the first match."""
    if not isinstance(keyword, str) or not keyword.strip():
        raise ExportError("联系人名称或 ID 不能为空。")
    exact = [c for c in contacts if keyword in (c.username, c.nickname, c.remark)]
    candidates = exact or contacts
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise ExportError(f"没有找到联系人：{keyword}。可先运行 contacts --search。")
    choices = "\n".join(f"  {c.username}  {display_cell(c.display_name)}" for c in candidates)
    raise ExportError("匹配到多个会话，请用 --contact-id 指定唯一 ID：\n" + choices)


def display_cell(value: str) -> str:
    return str(value).replace("\t", " ").replace("\r", " ").replace("\n", " ")


def date_range(start: str | None, end: str | None) -> tuple[datetime | None, datetime | None]:
    if any(value is not None and not value.strip() for value in (start, end)):
        raise ExportError("日期参数不能为空，请使用 YYYY-MM-DD 或省略该参数。")
    try:
        a = datetime.strptime(start, "%Y-%m-%d").replace(tzinfo=CHINA_TIME) if start else None
        b = datetime.strptime(end, "%Y-%m-%d").replace(tzinfo=CHINA_TIME) + timedelta(days=1) if end else None
    except (ValueError, OverflowError) as exc:
        raise ExportError("日期需要使用 YYYY-MM-DD 格式，例如 2026-01-01。") from exc
    if a and b and a >= b:
        raise ExportError("--start 不能晚于 --end。日期范围包含起止两天。")
    return a, b


def load_json(path: Path) -> Conversation:
    """Only accept this project's schema; never execute imported content."""
    try:
        from .media_payload import parse_media

        def reject_constant(value: str):
            raise ValueError(f"不允许 JSON 非有限数值：{value}")
        data = json.loads(path.read_text(encoding="utf-8-sig"), parse_constant=reject_constant)
        if not isinstance(data, dict) or data.get("schema_version") != 1:
            raise ValueError("需要 schema_version=1 的项目导出文件")
        contact_data = data["contact"]
        if not isinstance(contact_data, dict) or not isinstance(contact_data.get("username"), str):
            raise ValueError("contact.username 无效")
        contact = Contact(**{k: v for k, v in contact_data.items() if k in {f.name for f in fields(Contact)}})
        if not all(isinstance(v, str) for v in (contact.username, contact.nickname, contact.remark)):
            raise ValueError("联系人字段必须是字符串")
        if not contact.username or any(ch.isspace() or ch == "\0" for ch in contact.username):
            raise ValueError("contact.username 不能为空或包含空白字符")
        if not isinstance(data["messages"], list):
            raise ValueError("messages 必须为数组")
        names = {f.name for f in fields(Message)}
        messages = []
        for item in data["messages"]:
            if not isinstance(item, dict):
                raise ValueError("消息必须是对象")
            msg = Message(**{k: v for k, v in item.items() if k in names})
            msg.media = parse_media(msg.media)
            if not all(isinstance(v, str) for v in (msg.time, msg.sender, msg.sender_status, msg.type, msg.content)):
                raise ValueError("消息文字字段无效")
            if msg.transcript is not None and not isinstance(msg.transcript, str):
                raise ValueError("transcript 必须为字符串或 null")
            if not isinstance(msg.timestamp, (int, float)) or isinstance(msg.timestamp, bool):
                raise ValueError("timestamp 必须为数值")
            if not math.isfinite(msg.timestamp):
                raise ValueError("timestamp 必须为有限数值")
            actual = datetime.fromtimestamp(msg.timestamp, CHINA_TIME)
            supplied = datetime.fromisoformat(msg.time)
            if supplied.tzinfo is None:
                supplied = supplied.replace(tzinfo=CHINA_TIME)
            if abs(actual.timestamp() - supplied.timestamp()) > 0.001:
                raise ValueError("time 与 timestamp 不一致")
            if not isinstance(msg.type_code, int) or isinstance(msg.type_code, bool):
                raise ValueError("type_code 必须为整数")
            if msg.is_self is not None and type(msg.is_self) is not bool:
                raise ValueError("is_self 必须为布尔值或 null")
            if msg.sender_username is not None and not isinstance(msg.sender_username, str):
                raise ValueError("sender_username 必须为字符串或 null")
            if not isinstance(msg.source_db, str):
                raise ValueError("source_db 必须为字符串")
            if msg.local_id is not None and type(msg.local_id) is not int:
                raise ValueError("local_id 必须为整数或 null")
            if msg.server_id is not None and (not isinstance(msg.server_id, (int, str)) or isinstance(msg.server_id, bool)):
                raise ValueError("server_id 必须为整数、字符串或 null")
            if type(msg.sort_seq) is not int:
                raise ValueError("sort_seq 必须为整数")
            messages.append(msg)
        warnings = data.get("warnings", [])
        metadata = data.get("metadata", {})
        if not isinstance(warnings, list) or not all(isinstance(w, str) for w in warnings):
            raise ValueError("warnings 必须为字符串数组")
        if not isinstance(metadata, dict):
            raise ValueError("metadata 必须为对象")
        if "message_count" in data and data["message_count"] != len(messages):
            raise ValueError("message_count 与消息数组长度不一致")
        exported_at = data.get("exported_at")
        if exported_at is not None:
            if not isinstance(exported_at, str):
                raise ValueError("exported_at 必须为日期字符串")
            datetime.fromisoformat(exported_at)
            return Conversation(contact, messages, exported_at=exported_at, warnings=warnings, metadata=metadata)
        return Conversation(contact, messages, warnings=warnings, metadata=metadata)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        raise ExportError(f"JSON 文件格式无效：{exc}") from exc


def parser() -> argparse.ArgumentParser:
    def nonblank(value: str) -> str:
        if not value.strip():
            raise argparse.ArgumentTypeError("参数不能为空")
        return value

    def nonblank_path(value: str) -> Path:
        return Path(nonblank(value))

    p = argparse.ArgumentParser(prog="wechat-export", description="将微信本地聊天导出为可阅读文件（HTML / TXT / JSON）。")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    def live(name: str, help: str, account: bool = True):
        cmd = sub.add_parser(name, help=help, description=help)
        cmd.add_argument("--data-dir", type=nonblank_path, help="xwechat_files 目录或具体账号目录；默认自动探测")
        if account:
            cmd.add_argument("--account", type=nonblank, help="账号目录名称；发现多个账号时必须指定")
        return cmd

    live("doctor", "检查 Windows、微信进程与本地账号目录", account=False)
    live("accounts", "列出本地账号目录", account=False)
    contacts = live("contacts", "查询联系人或群聊的唯一 ID")
    contacts.add_argument("--search", default="", help="按昵称、备注或 ID 查询")
    export = live("export", "导出一个指定会话的全部本地记录")
    target = export.add_mutually_exclusive_group(required=True)
    target.add_argument("--contact", type=nonblank, help="昵称或备注；匹配不唯一时拒绝导出")
    target.add_argument("--contact-id", type=nonblank, help="contacts 命令返回的唯一 username")
    export.add_argument("--start", type=nonblank, help="起始日期 YYYY-MM-DD（北京时间，包含当天）")
    export.add_argument("--end", type=nonblank, help="结束日期 YYYY-MM-DD（北京时间，包含当天）")
    export.add_argument("--no-media", action="store_true", help="只导出文字和媒体标记，不读取图片或表情文件")
    export.add_argument("--download-stickers", action="store_true", help="明确允许从微信 CDN 下载本地缺失的表情；图片仍只读取本地文件")
    demo = sub.add_parser("demo", help="生成虚构聊天示例，无需微信，支持跨平台")
    convert = sub.add_parser("convert", help="将本项目导出的 JSON 重新生成为可阅读文件")
    convert.add_argument("input", type=nonblank_path, help="本项目 schema_version=1 的 chat.json")
    for cmd in (export, demo, convert):
        cmd.add_argument("--output", type=nonblank_path, help="独立输出目录；默认 exports/当前时间")
        cmd.add_argument("--format", nargs="+", choices=("html", "txt", "json"), default=["html", "txt", "json"], dest="formats")
        cmd.add_argument("--overwrite", action="store_true", help="显式允许替换输出目录内同名 chat.* 文件")
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "demo":
            from .demo import demo_conversation
            conversation = demo_conversation()
        elif args.command == "convert":
            conversation = load_json(args.input)
        else:
            from .backend import LocalReader, doctor, list_accounts
            if args.command == "doctor":
                print(json.dumps(doctor(args.data_dir), ensure_ascii=False, indent=2))
                return 0
            if args.command == "accounts":
                print(json.dumps(list_accounts(args.data_dir), ensure_ascii=False, indent=2))
                return 0
            bounds = date_range(args.start, args.end) if args.command == "export" else (None, None)
            if args.command == "export" and args.no_media and args.download_stickers:
                raise ExportError("--no-media 不能与 --download-stickers 同时使用。")
            def progress(message: str) -> None:
                print(message, file=sys.stderr, flush=True)
            with LocalReader(args.data_dir, args.account, progress=progress) as reader:
                if args.command == "contacts":
                    contacts = reader.contacts(args.search)
                    print("ID\t昵称\t备注")
                    for contact in contacts:
                        print("\t".join(map(display_cell, (contact.username, contact.nickname, contact.remark))))
                    print(f"共 {len(contacts)} 项", file=sys.stderr)
                    return 0
                contact_id = args.contact_id
                if not contact_id:
                    contact_id = select_contact(reader.contacts(args.contact), args.contact).username
                contact, self_username, rows, metadata = reader.read_conversation(contact_id)
                from .parsing import build_conversation
                metadata = {**metadata, "date_filter": {"start_inclusive": args.start, "end_inclusive": args.end}}
                conversation = build_conversation(contact, self_username, rows, start=bounds[0], end=bounds[1], metadata=metadata)
                if not args.no_media:
                    reader.attach_media(conversation, rows, download_stickers=args.download_stickers)
            if not conversation.messages:
                raise ExportError("指定会话或日期范围内没有本地消息，未写入导出文件。")
        from .rendering import write_exports
        output = args.output or Path("exports") / datetime.now(CHINA_TIME).strftime("%Y%m%d_%H%M%S")
        paths = write_exports(conversation, output, args.formats, overwrite=args.overwrite)
        print(f"已导出 {len(conversation.messages):,} 条消息：{conversation.contact.display_name}")
        for kind, path in paths.items():
            print(f"{kind.upper()}: {path.resolve()}")
        for warning in conversation.warnings:
            print("提示：" + warning, file=sys.stderr)
        return 0
    except (ExportError, OSError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("已取消，正在退出并清理临时文件。", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
