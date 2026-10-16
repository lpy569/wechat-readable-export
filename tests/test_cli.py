from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import json
from pathlib import Path
import tempfile
import unittest

from wechat_readable_export.cli import date_range, load_json, main, parser, select_contact
from wechat_readable_export.demo import demo_conversation
from wechat_readable_export.models import Contact, ExportError


class CliTests(unittest.TestCase):
    def test_ambiguous_name_is_rejected_instead_of_first_match(self):
        contacts = [Contact("a", "同名"), Contact("b", "同名")]
        with self.assertRaisesRegex(ExportError, "多个"):
            select_contact(contacts, "同名")

    def test_exact_id_disambiguates(self):
        contacts = [Contact("a", "同名"), Contact("b", "同名")]
        self.assertEqual(select_contact(contacts, "b").username, "b")

    def test_date_end_includes_whole_day_in_beijing(self):
        start, end = date_range("2026-01-01", "2026-01-01")
        self.assertEqual((end - start).total_seconds(), 86400)
        self.assertEqual(start.utcoffset().total_seconds(), 28800)
        with self.assertRaises(ExportError):
            date_range("2026-02-02", "2026-01-01")
        with self.assertRaises(ExportError):
            date_range("not-a-date", None)

    def test_demo_and_convert_work_without_loading_windows_backend(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
                self.assertEqual(main(["demo", "--output", str(root / "demo")]), 0)
                self.assertEqual(main(["convert", str(root / "demo" / "chat.json"), "--output", str(root / "converted")]), 0)
                self.assertEqual(main(["demo", "--output", str(root / "demo")]), 1)
            original = load_json(root / "demo" / "chat.json")
            converted = load_json(root / "converted" / "chat.json")
            self.assertEqual([m.content for m in original.messages], [m.content for m in converted.messages])
            self.assertEqual(len(original.messages), 10)

    def test_unknown_json_schema_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "invalid.json"
            path.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
            with self.assertRaises(ExportError):
                load_json(path)

    def test_empty_target_or_date_is_rejected_before_database_access(self):
        for arguments in (["export", "--contact", ""], ["export", "--contact-id", " "],
                          ["export", "--contact", "演示", "--start", ""],
                          ["demo", "--output", ""]):
            with self.subTest(arguments=arguments), redirect_stderr(StringIO()), self.assertRaises(SystemExit):
                parser().parse_args(arguments)
        with self.assertRaises(ExportError):
            select_contact([Contact("a", "演示", "")], "")
        with self.assertRaises(ExportError):
            date_range("", None)

    def test_import_rejects_bool_coercion_nonfinite_times_and_invalid_fields(self):
        for field, value in (("is_self", 1), ("is_self", 0), ("timestamp", float("nan")),
                             ("timestamp", float("inf")), ("source_db", {}),
                             ("sort_seq", "1"), ("sender_username", []), ("time", "2026-99-99")):
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as temporary:
                data = demo_conversation().to_dict()
                data["messages"][0][field] = value
                path = Path(temporary) / "input.json"
                path.write_text(json.dumps(data), encoding="utf-8")
                with self.assertRaises(ExportError):
                    load_json(path)


if __name__ == "__main__":
    unittest.main()
