"""Offline, escaped HTML and readable text exports with safe file publication."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Iterable

from .media_payload import parse_media
from .models import Conversation, ExportError, Message
from .native_emoji import get_render_assets, prepare_native_assets, render_native_text

_FORMATS = frozenset({"html", "txt", "json"})
_SCOPE = "仅包含提供的本地数据库中可读取的聊天记录，不能代表账号的全部历史记录。"
_MEDIA_NOTE = "本机可恢复的图片、表情和语音嵌入 HTML / JSON，可离线查看或播放；未找到或不支持的媒体保留文字提示。视频和附件保留文字标记。"

_CSS = """
:root{color-scheme:light;--ink:#172d29;--muted:#657870;--accent:#167459;--paper:#f5f6f2;--line:#dce5df}
*{box-sizing:border-box}body{margin:0;background:var(--paper);color:var(--ink);font:15px/1.65 system-ui,-apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
button,input,select{font:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible{outline:3px solid #8bcab4;outline-offset:2px}
.shell{max-width:1080px;margin:auto;padding:0 28px}.masthead{padding:48px 0 26px}.eyebrow{font-size:11px;font-weight:750;letter-spacing:.18em;color:var(--accent)}
h1{font-size:clamp(25px,4vw,38px);line-height:1.25;margin:13px 0 18px;overflow-wrap:anywhere}h1 small{display:block;font-size:14px;font-weight:450;color:var(--muted);margin-top:11px}
.summary{display:flex;flex-wrap:wrap;gap:12px 28px;color:var(--muted);font-size:13px}.summary strong{color:var(--ink);font-weight:650}.notice{font-size:12px;color:var(--muted);margin:15px 0 0}
.toolbar{position:sticky;top:0;z-index:2;background:#f5f6f2f5;border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:17px 0 13px;backdrop-filter:blur(12px)}
.filters{display:grid;grid-template-columns:minmax(180px,2fr) minmax(125px,1fr) minmax(130px,1fr) minmax(130px,1fr);gap:12px}.field{display:flex;flex-direction:column;gap:5px;font-size:11px;color:var(--muted);font-weight:600}
input,select{width:100%;min-width:0;background:#fff;border:1px solid #ccd9d0;border-radius:8px;padding:9px 10px;color:var(--ink);font-size:13px}input[type=search]{appearance:none}
.toolbar-bottom{display:flex;align-items:center;justify-content:space-between;gap:14px;margin-top:10px}.result-count{font-size:12px;color:var(--muted)}.actions{display:flex;gap:7px}
button{font-size:12px;background:#fff;color:var(--ink);border:1px solid #ccd9d0;padding:5px 12px;border-radius:6px}button:hover{border-color:var(--accent);color:var(--accent)}
.chat-log{max-width:850px;margin:0 auto;padding:10px 0 40px}.day{margin-top:22px}.day-title{text-align:center;margin:24px 0 21px;font-size:11px;font-weight:550;color:var(--muted)}.day-title span{display:inline-block;border:1px solid var(--line);border-radius:99px;padding:4px 15px;background:#fff}
.message{display:flex;flex-direction:column;align-items:flex-start;margin:0 0 19px;padding-right:13%}.message.self{align-items:flex-end;padding-right:0;padding-left:13%}
.message-meta{display:flex;gap:10px;align-items:baseline;font-size:11px;color:var(--muted);max-width:100%;margin:0 4px 5px}.sender{font-weight:650;color:#38594d;overflow-wrap:anywhere}.message.self .message-meta{flex-direction:row-reverse}
.bubble{min-width:65px;max-width:100%;background:#fff;border:1px solid var(--line);border-radius:3px 13px 13px 13px;padding:11px 15px;box-shadow:0 2px 3px #173c2210}.self .bubble{background:#e0f0e7;border-color:#c5dfcf;border-radius:13px 3px 13px 13px}
.message-content,.transcript{white-space:pre-wrap;overflow-wrap:anywhere;word-break:break-word}.transcript{margin-top:8px;padding-top:8px;border-top:1px solid #d7e4dc;font-size:13px;color:#456454}.type-label{font-size:10px;display:inline-block;margin-bottom:5px;color:var(--muted)}
.native-emoji{display:inline-block;position:relative;width:1.5em;height:1.5em;vertical-align:-.35em;white-space:normal}.native-emoji-image{display:block;width:100%;height:100%;object-fit:contain}.native-emoji-label{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap;border:0}
.media-toggle{display:block;max-width:100%;padding:0;margin-top:8px;border:0;background:transparent;border-radius:8px;overflow:hidden;cursor:zoom-in}.media-toggle:first-child{margin-top:0}.media-image{display:block;max-width:100%;width:auto;height:auto;max-height:340px;object-fit:contain;border-radius:8px}.media-toggle:not(.expanded){max-width:320px}.media-toggle.expanded{cursor:zoom-out}.media-toggle.expanded .media-image{max-height:none}.media-unavailable,.media-reason{font-size:11px;color:var(--muted);margin-top:6px;white-space:pre-wrap;overflow-wrap:anywhere}
.media-audio{display:block;width:280px;max-width:100%;height:40px;margin-top:8px}.media-audio:first-child{margin-top:0}
.warnings{border:1px solid #e6dbbc;background:#fffcf1;padding:12px 16px;border-radius:9px;font-size:12px;margin:22px 0;color:#775e2a}.warnings summary{cursor:pointer;font-weight:600}.warnings ul{margin:8px 0 0;padding-left:20px;overflow-wrap:anywhere}
.empty{text-align:center;padding:70px 20px;color:var(--muted)}.empty strong{display:block;color:var(--ink);font-size:18px;margin-bottom:6px}.footer{border-top:1px solid var(--line);padding:20px 0 30px;color:var(--muted);font-size:11px}
[hidden]{display:none!important}@media(max-width:680px){.shell{padding:0 16px}.masthead{padding-top:28px}.filters{grid-template-columns:1fr 1fr;gap:9px}.toolbar{padding-top:12px}.message{padding-right:5%}.message.self{padding-left:5%}.summary{gap:5px 16px}.bubble{padding:10px 12px}.message-meta{gap:7px}}
@media print{body{background:white;font-size:11px}.shell{max-width:none;padding:0}.masthead{padding:0 0 15px}h1{font-size:24px}.toolbar,.actions,.footer,.warnings,.media-hint{display:none}.chat-log{max-width:none;padding-bottom:0}.message{break-inside:avoid;margin-bottom:12px}.bubble{box-shadow:none;border-color:#bbb}.day-title{break-after:avoid}.day{margin-top:15px}.self .bubble{background:#f1f7f3}.notice{font-size:10px}.media-toggle.expanded .media-image{max-height:340px}}
"""

# User data is never interpolated into this script. All text comes from escaped
# HTML text nodes, and all DOM updates below use textContent or safe properties.
_JS = """
(() => {
  'use strict';
  const search = document.getElementById('search');
  const sender = document.getElementById('sender');
  const from = document.getElementById('from');
  const to = document.getElementById('to');
  const result = document.getElementById('result-count');
  const empty = document.getElementById('empty');
  const groups = Array.from(document.querySelectorAll('.day')).map(section => ({
    section,
    entries: Array.from(section.querySelectorAll('.message')).map(node => ({
      node,
      sender: node.dataset.sender,
      day: node.dataset.day,
      audio: Array.from(node.querySelectorAll('audio')),
      search: node.textContent.toLocaleLowerCase()
    }))
  }));
  const total = groups.reduce((n, group) => n + group.entries.length, 0);
  let timer;
  function filter() {
    const query = search.value.trim().toLocaleLowerCase();
    const selected = sender.value;
    const start = from.value;
    const end = to.value;
    let count = 0;
    for (const group of groups) {
      let visible = 0;
      for (const entry of group.entries) {
        const match = (!query || entry.search.includes(query)) &&
          (!selected || entry.sender === selected) &&
          (!start || (entry.day && entry.day >= start)) &&
          (!end || (entry.day && entry.day <= end));
        if (entry.node.hidden === match) entry.node.hidden = !match;
        if (!match) for (const audio of entry.audio) audio.pause();
        if (match) visible++;
      }
      group.section.hidden = visible === 0;
      count += visible;
    }
    result.textContent = `显示 ${count.toLocaleString()} / ${total.toLocaleString()} 条记录`;
    empty.hidden = count !== 0;
  }
  search.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(filter, 150);
  });
  for (const field of [sender, from, to]) field.addEventListener('change', filter);
  document.getElementById('reset').addEventListener('click', () => {
    clearTimeout(timer);
    search.value = ''; sender.value = ''; from.value = ''; to.value = '';
    filter(); search.focus();
  });
  document.getElementById('print').addEventListener('click', () => {
    clearTimeout(timer); filter(); window.print();
  });
  for (const button of document.querySelectorAll('.media-toggle')) {
    button.addEventListener('click', () => {
      const expanded = button.classList.toggle('expanded');
      button.setAttribute('aria-expanded', String(expanded));
      button.setAttribute('aria-label', expanded ? '缩小图片' : '放大图片');
    });
  }
  const audioPlayers = Array.from(document.querySelectorAll('.media-audio'));
  for (const audio of audioPlayers) {
    audio.addEventListener('play', () => {
      for (const other of audioPlayers) if (other !== audio) other.pause();
      if (audio.closest('.message').hidden) audio.pause();
    });
  }
  filter();
})();
"""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _day(time: str) -> str:
    """Only valid ISO calendar dates may become machine-readable filter values."""
    value = time[:10]
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        return ""


def _transcript(message: Message) -> str:
    if message.transcript and message.transcript != message.content:
        return message.transcript
    return ""


def _render_json(conversation: Conversation) -> str:
    return json.dumps(conversation.to_dict(), ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def _media_reason(message: Message) -> str:
    if message.media is not None:
        media = message.media
        if media.reason:
            return media.reason
        if media.status == "missing":
            return "本机未找到可恢复的媒体文件。"
        if media.status == "unsupported":
            return "此语音格式暂不支持播放。" if media.kind == "audio" else "此媒体格式暂不支持展示。"
    elif (message.type_code & 0xFFFFFFFF) in {3, 47}:
        return "此记录未附带可展示的图片文件。"
    elif (message.type_code & 0xFFFFFFFF) == 34:
        return "此记录未附带可播放的语音文件。"
    return ""


def _media_html(message: Message) -> str:
    media = message.media
    reason = _media_reason(message)
    if media is None or media.status != "available":
        return f'<div class="media-unavailable">{_escape(reason)}</div>' if reason else ""
    # write_exports validates the MIME, strict Base64, and media bytes before
    # any output is staged; paths and remote URLs cannot enter this attribute.
    source = f"data:{media.mime_type};base64,{media.data_base64}"
    reason_html = f'<div class="media-reason">{_escape(reason)}</div>' if reason else ""
    if media.kind == "audio":
        return (
            '<audio class="media-audio" controls preload="none" aria-label="聊天语音" '
            f'src="{_escape(source)}">浏览器不支持语音播放。</audio>' + reason_html
        )
    label = "动画表情" if media.kind == "sticker" else "聊天图片"
    return (
        '<button class="media-toggle" type="button" aria-expanded="false" aria-label="放大图片">'
        f'<img class="media-image" src="{_escape(source)}" alt="{label}" loading="lazy" decoding="async">'
        '</button>' + reason_html
    )


def _render_txt(conversation: Conversation) -> str:
    data = conversation.to_dict()
    lines = [
        f"聊天记录：{data['chat_name']}",
        f"导出时间：{data['exported_at']}",
        "时区：Asia/Shanghai（UTC+08:00）",
        f"记录数量：{data['message_count']}",
        f"时间范围：{data['first_time'] or '无记录'} 至 {data['last_time'] or '无记录'}",
        f"记录范围：{_SCOPE}",
        f"媒体说明：{_MEDIA_NOTE}",
    ]
    if conversation.warnings:
        lines.extend(["", "导出提示："])
        lines.extend(f"- {warning}" for warning in conversation.warnings)
    lines.extend(["", "=" * 64, ""])
    for message in conversation.messages:
        lines.append(f"[{message.time}] {message.sender}")
        lines.append(message.content)
        if reason := _media_reason(message):
            lines.append("[媒体提示] " + reason)
        if transcript := _transcript(message):
            lines.append(f"[语音转写] {transcript}")
        lines.append("")
    return "\n".join(lines) + "\n"


def _hash_csp(source: str) -> str:
    digest = hashlib.sha256(source.encode("utf-8")).digest()
    return "'sha256-" + base64.b64encode(digest).decode("ascii") + "'"


def _render_html(conversation: Conversation) -> str:
    data = conversation.to_dict()
    native_assets = get_render_assets(conversation)
    senders = dict.fromkeys(message.sender for message in conversation.messages)
    sender_ids = {name: str(index) for index, name in enumerate(senders, start=1)}
    options = ''.join(
        f'<option value="{index}">{_escape(name)}</option>'
        for name, index in sender_ids.items()
    )
    groups: dict[str, list[Message]] = defaultdict(list)
    for message in conversation.messages:
        groups[_day(message.time)].append(message)
    sections = []
    for day, messages in groups.items():
        items = []
        for message in messages:
            position = " self" if message.is_self is True else ""
            image_shown = ((message.type_code & 0xFFFFFFFF) == 3
                           and message.media is not None
                           and message.media.status == "available")
            audio_shown = ((message.type_code & 0xFFFFFFFF) == 34
                           and message.media is not None
                           and message.media.kind == "audio"
                           and message.media.status == "available")
            type_label = (
                f'<div class="type-label">{_escape(message.type)}</div>'
                if message.type_code != 1 and not (image_shown or audio_shown) else ""
            )
            content_html = (
                f'<div class="message-content">{render_native_text(message.content, native_assets)}</div>'
                if not ((image_shown and message.content.strip() == "[图片]")
                        or (audio_shown and message.content.strip() == "[语音]")) else ""
            )
            transcript = _transcript(message)
            transcript_html = (
                f'<div class="transcript">语音转写：{render_native_text(transcript, native_assets)}</div>'
                if transcript else ""
            )
            items.append(
                f'<article class="message{position}" data-sender="{sender_ids[message.sender]}" data-day="{day}">'
                f'<div class="message-meta"><span class="sender">{_escape(message.sender)}</span>'
                f'<time>{_escape(message.time)}</time></div>'
                f'<div class="bubble">{type_label}{content_html}'
                f'{_media_html(message)}{transcript_html}</div></article>'
            )
        sections.append(
            f'<section class="day"><h2 class="day-title"><span>{day or "未标注日期"}</span></h2>'
            + ''.join(items) + '</section>'
        )
    warnings = ""
    if conversation.warnings:
        warning_items = ''.join(f'<li>{_escape(warning)}</li>' for warning in conversation.warnings)
        warnings = (
            f'<details class="warnings"><summary>导出提示 · {len(conversation.warnings)} 项</summary>'
            f'<ul>{warning_items}</ul></details>'
        )
    csp = (
        "default-src 'none'; "
        f"script-src {_hash_csp(_JS)}; style-src {_hash_csp(_CSS)}; "
        "connect-src 'none'; img-src data:; font-src 'none'; media-src data:; "
        "object-src 'none'; frame-src 'none'; base-uri 'none'; form-action 'none'"
    )
    count = len(conversation.messages)
    empty_hidden = " hidden" if count else ""
    return f'''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="{_escape(csp)}">
<meta name="referrer" content="no-referrer">
<title>{_escape(data['chat_name'])} · 聊天记录</title>
<style>{_CSS}</style></head><body><div class="shell">
<header class="masthead"><div class="eyebrow">WECHAT / READABLE EXPORT</div>
<h1>{_escape(data['chat_name'])}<small>本地聊天记录 · 离线阅读</small></h1>
<div class="summary"><span><strong>{count:,}</strong> 条记录</span>
<span>{_escape(data['first_time'] or '无记录')} — {_escape(data['last_time'] or '无记录')}</span>
<span>Asia/Shanghai · UTC+08:00</span></div>
<p class="notice">{_SCOPE}<br>{_MEDIA_NOTE}</p></header>
<div class="toolbar" role="search" aria-label="筛选聊天记录"><div class="filters">
<label class="field" for="search">搜索内容或发送人<input id="search" type="search" placeholder="输入关键词…" autocomplete="off"></label>
<label class="field" for="sender">发送人<select id="sender"><option value="">全部发送人</option>{options}</select></label>
<label class="field" for="from">开始日期<input id="from" type="date"></label>
<label class="field" for="to">结束日期<input id="to" type="date"></label></div>
<div class="toolbar-bottom"><output class="result-count" id="result-count" aria-live="polite">显示 {count:,} / {count:,} 条记录</output>
<div class="actions"><button id="reset" type="button">清除筛选</button><button id="print" type="button">打印 / PDF</button></div></div></div>
{warnings}<main class="chat-log" aria-label="聊天记录">{''.join(sections)}
<div class="empty" id="empty"{empty_hidden}><strong>没有匹配的记录</strong>试试调整关键词、发送人或日期范围。</div></main>
<footer class="footer">导出于 {_escape(data['exported_at'])} · 本文件可离线阅读，筛选操作仅在当前浏览器内完成。<noscript> 搜索和筛选需要启用 JavaScript；所有记录仍可直接阅读。</noscript></footer>
</div><script>{_JS}</script></body></html>
'''


def _check_destinations(paths: Iterable[Path], overwrite: bool) -> None:
    for path in paths:
        if os.path.lexists(path):
            if not overwrite:
                raise ExportError(f"目标文件已存在：{path.name}。请选择新目录，或显式使用 --overwrite。")
            if path.is_symlink() or not path.is_file():
                raise ExportError(f"不能覆盖非普通文件：{path.name}。请选择新目录。")


def _stage(path: Path, content: str) -> Path:
    descriptor, temporary_name = tempfile.mkstemp(prefix=".chat-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def _publish(staged: dict[Path, Path], overwrite: bool) -> None:
    """Publish complete files; roll back this call if any publication fails."""
    published: list[Path] = []
    backups: dict[Path, Path] = {}
    try:
        _check_destinations(staged, overwrite)
        if overwrite:
            # Hard links preserve originals until every new file is in place.
            # Temporary and target files share a filesystem by construction.
            for target in staged:
                if target.exists():
                    descriptor, name = tempfile.mkstemp(prefix=".chat-backup-", suffix=".tmp", dir=target.parent)
                    os.close(descriptor)
                    backup = Path(name)
                    backup.unlink()
                    os.link(target, backup)
                    backups[target] = backup
        for target, temporary in staged.items():
            if overwrite:
                os.replace(temporary, target)
            else:
                # Unlike replace()/rename(), link() atomically refuses an
                # existing destination even if it appeared after preflight.
                os.link(temporary, target)
            published.append(target)
    except BaseException as original_error:
        recovery_errors = []
        for target in reversed(published):
            try:
                if target in backups:
                    os.replace(backups[target], target)
                    del backups[target]
                elif not overwrite:
                    # A concurrent writer may have replaced our newly linked
                    # file. Only remove the inode that this call published.
                    try:
                        if os.path.samefile(target, staged[target]):
                            target.unlink(missing_ok=True)
                    except FileNotFoundError:
                        pass
                else:
                    target.unlink(missing_ok=True)
            except OSError as recovery_error:
                recovery_errors.append(f"{target.name}: {recovery_error}")
        if recovery_errors:
            # Preserve backups if the OS prevents rollback; never delete the
            # only remaining original just to remove temporary files.
            saved = ', '.join(str(path) for path in backups.values())
            backups.clear()
            raise ExportError(
                f"导出失败且无法完整回滚：{'; '.join(recovery_errors)}。原文件备份：{saved or '无'}"
            ) from original_error
        raise
    finally:
        for backup in backups.values():
            backup.unlink(missing_ok=True)


def write_exports(
    conversation: Conversation,
    output_dir: str | Path,
    formats: Iterable[str] = ("html", "txt", "json"),
    *,
    overwrite: bool = False,
) -> dict[str, Path]:
    """Write fixed-name files, staging all formats before publishing any of them.

    Existing exports are refused by default. A concurrent destination conflict
    also refuses publication and removes files newly published by this call.
    The caller should supply a separate output directory for each conversation.
    """
    selected = tuple(dict.fromkeys(formats))
    if not selected:
        raise ExportError("请至少选择一种导出格式：html、txt、json。")
    invalid = set(selected) - _FORMATS
    if invalid:
        raise ExportError(f"不支持的导出格式：{', '.join(sorted(invalid))}。")
    directory = Path(output_dir)
    targets = {kind: directory / f"chat.{kind}" for kind in selected}
    renderers = {"html": _render_html, "txt": _render_txt, "json": _render_json}
    staged: dict[Path, Path] = {}
    try:
        for message in conversation.messages:
            message.media = parse_media(message.media)
        prepare_native_assets(conversation)
        directory.mkdir(parents=True, exist_ok=True)
        _check_destinations(targets.values(), overwrite)
        for kind, target in targets.items():
            staged[target] = _stage(target, renderers[kind](conversation))
        _publish(staged, overwrite)
    except ExportError:
        raise
    except (OSError, ValueError, TypeError) as error:
        raise ExportError(f"无法写入导出文件：{error}") from error
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
    return targets
