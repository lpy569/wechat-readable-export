"""Synthetic-only tests for offline native emoji and literal text retention."""

from __future__ import annotations

import base64
from dataclasses import asdict
import io
import json
from pathlib import Path
import random
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from wechat_readable_export import native_emoji, rendering
from wechat_readable_export.media_payload import make_media
from wechat_readable_export.models import Contact, Conversation, ExportError, Message
from test_rendering import Document


def png_payload(size: tuple[int, int] = (24, 24), color: str = "orange") -> dict:
    stream = io.BytesIO()
    Image.new("RGB", size, color).save(stream, format="PNG")
    return asdict(make_media(stream.getvalue(), "sticker"))


def synthetic_conversation(text: str = "你好[捂脸][微笑] & [未知表情]") -> Conversation:
    return Conversation(
        contact=Contact("synthetic-native-emoji", nickname="合成好友"),
        messages=[Message(1767261600, "2026-01-01 18:00:00+08:00", "我", "synthetic-self", "resolved",
                          True, "文本", 1, text)],
    )


class NativeEmojiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)
        # Never inspect a real user cache during synthetic tests.
        self.cache_patch = patch.object(native_emoji, "default_cache_path", return_value=self.root / "missing.json")
        self.cache_patch.start()
        self.addCleanup(self.cache_patch.stop)

    def write_cache(self, assets: dict, *, wrapper: bool = True) -> Path:
        value = {"schema_version": 1, "source": "https://example.invalid/synthetic.png", "assets": assets} if wrapper else assets
        path = self.root / "cache.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_native_tokens_are_bounded_distinct_literal_candidates(self) -> None:
        self.assertEqual(native_emoji.native_tokens("[捂脸][微笑][捂脸][不存在]"), ["[捂脸]", "[微笑]", "[不存在]"])
        self.assertEqual(native_emoji.native_tokens("[] [跨\n行] [" + "长" * 17 + "]"), [])

    def test_wrapper_cache_selects_only_used_assets_and_keeps_source_private(self) -> None:
        payload = png_payload()
        data = synthetic_conversation()
        path = self.write_cache({"[捂脸]": payload, "[再见]": payload})
        native_emoji.prepare_native_assets(data, path)
        self.assertEqual(data.metadata["native_emoji_assets"], {"[捂脸]": payload})
        self.assertNotIn("source", data.metadata)
        self.assertNotIn("https://", json.dumps(data.metadata))
        self.assertTrue(native_emoji.get_render_assets(data)["[捂脸]"].startswith("data:image/png;base64,"))

    def test_embedded_assets_win_over_cache_and_transcript_assets_are_kept(self) -> None:
        data = synthetic_conversation()
        embedded = png_payload(color="red")
        cached = png_payload(color="blue")
        data.metadata["native_emoji_assets"] = {"[捂脸]": embedded}
        data.messages[0].transcript = "合成转写[再见]"
        native_emoji.prepare_native_assets(data, self.write_cache({"[捂脸]": cached, "[再见]": cached}, wrapper=False))
        self.assertEqual(data.metadata["native_emoji_assets"]["[捂脸]"], embedded)
        self.assertEqual(data.metadata["native_emoji_assets"]["[再见]"], cached)

    def test_html_is_offline_safe_searchable_and_original_formats_are_unchanged(self) -> None:
        original = '<script>alert("x")</script>[捂脸][微笑] & [未知表情]'
        data = synthetic_conversation(original)
        data.messages[0].transcript = "合成转写[微笑]"
        payload = png_payload()
        data.metadata["native_emoji_assets"] = {"[捂脸]": payload, "[微笑]": payload, "[再见]": payload}
        paths = rendering.write_exports(data, self.root / "export")
        source = paths["html"].read_text(encoding="utf-8")
        document = Document(source)
        images = [attrs for tag, attrs in document.tags if tag == "img"]
        self.assertEqual(len(images), 3)
        self.assertTrue(all(attrs.get("class") == "native-emoji-image" for attrs in images))
        self.assertTrue(all(attrs["src"].startswith("data:image/png;base64,") for attrs in images))
        self.assertTrue(all(attrs.get("aria-hidden") == "true" for attrs in images))
        self.assertFalse(any(tag == "button" and attrs.get("class") == "media-toggle" for tag, attrs in document.tags))
        self.assertFalse(any(name.startswith("on") for _, attrs in document.tags for name in attrs))
        self.assertEqual(sum(tag == "script" for tag, _ in document.tags), 1)
        self.assertIn(original, ''.join(document.text))
        self.assertIn("&lt;script&gt;", source)
        self.assertIn("[未知表情]", source)
        self.assertIn("node.textContent.toLocaleLowerCase()", source)
        self.assertEqual(data.messages[0].content, original)
        exported = json.loads(paths["json"].read_text(encoding="utf-8"))
        self.assertEqual(exported["messages"][0]["content"], original)
        self.assertEqual(exported["messages"][0]["transcript"], "合成转写[微笑]")
        self.assertEqual(set(exported["metadata"]["native_emoji_assets"]), {"[捂脸]", "[微笑]"})
        self.assertIn(original, paths["txt"].read_text(encoding="utf-8"))
        policy = next(attrs["content"] for tag, attrs in document.tags if tag == "meta" and attrs.get("http-equiv") == "Content-Security-Policy")
        self.assertIn("img-src data:", policy)
        self.assertIn("connect-src 'none'", policy)
        self.assertNotIn("unsafe-inline", policy)

    def test_unknown_labels_and_remote_sources_remain_literal(self) -> None:
        text = '[未知表情] & <b>[捂脸]</b>'
        source = native_emoji.render_native_text(text, {"[捂脸]": "https://example.invalid/image.png"})
        self.assertEqual(source, '[未知表情] &amp; &lt;b&gt;[捂脸]&lt;/b&gt;')
        self.assertNotIn("<img", source)
        self.assertEqual(native_emoji.render_native_text("[动画表情]", {}), "[动画表情]")

    def test_missing_cache_keeps_labels_and_corrupt_implicit_cache_warns_once(self) -> None:
        data = synthetic_conversation()
        native_emoji.prepare_native_assets(data)
        self.assertEqual(data.metadata, {})
        self.assertEqual(data.warnings, [])
        bad = self.root / "bad.json"
        bad.write_text("{invalid", encoding="utf-8")
        with patch.object(native_emoji, "default_cache_path", return_value=bad):
            native_emoji.prepare_native_assets(data)
            native_emoji.prepare_native_assets(data)
        self.assertEqual(len(data.warnings), 1)
        with self.assertRaises(ValueError):
            native_emoji.prepare_native_assets(data, bad)

    def test_invalid_asset_shapes_or_rasters_fail_before_output_staging(self) -> None:
        valid = png_payload()
        cases = [
            {"not-a-token": valid},
            {"[捂脸]": {**valid, "source": "https://example.invalid/image.png"}},
            {"[捂脸]": {**valid, "kind": "image"}},
            {"[捂脸]": {**valid, "status": "missing"}},
            {"[捂脸]": {**valid, "mime_type": "image/svg+xml"}},
            {"[捂脸]": {**valid, "data_base64": "https://example.invalid/image.png"}},
            {"[捂脸]": {**valid, "data_base64": base64.b64encode(b'<svg xmlns="http://www.w3.org/2000/svg"/>').decode("ascii")}},
            {"[捂脸]": {**valid, "data_base64": "AAAA"}},
            {"[捂脸]": {**valid, "reason": "unexpected resource URL"}},
            {"[捂脸]": png_payload((129, 24))},
            {"[捂脸]": {**valid, "data_base64": "A" * (4 * ((native_emoji.MAX_NATIVE_BYTES + 2) // 3) + 4)}},
            {f"[合成{i}]": valid for i in range(257)},
        ]
        for index, assets in enumerate(cases):
            with self.subTest(index=index):
                data = synthetic_conversation()
                data.metadata["native_emoji_assets"] = assets
                directory = self.root / f"bad-{index}"
                with self.assertRaises(ExportError):
                    rendering.write_exports(data, directory)
                self.assertFalse(directory.exists())

    def test_total_decoded_asset_limit_is_enforced(self) -> None:
        pixels = random.Random(42).randbytes(128 * 128 * 3)
        stream = io.BytesIO()
        Image.frombytes("RGB", (128, 128), pixels).save(stream, format="PNG")
        payload = asdict(make_media(stream.getvalue(), "sticker"))
        count = native_emoji.MAX_NATIVE_TOTAL_BYTES // len(stream.getvalue()) + 1
        self.assertLess(count, native_emoji.MAX_NATIVE_ASSETS)
        data = synthetic_conversation()
        data.metadata["native_emoji_assets"] = {f"[合成{i}]": payload for i in range(count)}
        with self.assertRaises(ValueError):
            native_emoji.get_render_assets(data)

    def test_animated_png_is_not_a_native_inline_tile(self) -> None:
        stream = io.BytesIO()
        first = Image.new("RGBA", (24, 24), "red")
        second = Image.new("RGBA", (24, 24), "blue")
        first.save(stream, format="PNG", save_all=True, append_images=[second], duration=100, loop=0)
        data = synthetic_conversation()
        data.metadata["native_emoji_assets"] = {"[捂脸]": asdict(make_media(stream.getvalue(), "sticker"))}
        with self.assertRaises(ValueError):
            native_emoji.get_render_assets(data)


if __name__ == "__main__":
    unittest.main()
