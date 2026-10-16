"""Synthetic-only tests for offline rendering, escaping, and publication safety."""

from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import unittest
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from wechat_readable_export import rendering
from wechat_readable_export.demo import demo_conversation
from wechat_readable_export.models import Contact, Conversation, ExportError, MediaAttachment, Message


def conversation() -> Conversation:
    return Conversation(
        contact=Contact("synthetic-contact-id", nickname="演示好友 🌿"),
        exported_at="2026-01-03T12:00:00+08:00",
        warnings=["演示提示：仅使用合成记录。"],
        messages=[
            Message(
                timestamp=1767261600,
                time="2026-01-01 18:00:00+08:00",
                sender="演示好友 🌿",
                sender_username="synthetic-contact-id",
                sender_status="resolved",
                is_self=False,
                type="text",
                type_code=1,
                content="你好，世界！\n第二行 & café",
                source_db="private-path/must-not-appear-in-html.db",
            ),
            Message(
                timestamp=1767348000,
                time="2026-01-02 18:00:00+08:00",
                sender="我",
                sender_username="synthetic-self-id",
                sender_status="resolved",
                is_self=True,
                type="voice",
                type_code=34,
                content="[语音]",
                transcript="合成转写文本。",
            ),
        ],
    )


class Document(HTMLParser):
    def __init__(self, source: str):
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str | None]]] = []
        self.text: list[str] = []
        self.feed(source)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.tags.append((tag, dict(attrs)))

    def handle_data(self, data: str) -> None:
        self.text.append(data)


class RenderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name) / "exports"
        self.addCleanup(self.temporary.cleanup)

    def test_unicode_and_transcript_roundtrip_all_formats(self) -> None:
        paths = rendering.write_exports(conversation(), self.directory)
        self.assertEqual({path.name for path in paths.values()}, {"chat.html", "chat.txt", "chat.json"})
        data = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(data["messages"][0]["content"], "你好，世界！\n第二行 & café")
        self.assertEqual(data["messages"][0]["sender_username"], "synthetic-contact-id")
        self.assertEqual(data["schema_version"], 1)
        text = paths["txt"].read_text(encoding="utf-8")
        self.assertIn("Asia/Shanghai（UTC+08:00）", text)
        self.assertIn("时间范围：2026-01-01", text)
        self.assertIn("记录范围：", text)
        self.assertIn("媒体说明：", text)
        self.assertIn("[语音转写] 合成转写文本。", text)
        self.assertIn("演示提示：仅使用合成记录。", text)
        source = paths["html"].read_text(encoding="utf-8")
        self.assertIn("你好，世界！\n第二行 &amp; café", source)
        self.assertIn('class="message self"', source)
        self.assertNotIn("synthetic-contact-id", source)
        self.assertNotIn("must-not-appear-in-html.db", source)
        self.assertNotIn("must-not-appear-in-html.db", text)
        self.assertEqual(sorted(path.name for path in self.directory.iterdir()), ["chat.html", "chat.json", "chat.txt"])

    def test_user_data_cannot_escape_html_text_or_create_active_elements(self) -> None:
        attack = '</title><script>alert("bad")</script><img src="https://invalid.test" onerror="alert(1)"> & \' "'
        data = conversation()
        data.contact = Contact("do-not-render-this-id", nickname=attack)
        data.messages[0].sender = attack
        data.messages[0].content = attack
        data.messages[1].transcript = attack
        data.messages[1].type = attack
        data.warnings = [attack]
        path = rendering.write_exports(data, self.directory, ("html",))["html"]
        source = path.read_text(encoding="utf-8")
        document = Document(source)
        self.assertEqual(sum(tag == "script" for tag, _ in document.tags), 1)
        self.assertEqual(sum(tag == "title" for tag, _ in document.tags), 1)
        self.assertFalse(any(tag == "img" for tag, _ in document.tags))
        self.assertFalse(any(name.startswith("on") for _, attrs in document.tags for name in attrs))
        self.assertIn(attack, ''.join(document.text))
        self.assertNotIn(attack, source)
        self.assertNotIn("do-not-render-this-id", source)

    def test_content_security_policy_allows_only_the_bundled_script_and_style(self) -> None:
        source = rendering.write_exports(conversation(), self.directory, ("html",))["html"].read_text(encoding="utf-8")
        document = Document(source)
        policy = next(attrs["content"] for tag, attrs in document.tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        self.assertIn("default-src 'none'", policy)
        self.assertIn("connect-src 'none'", policy)
        self.assertIn("img-src data:", policy)
        self.assertIn("form-action 'none'", policy)
        self.assertNotIn("unsafe-inline", policy)
        for tag in ("script", "style"):
            bundled = source.split(f"<{tag}>", 1)[1].split(f"</{tag}>", 1)[0]
            digest = base64.b64encode(hashlib.sha256(bundled.encode("utf-8")).digest()).decode("ascii")
            self.assertIn(f"{tag}-src 'sha256-{digest}'", policy)
        self.assertFalse(any("src" in attrs or "href" in attrs for _, attrs in document.tags))

    def test_images_and_animated_stickers_are_embedded_without_external_resources(self) -> None:
        data = demo_conversation()
        paths = rendering.write_exports(data, self.directory)
        source = paths["html"].read_text(encoding="utf-8")
        document = Document(source)
        images = [attrs for tag, attrs in document.tags if tag == "img"]
        self.assertEqual(len(images), 2)
        self.assertTrue(images[0]["src"].startswith("data:image/png;base64,"))
        self.assertTrue(images[1]["src"].startswith("data:image/gif;base64,"))
        self.assertTrue(all(attrs.get("loading") == "lazy" for attrs in images))
        self.assertFalse(any("href" in attrs for _, attrs in document.tags))
        self.assertFalse(any(name.startswith("on") for _, attrs in document.tags for name in attrs))
        buttons = [attrs for tag, attrs in document.tags if tag == "button" and attrs.get("class") == "media-toggle"]
        self.assertEqual(len(buttons), 2)
        self.assertTrue(all(attrs["aria-expanded"] == "false" for attrs in buttons))
        exported = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(exported["messages"][2]["media"]["data_base64"], images[0]["src"].split(",", 1)[1])
        self.assertIn("[动画表情]", paths["txt"].read_text(encoding="utf-8"))

    def test_missing_media_is_readable_and_its_reason_remains_literal(self) -> None:
        data = conversation()
        reason = '<img src="https://invalid.test" onerror="alert(1)"> & unavailable'
        data.messages[0].type_code = 3
        data.messages[0].content = "[图片]"
        data.messages[0].media = MediaAttachment("image", "missing", reason=reason)
        data.messages[1].type_code = 47
        paths = rendering.write_exports(data, self.directory)
        source = paths["html"].read_text(encoding="utf-8")
        document = Document(source)
        self.assertFalse(any(tag == "img" for tag, _ in document.tags))
        self.assertIn(reason, ''.join(document.text))
        self.assertIn("此记录未附带可展示的图片文件。", source)
        self.assertIn("[媒体提示] " + reason, paths["txt"].read_text(encoding="utf-8"))

    def test_invalid_in_memory_media_is_rejected_before_creating_outputs(self) -> None:
        data = conversation()
        data.messages[0].media = MediaAttachment("image", "available", "image/svg+xml", "PHN2Zy8+")
        with self.assertRaises(ExportError):
            rendering.write_exports(data, self.directory)
        self.assertFalse(self.directory.exists())

    def test_contact_name_cannot_choose_output_paths(self) -> None:
        data = conversation()
        data.contact = Contact("../../other", nickname="../../escape\\outside: ?*")
        paths = rendering.write_exports(data, self.directory)
        self.assertEqual(set(paths.values()), {self.directory / "chat.html", self.directory / "chat.txt", self.directory / "chat.json"})
        self.assertEqual([path.name for path in self.directory.parent.iterdir()], ["exports"])

    def test_default_refuses_overwrite_before_writing_any_selected_format(self) -> None:
        self.directory.mkdir()
        original = self.directory / "chat.html"
        original.write_bytes(b"original content")
        with self.assertRaises(ExportError):
            rendering.write_exports(conversation(), self.directory, ("txt", "html", "json"))
        self.assertEqual(original.read_bytes(), b"original content")
        self.assertEqual([path.name for path in self.directory.iterdir()], ["chat.html"])

    def test_all_formats_stage_before_any_existing_file_is_replaced(self) -> None:
        self.directory.mkdir()
        original = self.directory / "chat.html"
        original.write_bytes(b"original content")
        with patch.object(rendering, "_render_txt", side_effect=ValueError("synthetic render failure")):
            with self.assertRaisesRegex(ExportError, "synthetic render failure"):
                rendering.write_exports(conversation(), self.directory, overwrite=True)
        self.assertEqual(original.read_bytes(), b"original content")
        self.assertEqual([path.name for path in self.directory.iterdir()], ["chat.html"])

    def test_concurrent_conflict_preserves_other_file_and_rolls_back_ours(self) -> None:
        original_link = rendering.os.link

        def concurrent_link(source: Path, target: Path) -> None:
            if Path(target).name == "chat.txt":
                Path(target).write_bytes(b"concurrent writer's original")
            original_link(source, target)

        with patch.object(rendering.os, "link", side_effect=concurrent_link):
            with self.assertRaises(ExportError):
                rendering.write_exports(conversation(), self.directory)
        self.assertEqual([path.name for path in self.directory.iterdir()], ["chat.txt"])
        self.assertEqual((self.directory / "chat.txt").read_bytes(), b"concurrent writer's original")

    def test_overwrite_publication_failure_restores_originals_and_cleans_staging(self) -> None:
        self.directory.mkdir()
        for kind in ("html", "txt", "json"):
            (self.directory / f"chat.{kind}").write_text(f"original {kind}", encoding="utf-8")
        original_replace = rendering.os.replace
        calls = 0

        def failing_replace(source: Path, target: Path) -> None:
            nonlocal calls
            calls += 1
            if calls == 2:
                raise PermissionError("synthetic publication failure")
            original_replace(source, target)

        with patch.object(rendering.os, "replace", side_effect=failing_replace):
            with self.assertRaisesRegex(ExportError, "synthetic publication failure"):
                rendering.write_exports(conversation(), self.directory, overwrite=True)
        for kind in ("html", "txt", "json"):
            self.assertEqual((self.directory / f"chat.{kind}").read_text(encoding="utf-8"), f"original {kind}")
        self.assertEqual(len(list(self.directory.iterdir())), 3)

    def test_rollback_preserves_a_concurrent_replacement_of_our_output(self) -> None:
        original_link = rendering.os.link

        def concurrent_link(source: Path, target: Path) -> None:
            if Path(target).name == "chat.txt":
                html_path = self.directory / "chat.html"
                html_path.unlink()
                html_path.write_bytes(b"another writer's replacement")
                Path(target).write_bytes(b"concurrent conflict")
            original_link(source, target)

        with patch.object(rendering.os, "link", side_effect=concurrent_link):
            with self.assertRaises(ExportError):
                rendering.write_exports(conversation(), self.directory)
        self.assertEqual((self.directory / "chat.html").read_bytes(), b"another writer's replacement")
        self.assertEqual((self.directory / "chat.txt").read_bytes(), b"concurrent conflict")
        self.assertEqual(len(list(self.directory.iterdir())), 2)

    def test_explicit_overwrite_succeeds(self) -> None:
        self.directory.mkdir()
        (self.directory / "chat.txt").write_bytes(b"old")
        path = rendering.write_exports(conversation(), self.directory, ("txt",), overwrite=True)["txt"]
        self.assertIn("演示好友 🌿", path.read_text(encoding="utf-8"))
        self.assertEqual([item.name for item in self.directory.iterdir()], ["chat.txt"])

    def test_overwrite_does_not_replace_a_directory(self) -> None:
        (self.directory / "chat.html").mkdir(parents=True)
        with self.assertRaisesRegex(ExportError, "非普通文件"):
            rendering.write_exports(conversation(), self.directory, overwrite=True)
        self.assertTrue((self.directory / "chat.html").is_dir())
        self.assertFalse((self.directory / "chat.txt").exists())

    def test_empty_conversation_is_readable_and_has_zero_count(self) -> None:
        data = conversation()
        data.messages = []
        paths = rendering.write_exports(data, self.directory)
        source = paths["html"].read_text(encoding="utf-8")
        self.assertIn('id="empty"><strong>', source)
        self.assertIn("0 / 0 条记录", source)
        self.assertIn("时间范围：无记录 至 无记录", paths["txt"].read_text(encoding="utf-8"))
        self.assertEqual(json.loads(paths["json"].read_text(encoding="utf-8"))["message_count"], 0)

    def test_invalid_formats_do_not_create_output_directory(self) -> None:
        for formats in ((), ("../bad",), ("html", "xml")):
            with self.subTest(formats=formats):
                with self.assertRaises(ExportError):
                    rendering.write_exports(conversation(), self.directory, formats)
                self.assertFalse(self.directory.exists())


if __name__ == "__main__":
    unittest.main()
