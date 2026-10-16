"""Synthetic parser fixtures; these tests contain no real WeChat data."""

from datetime import datetime, timezone
import json
import unittest

from wechat_readable_export.models import CHINA_TIME, Contact, ExportError
from wechat_readable_export.parsing import build_conversation

try:
    import zstandard
except ImportError:
    zstandard = None


CONTACT = Contact("wxid_test_peer", nickname="林同学测试", remark="林同学🌿")
SELF = "wxid_test_self"
EPOCH = 1_700_000_000


def row(text="合成消息", **changes):
    result = {
        "create_time": EPOCH, "local_type": 1,
        "message_content": text, "local_id": 1, "server_id": 1001,
        "_db_rel": "message/message_0.db",
        "_sender_username": CONTACT.username, "_sender_status": "resolved",
    }
    result.update(changes)
    return result


class ParsingTests(unittest.TestCase):
    def parse(self, rows, **kwargs):
        return build_conversation(CONTACT, SELF, rows, **kwargs)

    def test_plain_text_is_literal_including_markup_and_entities(self):
        text = '  <script>alert("synthetic")</script> &amp; <msg>字面文字</msg>\n尾行  '
        result = self.parse([row(text)])
        self.assertEqual(result.messages[0].content, text)
        self.assertEqual(result.messages[0].sender, CONTACT.display_name)
        self.assertEqual(result.warnings, [])

    @unittest.skipIf(zstandard is None, "zstandard dependency unavailable")
    def test_zstd_fallback_recovers_chinese_without_declared_size(self):
        compressed = zstandard.ZstdCompressor(write_content_size=False).compress("压缩的合成中文\n第二行".encode())
        result = self.parse([row("", compress_content=compressed)])
        self.assertEqual(result.messages[0].content, "压缩的合成中文\n第二行")
        self.assertEqual(result.warnings, [])

    def test_damaged_primary_text_can_use_readable_fallback(self):
        result = self.parse([row(b"\xff\xfe", compress_content="可恢复的备用正文")])
        self.assertEqual(result.messages[0].content, "可恢复的备用正文")
        self.assertTrue(any("UTF-8" in item for item in result.warnings))

    def test_failed_compression_and_missing_text_are_visible(self):
        result = self.parse([row(b"\x28\xb5\x2f\xfdinvalid")])
        self.assertIn("正文未保存在本机或解码失败", result.messages[0].content)
        self.assertTrue(any("Zstandard" in item for item in result.warnings))
        self.assertTrue(any("缺少可恢复正文" in item for item in result.warnings))

    def test_bad_utf8_is_retained_with_replacement_and_warning(self):
        result = self.parse([row(b"before\xffafter")])
        self.assertEqual(result.messages[0].content, "before\ufffdafter")
        self.assertTrue(any("UTF-8" in item for item in result.warnings))

    def test_reference_recovery_and_link_keep_readable_content(self):
        quote = '<msg><appmsg><type>57</type><title>这是回复</title><refermsg><type>1</type><displayname>引用名</displayname><content>原文 &lt;b&gt;仍为文字&lt;/b&gt;</content></refermsg></appmsg></msg>'
        link = '<msg><appmsg><type>5</type><title>标题</title><des>描述</des><url>https://example.invalid/?a=1&amp;b=2</url></appmsg></msg>'
        result = self.parse([row(quote, local_type=49), row(link, local_type=49, local_id=2, server_id=1002)])
        self.assertIn("这是回复\n    ↳ 引用 引用名：原文 <b>仍为文字</b>", result.messages[0].content)
        self.assertIn("https://example.invalid/?a=1&b=2", result.messages[1].content)

    def test_file_location_call_and_revocation_are_readable(self):
        messages = [
            row('<msg><appmsg><type>6</type><title>测试文件.txt</title></appmsg></msg>', local_type=49),
            row('<msg><location x="31.2" y="121.5" label="测试路" poiname="测试点"/></msg>', local_type=48, local_id=2, server_id=1002),
            row('<msg><voipmsg><VoIPBubbleMsg><msg>通话时长 00:12</msg></VoIPBubbleMsg></voipmsg></msg>', local_type=50, local_id=3, server_id=1003),
            row('<sysmsg type="revokemsg"><revokemsg><replacemsg><![CDATA[对方撤回了一条消息]]></replacemsg></revokemsg></sysmsg>', local_type=10000, local_id=4, server_id=1004),
        ]
        result = self.parse(messages)
        self.assertIn("测试文件.txt", result.messages[0].content)
        self.assertIn("测试点", result.messages[1].content)
        self.assertIn("31.2, 121.5", result.messages[1].content)
        self.assertIn("通话时长 00:12", result.messages[2].content)
        self.assertEqual(result.messages[3].sender, "系统消息")
        self.assertEqual(result.messages[3].content, "[撤回] 对方撤回了一条消息")

    def test_malicious_xml_is_literal_and_never_resolves_entities(self):
        malicious = '<!DOCTYPE msg [<!ENTITY x SYSTEM "file:///nonexistent-private-test">]><msg><appmsg><type>5</type><title>&x;</title></appmsg></msg>'
        result = self.parse([row(malicious, local_type=49)])
        self.assertIn(malicious, result.messages[0].content)
        self.assertTrue(any("实体声明" in item for item in result.warnings))

    def test_native_voice_transcripts_preserve_chinese_and_json_escapes(self):
        result = self.parse([
            row('<msg><voicetrans transtext="你好，合成中文 &amp; 字符" /></msg>', local_type=34),
            row("", local_type=34, local_id=2, server_id=1002, source=json.dumps({"transtext": "中文\n下一行😀"}, ensure_ascii=False)),
            row("", local_type=34, local_id=3, server_id=1003, packed_info_data=b'{"transtext":"\\u4f60\\u597d"}'),
        ])
        self.assertEqual(result.messages[0].transcript, "你好，合成中文 & 字符")
        self.assertEqual(result.messages[1].transcript, "中文\n下一行😀")
        self.assertEqual(result.messages[2].transcript, "你好")
        self.assertEqual(result.metadata["parser_statistics"]["native_voice_transcript_count"], 3)

    def test_same_numeric_sender_id_is_resolved_by_supplied_shard_mapping(self):
        result = self.parse([
            row("对方发言", real_sender_id=7),
            row("我发言", _db_rel="message/message_1.db", _sender_username=SELF, real_sender_id=7, local_id=2, server_id=1002),
        ])
        self.assertEqual([m.sender for m in result.messages], [CONTACT.display_name, "我"])
        self.assertEqual([m.is_self for m in result.messages], [False, True])
        self.assertEqual(result.warnings, [])

    def test_unknown_sender_cannot_be_guessed_from_id_or_body(self):
        result = self.parse([row(SELF + ": 合成正文", real_sender_id=1, _sender_username=None, _sender_status="unmapped_sender_id")])
        message = result.messages[0]
        self.assertEqual(message.sender, "未知发送者")
        self.assertIsNone(message.is_self)
        self.assertTrue(message.content.startswith(SELF))
        self.assertTrue(any("发送者未能确认" in item for item in result.warnings))

    def test_private_sender_outside_both_accounts_is_flagged(self):
        result = self.parse([row(_sender_username="wxid_unrelated_test")])
        self.assertEqual(result.messages[0].sender_status, "unexpected_private_sender")
        self.assertEqual(result.messages[0].sender, "未知发送者")

    def test_group_prefix_is_removed_only_when_it_matches_resolved_username(self):
        group = Contact("synthetic_group@chatroom", nickname="测试群")
        result = build_conversation(group, SELF, [
            row(CONTACT.username + ":\n合成群文本"),
            row("wxid_other_test:\n保留此前缀", local_id=2, server_id=1002),
            row(CONTACT.username + ":\n身份未确认不删除", local_id=3, server_id=1003, _sender_status="missing_name2id"),
        ])
        self.assertEqual(result.messages[0].content, "合成群文本")
        self.assertTrue(result.messages[1].content.startswith("wxid_other_test:"))
        self.assertTrue(result.messages[2].content.startswith(CONTACT.username + ":"))

    def test_chronology_uses_timestamp_and_preserves_duplicates(self):
        result = self.parse([
            row("后", create_time=(EPOCH + 2) * 1000, sort_seq=1),
            row("前", create_time=EPOCH, sort_seq=99, local_id=2),
            row("中", create_time=(EPOCH + 1) * 1000 + 125, sort_seq=2, local_id=3),
        ])
        self.assertEqual([m.content for m in result.messages], ["前", "中", "后"])
        self.assertEqual(result.messages[1].timestamp, EPOCH + 1.125)
        self.assertTrue(result.messages[1].time.endswith(".125"))
        self.assertEqual(len(result.messages), 3)
        self.assertTrue(any("重复 server_id" in item and "均已保留" in item for item in result.warnings))

    def test_filter_is_start_inclusive_end_exclusive_with_milliseconds(self):
        result = self.parse([
            row("之前", create_time=EPOCH * 1000 + 124),
            row("开始", create_time=EPOCH * 1000 + 125, local_id=2, server_id=1002),
            row("结束", create_time=EPOCH * 1000 + 126, local_id=3, server_id=1003),
        ], start=datetime.fromtimestamp(EPOCH + .125, timezone.utc), end=datetime.fromtimestamp(EPOCH + .126, timezone.utc))
        self.assertEqual([m.content for m in result.messages], ["开始"])
        self.assertEqual(result.metadata["parser_statistics"]["filtered_out_count"], 2)

    def test_naive_filter_dates_and_output_use_beijing_time(self):
        local = datetime.fromtimestamp(EPOCH, CHINA_TIME).replace(tzinfo=None)
        result = self.parse([row()], start=local)
        self.assertEqual(len(result.messages), 1)
        self.assertEqual(result.messages[0].time, local.strftime("%Y-%m-%d %H:%M:%S"))

    def test_invalid_time_and_reversed_interval_raise_actionable_error(self):
        for value in (None, True, "not-a-date", float("nan"), float("inf"), -1, 10 ** 50):
            with self.subTest(value=value), self.assertRaisesRegex(ExportError, "create_time 无效"):
                self.parse([row(create_time=value)])
        with self.assertRaisesRegex(ExportError, "开始时间必须早于"):
            self.parse([row()], start=datetime(2026, 1, 2), end=datetime(2026, 1, 1))

    def test_noninteger_raw_type_and_encoded_subtype_are_safe(self):
        result = self.parse([
            row("未知类型的可读文字", local_type={"invalid": "fixture"}),
            row('<msg><appmsg><type>6</type><title>高位编码文件.txt</title></appmsg></msg>', local_type=(1 << 32) + 49, local_id=2, server_id=1002),
        ])
        self.assertEqual(result.messages[0].type_code, -1)
        self.assertIn("未知类型的可读文字", result.messages[0].content)
        self.assertIn("高位编码文件.txt", result.messages[1].content)
        self.assertTrue(any("local_type 无效" in item for item in result.warnings))

    def test_unknown_and_malformed_cards_keep_original_payload(self):
        text = '<msg><appmsg><type>987</type><title>未来卡片</title><unknown>保留文字</unknown></appmsg></msg>'
        result = self.parse([row(text, local_type=49)])
        self.assertIn(text, result.messages[0].content)
        malformed = '<msg><appmsg><type>5</type>未闭合'
        result = self.parse([row(malformed, local_type=49)])
        self.assertIn(malformed, result.messages[0].content)
        self.assertTrue(any("XML 解析失败" in item for item in result.warnings))

    def test_merged_chat_and_miniprogram_keep_available_text(self):
        merged = '<msg><appmsg><type>19</type><title>合成合并记录</title><recorditem><![CDATA[<recordinfo><datalist><dataitem datatype="1"><sourcename>合成发言人</sourcename><sourcetime>2026-01-01 12:00</sourcetime><datadesc>合成引用文字</datadesc></dataitem></datalist></recordinfo>]]></recorditem></appmsg></msg>'
        mini = '<msg><appmsg><type>33</type><title>合成小程序</title><url>https://example.invalid</url></appmsg></msg>'
        result = self.parse([row(merged, local_type=49), row(mini, local_type=49, local_id=2, server_id=1002)])
        self.assertIn("2026-01-01 12:00 | 合成发言人 | 合成引用文字", result.messages[0].content)
        self.assertTrue(result.messages[1].content.startswith("[小程序]"))


if __name__ == "__main__":
    unittest.main()
