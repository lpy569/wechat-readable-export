"""Synthetic SQLite snapshots only; never reads a real WeChat account."""

from contextlib import closing
import hashlib
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from wechat_readable_export import local_voice


class LocalVoiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.contact = "synthetic-selected-contact"
        self.number = 0

    def shard(self, rows=(), *, names=None, schema=True, id_type=""):
        self.number += 1
        path = self.root / f"media_{self.number}.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            if schema:
                connection.execute("CREATE TABLE Name2Id(user_name TEXT)")
                connection.execute(
                    f"CREATE TABLE VoiceInfo(chat_name_id INTEGER, svr_id {id_type}, voice_data BLOB)"
                )
                connection.executemany("INSERT INTO Name2Id(rowid,user_name) VALUES (?,?)",
                                       names if names is not None else [(1, self.contact)])
                connection.executemany("INSERT INTO VoiceInfo VALUES (?,?,?)", rows)
            else:
                connection.execute("CREATE TABLE UnfamiliarSchema(value)")
        return (f"message/{path.name}", path)

    def read(self, shards, ids=(1001,)):
        return local_voice.read_voice_blobs(shards, self.contact, ids)

    def test_canonical_server_id_is_precision_safe(self):
        for value, expected in ((1, "1"), ("001", "1"), (-1, "-1"),
                                ((1 << 63) - 1, str((1 << 63) - 1)),
                                (1 << 63, str(-(1 << 63))),
                                (str((1 << 64) - 1), "-1"),
                                (str(-(1 << 63)), str(-(1 << 63)))):
            with self.subTest(value=value):
                self.assertEqual(local_voice.canonical_server_id(value), expected)
        for value in (None, True, False, 0, "0", "-0", "", " 1", "1 ", "+1", "1e3",
                      1.0, 1 << 64, -(1 << 63) - 1, "1" * 1000):
            with self.subTest(value=value):
                self.assertIsNone(local_voice.canonical_server_id(value))

    def test_exact_contact_and_requested_server_id_only(self):
        selected = b"#!SILK_V3synthetic"
        shard = self.shard([(1, 1001, selected), (1, 1002, b"unselected"),
                            (2, 1001, b"another conversation")],
                           names=[(1, self.contact), (2, "synthetic-other-contact")])
        blobs, diagnostics = self.read([shard])
        self.assertEqual(blobs, {"1001": selected})
        self.assertEqual(diagnostics["issues"], {})
        self.assertEqual(diagnostics["available"], 1)
        self.assertEqual(diagnostics["known_shards"], 1)

    def test_shard_contact_rowids_are_independent(self):
        first = self.shard([(1, 1001, b"first")])
        second = self.shard([(7, 1002, b"second"), (1, 1002, b"wrong")],
                            names=[(7, self.contact), (1, "another-contact")])
        blobs, diagnostics = self.read([first, second], (1001, 1002))
        self.assertEqual(blobs, {"1001": b"first", "1002": b"second"})
        self.assertEqual(diagnostics["shards_read"], 2)

    def test_missing_contact_cannot_use_same_server_id_from_another_chat(self):
        shard = self.shard([(1, 1001, b"wrong")], names=[(1, "other-contact")])
        blobs, diagnostics = self.read([shard])
        self.assertEqual(blobs, {})
        self.assertEqual(diagnostics["issues"], {"1001": "missing_session"})

    def test_contact_match_is_binary_even_if_column_collation_is_nocase(self):
        path = self.root / "collation.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.execute("CREATE TABLE Name2Id(user_name TEXT COLLATE NOCASE)")
            connection.execute("INSERT INTO Name2Id VALUES (?)", (self.contact.upper(),))
            connection.execute("CREATE TABLE VoiceInfo(chat_name_id,svr_id,voice_data)")
            connection.execute("INSERT INTO VoiceInfo VALUES (1,1001,?)", (b"wrong",))
        self.assertEqual(self.read([("media.db", path)])[0], {})

    def test_missing_blob_and_empty_blob_are_distinct(self):
        shard = self.shard([(1, 1001, b""), (1, 1002, None)])
        blobs, diagnostics = self.read([shard], (1001, 1002, 1003))
        self.assertEqual(blobs, {})
        self.assertEqual(diagnostics["issues"], {"1001": "empty_blob", "1002": "empty_blob",
                                                 "1003": "missing_blob"})

    def test_equal_duplicates_across_and_within_shards_are_accepted(self):
        first = self.shard([(1, 1001, b"same"), (1, 1001, b"same")])
        second = self.shard([(1, 1001, b"same")])
        self.assertEqual(self.read([first, second])[0], {"1001": b"same"})

    def test_conflicting_duplicates_across_and_within_shards_are_withheld(self):
        for across in (False, True):
            with self.subTest(across=across):
                if across:
                    shards = [self.shard([(1, 1001, b"one")]),
                              self.shard([(1, 1001, b"two")]),
                              self.shard([(1, 1001, b"one")])]
                else:
                    shards = [self.shard([(1, 1001, b"one"), (1, 1001, b"two"),
                                          (1, 1001, b"one")])]
                blobs, diagnostics = self.read(shards)
                self.assertEqual(blobs, {})
                self.assertEqual(diagnostics["issues"], {"1001": "ambiguous_blob"})

    def test_duplicate_contact_rows_are_all_considered_without_picking_first(self):
        shard = self.shard([(1, 1001, b"one"), (2, 1001, b"two")],
                           names=[(1, self.contact), (2, self.contact)])
        self.assertEqual(self.read([shard])[1]["issues"], {"1001": "ambiguous_blob"})

    def test_empty_duplicates_do_not_hide_valid_local_audio(self):
        first = self.shard([(1, 1001, b""), (1, 1001, b"valid")])
        second = self.shard([(1, 1001, None)])
        self.assertEqual(self.read([first, second])[0], {"1001": b"valid"})

    def test_malformed_text_blob_is_withheld_even_with_valid_duplicate(self):
        first = self.shard([(1, 1001, b"valid")])
        second = self.shard([(1, 1001, "not binary audio")])
        for shards in ([first, second], [second, first]):
            blobs, diagnostics = self.read(shards)
            self.assertEqual(blobs, {})
            self.assertEqual(diagnostics["issues"], {"1001": "invalid_blob"})

    def test_oversized_blob_is_not_materialized(self):
        shard = self.shard([(1, 1001, b"12345")])
        with patch.object(local_voice, "MAX_VOICE_BYTES", 4):
            blobs, diagnostics = self.read([shard])
        self.assertEqual(blobs, {})
        self.assertEqual(diagnostics["issues"], {"1001": "oversized_blob"})

    def test_total_source_budget_preserves_fitting_audio_and_marks_excess(self):
        shard = self.shard([(1, 1001, b"1234"), (1, 1002, b"5678"),
                            (1, 1003, b"12")])
        with patch.object(local_voice, "MAX_TOTAL_VOICE_BYTES", 6):
            blobs, diagnostics = self.read([shard], (1001, 1002, 1003))
        self.assertEqual(blobs, {"1001": b"1234", "1003": b"12"})
        self.assertEqual(diagnostics["issues"], {"1002": "resource_limit"})
        self.assertEqual(diagnostics["source_bytes"], 6)
        self.assertEqual(diagnostics["resource_limited"], 1)
        self.assertEqual(diagnostics["shards_read"], 1)
        self.assertEqual(diagnostics["shards_unreadable"], 0)
        self.assertEqual(len(diagnostics["warnings"]), 1)

    def test_total_budget_covers_completed_and_staged_shards_together(self):
        first = self.shard([(1, 1001, b"1234")])
        second = self.shard([(1, 1002, b"5678")])
        with patch.object(local_voice, "MAX_TOTAL_VOICE_BYTES", 6):
            blobs, diagnostics = self.read([first, second], (1001, 1002))
        self.assertEqual(blobs, {"1001": b"1234"})
        self.assertEqual(diagnostics["issues"], {"1002": "resource_limit"})
        self.assertEqual(diagnostics["source_bytes"], 4)

    def test_equal_duplicates_do_not_consume_total_budget_twice(self):
        first = self.shard([(1, 1001, b"1234"), (1, 1001, b"1234")])
        second = self.shard([(1, 1001, b"1234"), (1, 1002, b"56")])
        with patch.object(local_voice, "MAX_TOTAL_VOICE_BYTES", 6):
            blobs, diagnostics = self.read([first, second], (1001, 1002))
        self.assertEqual(blobs, {"1001": b"1234", "1002": b"56"})
        self.assertEqual(diagnostics["source_bytes"], 6)
        self.assertEqual(diagnostics["resource_limited"], 0)
        self.assertEqual(diagnostics["issues"], {})

    def test_ambiguous_staged_blob_releases_its_budget(self):
        shard = self.shard([(1, 1001, b"1234"), (1, 1001, b"5678"),
                            (1, 1002, b"abcd")])
        with patch.object(local_voice, "MAX_TOTAL_VOICE_BYTES", 4):
            blobs, diagnostics = self.read([shard], (1001, 1002))
        self.assertEqual(blobs, {"1002": b"abcd"})
        self.assertEqual(diagnostics["issues"], {"1001": "ambiguous_blob"})
        self.assertEqual(diagnostics["source_bytes"], 4)

    def test_conflict_with_completed_shard_needs_no_second_retained_copy(self):
        first = self.shard([(1, 1001, b"1234")])
        conflict = self.shard([(1, 1001, b"5678")])
        last = self.shard([(1, 1002, b"abcd")])
        with patch.object(local_voice, "MAX_TOTAL_VOICE_BYTES", 4):
            blobs, diagnostics = self.read([first, conflict, last], (1001, 1002))
        self.assertEqual(blobs, {"1002": b"abcd"})
        self.assertEqual(diagnostics["issues"], {"1001": "ambiguous_blob"})
        self.assertEqual(diagnostics["source_bytes"], 4)
        self.assertEqual(diagnostics["resource_limited"], 0)

    def test_failed_shard_releases_staged_budget_without_affecting_valid_shard(self):
        broken = self.shard([(1, 1001, b"1234"), (1, 1002, b"5678")])
        good = self.shard([(1, 1003, b"abcd")])
        with patch.object(local_voice, "MAX_TOTAL_VOICE_BYTES", 4), \
             patch.object(local_voice, "MAX_MATCHED_ROWS", 1):
            blobs, diagnostics = self.read([broken, good], (1001, 1002, 1003))
        self.assertEqual(blobs, {"1003": b"abcd"})
        self.assertEqual(diagnostics["source_bytes"], 4)
        self.assertEqual(diagnostics["shards_unreadable"], 1)

    def test_retained_stage_plus_completed_bytes_never_exceed_budget(self):
        first = self.shard([(1, 1001, b"1234"), (1, 1001, b"1234")])
        second = self.shard([(1, 1001, b"1234"), (1, 1002, b"56"),
                            (1, 1003, b"789"), (1, 1002, b"AB")])
        original = local_voice._stage_blob
        retained = []
        def observed(staged, staged_issues, blobs, *args):
            result = original(staged, staged_issues, blobs, *args)
            total = sum(map(len, staged.values())) + sum(map(len, blobs.values()))
            retained.append(total)
            self.assertLessEqual(total, 6)
            return result
        with patch.object(local_voice, "MAX_TOTAL_VOICE_BYTES", 6), \
             patch.object(local_voice, "_stage_blob", side_effect=observed):
            blobs, diagnostics = self.read([first, second], (1001, 1002, 1003))
        self.assertEqual(max(retained), 6)
        self.assertEqual(diagnostics["source_bytes"], sum(map(len, blobs.values())))

    def test_unsigned_json_id_matches_signed_sqlite_integer(self):
        shard = self.shard([(1, -123, b"correct")], id_type="INTEGER")
        self.assertEqual(self.read([shard], (str((1 << 64) - 123),))[0], {"-123": b"correct"})

    def test_signed_id_matches_unsigned_text_storage(self):
        shard = self.shard([(1, str((1 << 64) - 123), b"correct")], id_type="TEXT")
        self.assertEqual(self.read([shard], (-123,))[0], {"-123": b"correct"})

    def test_float_id_and_malformed_numeric_text_are_never_coerced_to_message(self):
        shard = self.shard([(1, 1001.5, b"wrong"), (1, "1001junk", b"wrong")])
        self.assertEqual(self.read([shard])[0], {})

    def test_partial_shards_preserve_valid_audio_and_report_uncertain_missing(self):
        good = self.shard([(1, 1001, b"valid")])
        bad = self.shard(schema=False)
        missing = ("missing.db", self.root / "does-not-exist.db")
        corrupt_path = self.root / "corrupt.db"
        corrupt_path.write_bytes(b"invalid SQLite file")
        blobs, diagnostics = self.read([good, bad, missing, ("bad.db", corrupt_path)], (1001, 1002))
        self.assertEqual(blobs, {"1001": b"valid"})
        self.assertEqual(diagnostics["issues"], {"1002": "unavailable_shards"})
        self.assertEqual(diagnostics["shards_read"], 1)
        self.assertEqual(diagnostics["shards_unsupported"], 1)
        self.assertEqual(diagnostics["shards_unreadable"], 2)
        self.assertEqual(len(diagnostics["warnings"]), 1)
        self.assertFalse(missing[1].exists())
        self.assertNotIn(str(self.root), str(diagnostics))

    def test_matching_row_limit_discards_partial_shard(self):
        shard = self.shard([(1, 1001, b"first"), (1, 1002, b"second")])
        with patch.object(local_voice, "MAX_MATCHED_ROWS", 1):
            blobs, diagnostics = self.read([shard], (1001, 1002))
        self.assertEqual(blobs, {})
        self.assertEqual(diagnostics["shards_read"], 0)
        self.assertEqual(diagnostics["shards_unreadable"], 1)
        self.assertEqual(set(diagnostics["issues"].values()), {"unavailable_shards"})

    def test_too_many_contact_mappings_are_rejected(self):
        shard = self.shard([(1, 1001, b"first")], names=[(1, self.contact), (2, self.contact)])
        with patch.object(local_voice, "MAX_CONTACT_ROWS", 1):
            blobs, diagnostics = self.read([shard])
        self.assertEqual(blobs, {})
        self.assertEqual(diagnostics["shards_unreadable"], 1)

    def test_query_batches_accept_more_than_sqlite_variable_limit(self):
        rows = [(1, value, str(value).encode()) for value in range(1, 650)]
        shard = self.shard(rows)
        blobs, diagnostics = self.read([shard], range(1, 650))
        self.assertEqual(len(blobs), 649)
        self.assertEqual(diagnostics["issues"], {})

    def test_input_snapshots_are_never_modified(self):
        shard = self.shard([(1, 1001, b"audio")])
        before = hashlib.sha256(shard[1].read_bytes()).digest()
        self.read([shard])
        self.assertEqual(hashlib.sha256(shard[1].read_bytes()).digest(), before)

    def test_invalid_or_empty_selection_reads_no_database(self):
        invalid_path = self.root / "missing.db"
        blobs, diagnostics = self.read([("missing", invalid_path)], (None, True, 0, 1.5))
        self.assertEqual(blobs, {})
        self.assertEqual(diagnostics["selected"], 0)
        self.assertEqual(diagnostics["shards_unreadable"], 0)


if __name__ == "__main__":
    unittest.main()
