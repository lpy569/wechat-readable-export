"""Exercise the export command with a synthetic reader, never live WeChat."""

from contextlib import contextmanager, redirect_stderr, redirect_stdout
import hashlib
from io import BytesIO, StringIO
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image

from wechat_readable_export.cli import load_json, main
from wechat_readable_export.local_media import LocalMediaResolver
from wechat_readable_export.models import Contact


class MediaExportTests(unittest.TestCase):
    def test_export_attaches_local_image_and_convert_preserves_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            contact = Contact("wxid_synthetic_friend", "演示好友")
            digest = "0123456789abcdef0123456789abcdef"
            chat_hash = hashlib.md5(contact.username.encode()).hexdigest()
            image_dir = root / "account" / "msg" / "attach" / chat_hash / "2026-01" / "Img"
            image_dir.mkdir(parents=True)
            buffer = BytesIO()
            Image.new("RGB", (12, 12), "green").save(buffer, format="PNG")
            image_dir.joinpath(digest + ".dat").write_bytes(buffer.getvalue())
            rows = [{"local_id": 1, "server_id": 123, "local_type": 3, "sort_seq": 1,
                     "create_time": 1768006800, "message_content": f'<msg><img md5="{digest}"/></msg>',
                     "_db_rel": "message/message_0.db", "_sender_username": contact.username,
                     "_sender_status": "resolved"}]
            calls = []

            class SyntheticReader:
                def read_conversation(self, contact_id):
                    self_outer.assertEqual(contact_id, contact.username)
                    return contact, "wxid_synthetic_self", rows, {}

                def attach_media(self, conversation, supplied_rows, *, download_stickers):
                    calls.append(download_stickers)
                    resolver = LocalMediaResolver(root / "account", contact.username,
                                                  download_stickers=download_stickers)
                    try:
                        resolver.attach(conversation, supplied_rows)
                    finally:
                        resolver.close()

            self_outer = self

            @contextmanager
            def reader(*args, **kwargs):
                yield SyntheticReader()

            with patch("wechat_readable_export.backend.LocalReader", reader), redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(["export", "--contact-id", contact.username, "--output", str(root / "export")]), 0)
                self.assertEqual(main(["convert", str(root / "export" / "chat.json"), "--output", str(root / "converted")]), 0)
                self.assertEqual(main(["export", "--contact-id", contact.username, "--no-media", "--output", str(root / "text")]), 0)
            self.assertEqual(calls, [False])
            loaded = load_json(root / "converted" / "chat.json")
            self.assertEqual(loaded.messages[0].media.status, "available")
            self.assertIn("data:image/png;base64,", (root / "converted" / "chat.html").read_text(encoding="utf-8"))
            self.assertIsNone(load_json(root / "text" / "chat.json").messages[0].media)


if __name__ == "__main__":
    unittest.main()
