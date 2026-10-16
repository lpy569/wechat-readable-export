"""Synthetic account trees only: media identity, fallbacks, and network boundaries."""

import base64
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import datetime
import hashlib
import io
import json
from pathlib import Path
import tempfile
import sqlite3
import unittest
from unittest.mock import MagicMock, patch
from urllib.request import Request

from PIL import Image

from wechat_readable_export import backend, cli, local_media
from wechat_readable_export.models import CHINA_TIME, Contact, Conversation, ExportError, Message


def raster(color: str = "#449966", *, animated: bool = False) -> bytes:
    first = Image.new("RGB", (12, 10), color)
    buffer = io.BytesIO()
    options = {}
    if animated:
        options = {"save_all": True, "append_images": [Image.new("RGB", first.size, "#eeaa55")], "duration": 100, "loop": 0}
    first.save(buffer, format="GIF" if animated else "PNG", **options)
    return buffer.getvalue()


class LocalMediaTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.account = self.root / "synthetic-account"
        self.account.mkdir()
        self.contact = "synthetic-contact"
        self.chat_hash = hashlib.md5(self.contact.encode()).hexdigest()
        self.time = int(datetime(2026, 1, 10, 12, tzinfo=CHINA_TIME).timestamp())
        self.month = "2026-01"

    def row(self, *, kind=3, local_id=7, shard="message/message_0.db", timestamp=None, server_id=1001, content="", packed=None):
        return {"_db_rel": shard, "local_id": local_id, "server_id": server_id,
                "create_time": self.time if timestamp is None else timestamp, "sort_seq": 1,
                "local_type": kind, "message_content": content, "packed_info_data": packed}

    def conversation(self, rows):
        messages = []
        for row in rows:
            kind = row["local_type"] & 0xFFFFFFFF
            messages.append(Message(
                timestamp=row["create_time"],
                time=datetime.fromtimestamp(row["create_time"], CHINA_TIME).strftime("%Y-%m-%d %H:%M:%S"),
                sender="合成联系人", sender_username=self.contact, sender_status="resolved", is_self=False,
                type="图片" if kind == 3 else "动画表情", type_code=row["local_type"],
                content="[图片]" if kind == 3 else "[动画表情]", source_db=row["_db_rel"],
                local_id=row["local_id"], server_id=row["server_id"], sort_seq=row["sort_seq"],
            ))
        return Conversation(Contact(self.contact, "合成联系人"), messages)

    def image_file(self, digest, data, *, suffix="_h.dat", contact_hash=None, month=None):
        path = (self.account / "msg" / "attach" / (contact_hash or self.chat_hash)
                / (month or self.month) / "Img" / (digest + suffix))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def thumbnail(self, data, *, local_id=7, timestamp=None, contact_hash=None):
        path = (self.account / "cache" / self.month / "Message" / (contact_hash or self.chat_hash)
                / "Thumb" / f"{local_id}_{self.time if timestamp is None else timestamp}_thumb.jpg")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def resolve(self, rows, **options):
        conversation = self.conversation(rows)
        resolver = local_media.LocalMediaResolver(self.account, self.contact, **options)
        self.addCleanup(resolver.close)
        resolver.attach(conversation, rows)
        return conversation

    def assert_bytes(self, media, expected):
        self.assertEqual(media.status, "available")
        self.assertEqual(base64.b64decode(media.data_base64), expected)

    def test_resource_hash_uses_the_encoded_field_not_arbitrary_hex_text(self):
        digest = "a1234567890bcdefa1234567890bcdef"
        marker = b"\x12\x22\x0a\x20"
        self.assertEqual(local_media.extract_resource_hashes(digest), [])
        self.assertEqual(local_media.extract_resource_hashes(b"filename=" + digest.encode()), [])
        self.assertEqual(local_media.extract_resource_hashes(marker + digest.upper().encode() + b"\x00" + marker + digest.encode()), [digest])
        self.assertEqual(local_media.extract_resource_hashes(marker + digest.encode() + b"\x32\x01x"), [digest])
        self.assertEqual(local_media.extract_resource_hashes(marker + b"z" * 32), [])

    def test_resource_hash_takes_precedence_over_mismatching_xml_hash(self):
        correct, wrong = raster(), raster("#882244")
        correct_hash, wrong_hash = hashlib.md5(correct).hexdigest(), hashlib.md5(wrong).hexdigest()
        self.image_file(correct_hash, correct)
        self.image_file(wrong_hash, wrong)
        row = self.row(content=f'<msg><img md5="{wrong_hash}"/></msg>')
        conversation = self.resolve([row], resource_hashes={7: [correct_hash]})
        self.assert_bytes(conversation.messages[0].media, correct)
        self.assertEqual(conversation.metadata["media_statistics"]["image_available"], 1)

    def test_packed_hash_precedes_xml_and_resolves_high_type_code(self):
        correct, wrong = raster(), raster("#882244")
        correct_hash, wrong_hash = hashlib.md5(correct).hexdigest(), hashlib.md5(wrong).hexdigest()
        self.image_file(correct_hash, correct)
        self.image_file(wrong_hash, wrong)
        row = self.row(kind=(1 << 32) + 3, content=f'<msg><img md5="{wrong_hash}"/></msg>',
                       packed=b"\x12\x22\x0a\x20" + correct_hash.encode())
        self.assert_bytes(self.resolve([row]).messages[0].media, correct)

    def test_images_and_thumbnails_from_other_chats_are_excluded(self):
        image = raster()
        digest = hashlib.md5(image).hexdigest()
        other = hashlib.md5(b"another-contact").hexdigest()
        self.image_file(digest, image, contact_hash=other)
        self.thumbnail(image, contact_hash=other)
        row = self.row(content=f'<msg><img md5="{digest}"/></msg>')
        self.assertEqual(self.resolve([row]).messages[0].media.status, "missing")

    def test_duplicate_local_id_in_different_shards_never_selects_resource_mapping(self):
        image = raster()
        digest = hashlib.md5(image).hexdigest()
        self.image_file(digest, image)
        rows = [self.row(), self.row(shard="message/message_1.db", server_id=1002, timestamp=self.time + 5)]
        conversation = self.resolve(rows, resource_hashes={7: [digest]})
        self.assertTrue(all(message.media.status == "missing" for message in conversation.messages))
        # A per-message hash still provides an unambiguous reference.
        rows[1]["message_content"] = f'<msg><img md5="{digest}"/></msg>'
        conversation = self.resolve(rows, resource_hashes={7: [digest]})
        self.assertEqual(conversation.messages[0].media.status, "missing")
        self.assert_bytes(conversation.messages[1].media, image)

    def test_duplicate_complete_row_keys_keep_their_distinct_xml_media(self):
        first, second = raster(), raster("#882244")
        first_hash, second_hash = hashlib.md5(first).hexdigest(), hashlib.md5(second).hexdigest()
        self.image_file(first_hash, first)
        self.image_file(second_hash, second)
        rows = [self.row(content=f'<msg><img md5="{first_hash}"/></msg>'),
                self.row(content=f'<msg><img md5="{second_hash}"/></msg>')]
        conversation = self.resolve(rows)
        self.assert_bytes(conversation.messages[0].media, first)
        self.assert_bytes(conversation.messages[1].media, second)

    def test_thumbnail_needs_exact_id_and_timestamp_instead_of_latest_file(self):
        expected, wrong = raster(), raster("#882244")
        self.thumbnail(wrong, timestamp=self.time - 1)
        self.thumbnail(wrong, timestamp=self.time + 100)
        self.thumbnail(wrong, local_id=8)
        self.assertEqual(self.resolve([self.row()]).messages[0].media.status, "missing")
        self.thumbnail(expected)
        media = self.resolve([self.row()]).messages[0].media
        self.assert_bytes(media, expected)
        self.assertIn("缩略图", media.reason)

    def test_duplicate_id_and_timestamp_across_shards_do_not_share_a_thumbnail(self):
        self.thumbnail(raster())
        rows = [self.row(), self.row(shard="message/message_1.db", server_id=1002)]
        conversation = self.resolve(rows)
        self.assertTrue(all(message.media.status == "missing" for message in conversation.messages))

    def test_key_provider_can_use_a_later_selected_sample_after_corrupt_v2(self):
        magic = b"\x07\x08\x56\x32\x08\x07"
        corrupt = magic + b"corrupt first sample" * 3
        encrypted = magic + b"valid second sample" * 3
        unrelated = magic + b"unselected sample" * 3
        first_hash, second_hash, unrelated_hash = "a" * 32, "b" * 32, "c" * 32
        self.image_file(first_hash, corrupt)
        self.image_file(second_hash, encrypted)
        self.image_file(unrelated_hash, unrelated)
        image = raster()
        aes, xor = b"0123456789abcdef", 0x55

        def key_provider(samples):
            self.assertEqual(samples[0], corrupt)
            self.assertIn(encrypted, samples)
            self.assertNotIn(unrelated, samples)
            self.assertLessEqual(len(samples), 8)
            return aes, xor

        def decode(data, aes_key=None, xor_key=None):
            if data == encrypted and (aes_key, xor_key) == (aes, xor):
                return image
            raise ValueError("synthetic invalid key or corrupt image")

        rows = [self.row(content=f'<msg><img md5="{first_hash}"/></msg>'),
                self.row(local_id=8, server_id=1002, content=f'<msg><img md5="{second_hash}"/></msg>')]
        provider = MagicMock(side_effect=key_provider)
        with patch.object(local_media, "decode_image", side_effect=decode):
            conversation = self.resolve(rows, key_provider=provider)
        provider.assert_called_once()
        self.assertEqual(conversation.messages[0].media.status, "unsupported")
        self.assert_bytes(conversation.messages[1].media, image)

    def test_corrupt_high_resolution_falls_back_to_lower_valid_image(self):
        image = raster()
        digest = hashlib.md5(image).hexdigest()
        self.image_file(digest, b"corrupt high resolution")
        self.image_file(digest, image, suffix=".dat")
        self.image_file(digest, raster("#882244"), suffix="_t.dat")
        row = self.row(content=f'<msg><img md5="{digest}"/></msg>')
        media = self.resolve([row]).messages[0].media
        self.assert_bytes(media, image)
        self.assertIsNone(media.reason)

    def test_unsupported_original_falls_back_to_thumbnail_with_visible_notice(self):
        image = raster()
        digest = hashlib.md5(image).hexdigest()
        self.image_file(digest, b"wxgf unsupported")
        self.image_file(digest, image, suffix="_t.dat")
        row = self.row(content=f'<msg><img md5="{digest}"/></msg>')
        media = self.resolve([row]).messages[0].media
        self.assert_bytes(media, image)
        self.assertIn("缩略图", media.reason)

    def test_local_animated_sticker_preserves_all_gif_frames(self):
        image = raster(animated=True)
        digest = hashlib.md5(image).hexdigest()
        path = self.account / "msg" / "emoji" / "pack" / (digest + ".gif")
        path.parent.mkdir(parents=True)
        path.write_bytes(image)
        row = self.row(kind=47, content=f'<msg><emoji md5="{digest}" cdnurl="https://mmbiz.qpic.cn/synthetic"/></msg>')
        with patch.object(local_media, "download_sticker") as download:
            media = self.resolve([row], download_stickers=True).messages[0].media
        download.assert_not_called()
        self.assert_bytes(media, image)
        self.assertEqual(media.kind, "sticker")
        self.assertEqual(media.mime_type, "image/gif")
        with Image.open(io.BytesIO(base64.b64decode(media.data_base64))) as decoded:
            self.assertEqual(decoded.n_frames, 2)

    def test_default_export_does_not_download_even_when_xml_has_cdn_urls(self):
        row = self.row(kind=47, content='<msg><emoji md5="' + "a" * 32 + '" cdnurl="https://mmbiz.qpic.cn/synthetic"/></msg>')
        with patch.object(local_media, "download_sticker") as download, patch.object(local_media, "build_opener") as opener:
            conversation = self.resolve([row])
        download.assert_not_called()
        opener.assert_not_called()
        self.assertEqual(conversation.messages[0].media.status, "missing")
        self.assertFalse(conversation.metadata["media_statistics"]["sticker_downloads_enabled"])

    def test_opt_in_download_requires_matching_md5_and_does_not_reuse_conflicting_cache(self):
        image = raster(animated=True)
        digest = hashlib.md5(image).hexdigest()
        url = "https://mmbiz.qpic.cn/synthetic"
        good = self.row(kind=47, content=f'<msg><emoji md5="{digest}" cdnurl="{url}"/></msg>')
        bad = self.row(kind=47, local_id=8, server_id=1002,
                       content=f'<msg><emoji md5="{"a" * 32}" cdnurl="{url}"/></msg>')
        with patch.object(local_media, "download_sticker", return_value=image) as download:
            conversation = self.resolve([good, bad], download_stickers=True)
        self.assert_bytes(conversation.messages[0].media, image)
        self.assertEqual(conversation.messages[1].media.status, "unsupported")
        self.assertGreaterEqual(download.call_count, 1)

    def test_unsafe_starting_urls_and_redirects_are_rejected_before_network(self):
        unsafe = ["http://127.0.0.1/a", "http://169.254.169.254/latest/meta-data/", "https://localhost/a",
                  "file:///etc/passwd", "https://mmbiz.qpic.cn.attacker.test/a", "https://attacker-qpic.cn/a",
                  "https://user:password@mmbiz.qpic.cn/a", "https://mmbiz.qpic.cn:444/a", "https://mmbiz.qpic.cn/\na"]
        redirects = local_media._CdnRedirects()
        for url in unsafe:
            with self.subTest(url=url), patch.object(local_media, "build_opener") as opener:
                with self.assertRaises(ValueError):
                    local_media.download_sticker(url)
                opener.assert_not_called()
                with self.assertRaises(ValueError):
                    redirects.redirect_request(Request("https://mmbiz.qpic.cn/good"), None, 302, "Found", {}, url)
        self.assertEqual(local_media._safe_cdn_url("http://mmbiz.qpic.cn/a?token=synthetic#fragment"),
                         "https://mmbiz.qpic.cn/a?token=synthetic")

    def test_download_reads_are_bounded_and_http_is_upgraded(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"x" * 65
        opener = MagicMock()
        opener.open.return_value = response
        with patch.object(local_media, "MAX_FILE_BYTES", 64), patch.object(local_media, "build_opener", return_value=opener):
            with self.assertRaises(ValueError):
                local_media.download_sticker("http://mmbiz.qpic.cn/synthetic")
        response.read.assert_called_once_with(65)
        self.assertEqual(opener.open.call_args.args[0].full_url, "https://mmbiz.qpic.cn/synthetic")

    def test_outside_account_and_oversized_files_are_rejected(self):
        image = raster()
        outside = self.root / "outside.png"
        outside.write_bytes(image)
        with self.assertRaises(ValueError):
            local_media._read_file(self.account.resolve(), outside)
        digest = hashlib.md5(image).hexdigest()
        path = self.image_file(digest, image)
        with patch.object(local_media, "MAX_FILE_BYTES", len(image) - 1):
            with self.assertRaises(ValueError):
                local_media._read_file(self.account.resolve(), path)
            media = self.resolve([self.row(content=f'<msg><img md5="{digest}"/></msg>')]).messages[0].media
        self.assertEqual(media.status, "unsupported")

    def test_symlink_to_media_outside_account_is_not_followed(self):
        image = raster()
        digest = hashlib.md5(image).hexdigest()
        outside = self.root / "outside.png"
        outside.write_bytes(image)
        link = self.account / "msg" / "attach" / self.chat_hash / self.month / "Img" / (digest + "_h.dat")
        link.parent.mkdir(parents=True)
        try:
            link.symlink_to(outside)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows does not grant symlink creation")
            raise
        with self.assertRaises(ValueError):
            local_media._read_file(self.account.resolve(), link)
        media = self.resolve([self.row(content=f'<msg><img md5="{digest}"/></msg>')]).messages[0].media
        self.assertEqual(media.status, "missing")

    def test_resource_database_scopes_hashes_to_chat_and_keeps_ambiguity(self):
        database = self.root / "synthetic-resource.db"
        marker = b"\x12\x22\x0a\x20"
        first, second, foreign = "a" * 32, "b" * 32, "c" * 32
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE ChatName2Id (user_name TEXT)")
            connection.executemany("INSERT INTO ChatName2Id (rowid, user_name) VALUES (?, ?)",
                                   [(1, self.contact), (2, "another-contact")])
            connection.execute("CREATE TABLE MessageResourceInfo (chat_id, message_local_id, packed_info)")
            connection.executemany("INSERT INTO MessageResourceInfo VALUES (?, ?, ?)", [
                (1, 7, marker + first.encode()), (1, 7, marker + first.encode()),
                (1, 7, marker + second.encode()), (2, 7, marker + foreign.encode()),
                (1, 0, marker + foreign.encode()), (1, -1, marker + foreign.encode()),
            ])
        original = database.read_bytes()
        hashes = backend._read_image_resource_hashes(database, self.contact)
        self.assertEqual(hashes, {7: [first, second]})
        self.assertEqual(backend._read_image_resource_hashes(database, "not-found"), {})
        self.assertEqual(database.read_bytes(), original)
        # The resource helper uses the same connection policy as all snapshots.
        readonly = backend._connect_readonly(database)
        try:
            with self.assertRaises(sqlite3.OperationalError):
                readonly.execute("DELETE FROM MessageResourceInfo")
        finally:
            readonly.close()
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("INSERT INTO ChatName2Id (user_name) VALUES (?)", (self.contact,))
        self.assertEqual(backend._read_image_resource_hashes(database, self.contact), {})

    def test_resource_database_schema_error_preserves_the_database(self):
        database = self.root / "synthetic-unsupported.db"
        with closing(sqlite3.connect(database)) as connection, connection:
            connection.execute("CREATE TABLE ChatName2Id (unsupported_column TEXT)")
        original = database.read_bytes()
        with self.assertRaisesRegex(ExportError, "结构"):
            backend._read_image_resource_hashes(database, self.contact)
        self.assertEqual(database.read_bytes(), original)
        self.assertFalse(Path(str(database) + "-journal").exists())

    def test_conflicting_media_cli_flags_fail_before_reader_access(self):
        output = self.root / "never-created"
        error = io.StringIO()
        with patch.object(backend, "LocalReader") as reader, redirect_stdout(io.StringIO()), redirect_stderr(error):
            result = cli.main(["export", "--contact-id", self.contact, "--no-media", "--download-stickers",
                               "--output", str(output)])
        self.assertEqual(result, 1)
        reader.assert_not_called()
        self.assertIn("不能", error.getvalue())
        self.assertFalse(output.exists())

    def test_no_media_cli_does_not_call_media_reader(self):
        output = self.root / "text-only"
        reader = MagicMock()
        reader.read_conversation.return_value = (Contact(self.contact, "合成联系人"), "synthetic-self", [self.row()], {})
        context = MagicMock()
        context.__enter__.return_value = reader
        with patch.object(backend, "LocalReader", return_value=context), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            result = cli.main(["export", "--contact-id", self.contact, "--no-media", "--output", str(output)])
        self.assertEqual(result, 0)
        reader.attach_media.assert_not_called()
        exported = json.loads((output / "chat.json").read_text(encoding="utf-8"))
        self.assertIsNone(exported["messages"][0]["media"])


if __name__ == "__main__":
    unittest.main()
