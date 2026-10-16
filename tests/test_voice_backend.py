"""Voice orchestration tests use synthetic snapshots and generated PCM only."""

from contextlib import closing
from datetime import datetime
import io
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
import wave

from wechat_readable_export import backend
from wechat_readable_export.models import CHINA_TIME, Contact, Conversation, ExportError, Message


def wav():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(bytes(4800))
    return buffer.getvalue()


class VoiceBackendTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.folder = self.root / "account" / "db_storage" / "message"
        self.folder.mkdir(parents=True)
        self.reader = backend._LocalReader(self.root / "account", self.root / "temporary")
        self.addCleanup(self.reader.close)

    def conversation(self, ids=(101,)):
        time = datetime(2026, 1, 1, tzinfo=CHINA_TIME)
        return Conversation(Contact("synthetic-friend"), [
            Message(timestamp=time.timestamp(), time=time.isoformat(), sender="合成好友",
                    sender_username="synthetic-friend", sender_status="resolved", is_self=False,
                    type="语音", type_code=34, content="[语音]", local_id=i, server_id=sid)
            for i, sid in enumerate(ids, 1)
        ])

    def shard(self, number=0, records=((101, b"synthetic silk"),)):
        path = self.folder / f"media_{number}.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
            connection.execute("INSERT INTO Name2Id VALUES ('synthetic-friend')")
            connection.execute("CREATE TABLE VoiceInfo(chat_name_id,svr_id,voice_data)")
            connection.executemany("INSERT INTO VoiceInfo VALUES (1,?,?)", records)
        return path

    def test_voice_discovery_only_accepts_numeric_regular_media_shards(self):
        for name in ("media_10.db", "media_2.db", "media_foo.db", "media_1.db-wal", "message_0.db"):
            (self.folder / name).touch()
        self.assertEqual([p.name for p in backend._voice_databases(self.reader.storage)],
                         ["media_2.db", "media_10.db"])

    def test_audio_attaches_without_modifying_original_messages_or_statistics(self):
        self.shard()
        conversation = self.conversation()
        conversation.metadata["media_statistics"] = {"image_available": 7, "sticker_downloads_enabled": False}
        with patch.object(self.reader, "_snapshot", side_effect=lambda p: p), \
             patch("wechat_readable_export.audio_codec.decode_voice", return_value=wav()) as decoder:
            self.reader.attach_voice(conversation)
        self.assertEqual(conversation.messages[0].content, "[语音]")
        self.assertEqual(conversation.messages[0].media.kind, "audio")
        self.assertEqual(conversation.messages[0].media.status, "available")
        self.assertEqual(conversation.metadata["media_statistics"]["image_available"], 7)
        self.assertFalse(conversation.metadata["media_statistics"]["sticker_downloads_enabled"])
        self.assertEqual(conversation.metadata["voice_resource_status"], "read")
        decoder.assert_called_once_with(b"synthetic silk")

    def test_partial_authentication_failure_does_not_claim_missing_audio_is_absent(self):
        good = self.shard(records=((101, b"synthetic silk"), (102, b"")))
        bad = self.folder / "media_1.db"
        bad.touch()
        conversation = self.conversation((101, 102))
        def snapshot(path):
            if path == bad:
                raise ExportError("synthetic authentication failure")
            return good
        with patch.object(self.reader, "_snapshot", side_effect=snapshot), \
             patch("wechat_readable_export.audio_codec.decode_voice", return_value=wav()):
            self.reader.attach_voice(conversation)
        self.assertEqual(conversation.messages[0].media.status, "available")
        self.assertEqual(conversation.messages[1].media.status, "missing")
        self.assertIn("无法确认", conversation.messages[1].media.reason)
        self.assertEqual(conversation.metadata["voice_resource_status"], "partial")

    def test_decode_failure_keeps_marker_and_clear_reason(self):
        self.shard()
        conversation = self.conversation()
        with patch.object(self.reader, "_snapshot", side_effect=lambda p: p), \
             patch("wechat_readable_export.audio_codec.decode_voice", side_effect=ValueError("合成音频损坏")):
            self.reader.attach_voice(conversation)
        self.assertEqual(conversation.messages[0].media.status, "unsupported")
        self.assertEqual(conversation.messages[0].content, "[语音]")
        self.assertEqual(conversation.messages[0].media.reason, "合成音频损坏")

    def test_no_voice_does_not_open_media_databases(self):
        self.shard()
        conversation = self.conversation(())
        with patch.object(self.reader, "_snapshot") as snapshot:
            self.reader.attach_voice(conversation)
        snapshot.assert_not_called()
        self.assertNotIn("voice_statistics", conversation.metadata)

    def test_missing_media_database_remains_readable(self):
        conversation = self.conversation()
        self.reader.attach_voice(conversation)
        self.assertEqual(conversation.metadata["voice_resource_status"], "missing")
        self.assertIn("没有语音媒体数据库", conversation.messages[0].media.reason)

    def test_unreadable_media_directory_does_not_abort_export(self):
        conversation = self.conversation()
        with patch.object(backend, "_voice_databases", side_effect=PermissionError("synthetic")), \
             patch.object(self.reader, "_snapshot") as snapshot:
            self.reader.attach_voice(conversation)
        snapshot.assert_not_called()
        self.assertEqual(conversation.metadata["voice_resource_status"], "unavailable")
        self.assertIn("无法确认", conversation.messages[0].media.reason)

    def test_invalid_server_ids_do_not_read_media_databases(self):
        conversation = self.conversation((None, 0))
        with patch.object(backend, "_voice_databases") as discover, \
             patch.object(self.reader, "_snapshot") as snapshot:
            self.reader.attach_voice(conversation)
        discover.assert_not_called()
        snapshot.assert_not_called()
        self.assertEqual(conversation.metadata["voice_resource_status"], "no_valid_ids")
        self.assertTrue(all("服务器消息 ID" in m.media.reason for m in conversation.messages))

    def test_changing_media_shard_set_disables_results_instead_of_guessing(self):
        shard = self.shard()
        conversation = self.conversation()
        with patch.object(self.reader, "_snapshot", return_value=shard), \
             patch.object(backend, "_voice_databases", side_effect=[[shard], []]), \
             patch("wechat_readable_export.audio_codec.decode_voice") as decoder:
            self.reader.attach_voice(conversation)
        decoder.assert_not_called()
        self.assertEqual(conversation.metadata["voice_resource_status"], "changed")
        self.assertIn("分片发生变化", conversation.messages[0].media.reason)


if __name__ == "__main__":
    unittest.main()
