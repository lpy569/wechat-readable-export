"""Synthetic image validation, including untrusted JSON payloads."""

import base64
from dataclasses import asdict
import io
import unittest
from unittest.mock import patch
import wave

from PIL import Image, features

from wechat_readable_export import media_payload
from wechat_readable_export.models import MediaAttachment


def raster(format: str = "PNG", *, animated: bool = False) -> bytes:
    image = Image.new("RGB", (8, 6), "#339966")
    output = io.BytesIO()
    options = {}
    if animated:
        options = {"save_all": True, "append_images": [Image.new("RGB", image.size, "#eeaa55")], "duration": 100, "loop": 0}
    image.save(output, format=format, **options)
    return output.getvalue()


class MediaPayloadTests(unittest.TestCase):
    def test_audio_wav_roundtrip_and_kind_mime_must_agree(self):
        output = io.BytesIO()
        with wave.open(output, "wb") as voice:
            voice.setnchannels(1)
            voice.setsampwidth(2)
            voice.setframerate(24000)
            voice.writeframes(b"\0\0" * 480)
        data = output.getvalue()
        media = media_payload.make_media(data, "audio")
        self.assertEqual(media.mime_type, "audio/wav")
        self.assertEqual(base64.b64decode(media.data_base64), data)
        self.assertEqual(media_payload.parse_media(asdict(media)), media)
        self.assertEqual(media_payload.parse_media(MediaAttachment("audio")), MediaAttachment("audio"))
        for kind in ("image", "sticker"):
            with self.subTest(kind=kind), self.assertRaises(ValueError):
                media_payload.make_media(data, kind)
            with self.assertRaises(ValueError):
                media_payload.parse_media({**asdict(media), "kind": kind})
        with self.assertRaises(ValueError):
            media_payload.make_media(raster(), "audio")
        image = asdict(media_payload.make_media(raster()))
        with self.assertRaises(ValueError):
            media_payload.parse_media({**image, "kind": "audio"})
        for change in ({"mime_type": "audio/mpeg"}, {"mime_type": "image/png"},
                       {"data_base64": media.data_base64 + "\n"},
                       {"data_base64": base64.b64encode(data[:-1]).decode()}):
            with self.subTest(change=list(change)), self.assertRaises(ValueError):
                media_payload.parse_media({**asdict(media), **change})

    def test_supported_rasters_roundtrip_and_keep_animation_bytes(self):
        formats = [("PNG", "image/png"), ("JPEG", "image/jpeg"), ("GIF", "image/gif")]
        if features.check("webp"):
            formats.append(("WEBP", "image/webp"))
        for format, mime in formats:
            with self.subTest(format=format):
                data = raster(format, animated=format == "GIF")
                payload = media_payload.make_media(data, "sticker" if format == "GIF" else "image")
                self.assertEqual(payload.status, "available")
                self.assertEqual(payload.mime_type, mime)
                self.assertEqual(media_payload.sniff_mime(data), mime)
                self.assertEqual(base64.b64decode(payload.data_base64), data)
                self.assertEqual(media_payload.parse_media(asdict(payload)), payload)
                self.assertEqual(media_payload.parse_media(payload), payload)
                if format == "GIF":
                    with Image.open(io.BytesIO(base64.b64decode(payload.data_base64))) as image:
                        self.assertEqual(image.n_frames, 2)

    def test_legacy_and_unavailable_media_do_not_need_image_data(self):
        self.assertIsNone(media_payload.parse_media(None))
        self.assertEqual(media_payload.parse_media({"kind": "image"}), MediaAttachment("image"))
        for status in ("missing", "unsupported"):
            media = MediaAttachment("sticker", status, reason="合成原因")
            self.assertEqual(media_payload.parse_media(asdict(media)), media)

    def test_remote_active_corrupt_and_mismatched_data_are_rejected(self):
        valid = asdict(media_payload.make_media(raster()))
        attacks = [
            {**valid, "mime_type": "image/svg+xml", "data_base64": base64.b64encode(b'<svg onload="alert(1)"/>').decode()},
            {**valid, "data_base64": "https://invalid.test/image.png"},
            {**valid, "data_base64": "data:image/png;base64," + valid["data_base64"]},
            {**valid, "data_base64": valid["data_base64"] + "\n"},
            {**valid, "data_base64": "☁"},
            {**valid, "mime_type": "image/jpeg"},
            {**valid, "data_base64": base64.b64encode(raster()[:25]).decode()},
            {**valid, "data_base64": base64.b64encode(b"<html>not an image</html>").decode()},
            {**valid, "data_base64": '" onerror="alert(1)'},
            {**valid, "url": "https://invalid.test/image.png"},
            {**valid, "kind": []},
            {**valid, "status": True},
            {**valid, "reason": {}},
            {**valid, "status": "missing"},
            {"kind": "image", "status": "available"},
            [],
            "https://invalid.test/image.png",
        ]
        for value in attacks:
            with self.subTest(value=value), self.assertRaises(ValueError):
                media_payload.parse_media(value)
        for content in (b'<svg xmlns="http://www.w3.org/2000/svg"/>', b"", b"https://invalid.test"):
            self.assertIsNone(media_payload.sniff_mime(content))
            with self.assertRaises(ValueError):
                media_payload.make_media(content)

    def test_decoded_size_dimensions_and_animation_budgets_are_enforced(self):
        image = raster()
        payload = media_payload.make_media(image)
        with patch.object(media_payload, "MAX_MEDIA_BYTES", len(image) - 1):
            with self.assertRaises(ValueError):
                media_payload.make_media(image)
            with self.assertRaises(ValueError):
                media_payload.parse_media(payload)
        with patch.object(media_payload, "MAX_IMAGE_PIXELS", 47), self.assertRaises(ValueError):
            media_payload.make_media(image)
        animated = raster("GIF", animated=True)
        with patch.object(media_payload, "MAX_ANIMATION_FRAMES", 1), self.assertRaises(ValueError):
            media_payload.make_media(animated, "sticker")
        with patch.object(media_payload, "MAX_ANIMATION_PIXELS", 95), self.assertRaises(ValueError):
            media_payload.make_media(animated, "sticker")


if __name__ == "__main__":
    unittest.main()
