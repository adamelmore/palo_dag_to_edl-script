"""Unit tests for dag_to_edl parse/merge/write helpers."""

import tempfile
import unittest
from pathlib import Path

import dag_to_edl as m


class TestParseComment(unittest.TestCase):
    def test_parse_roundtrip(self):
        meta = m.EntryMeta(
            dag="G1", orig="2026-01-01", last="2026-06-15", cnt=3
        )
        tail = m.build_comment(meta)
        parsed = m.parse_comment_tail(tail)
        self.assertEqual(parsed, meta)

    def test_parse_with_hash_prefix(self):
        parsed = m.parse_comment_tail("# dag=G|orig=2026-01-01|last=2026-01-02|cnt=1")
        self.assertEqual(
            parsed, m.EntryMeta(dag="G", orig="2026-01-01", last="2026-01-02", cnt=1)
        )

    def test_invalid_cnt(self):
        self.assertIsNone(
            m.parse_comment_tail("# dag=G|orig=2026-01-01|last=2026-01-02|cnt=0")
        )


class TestSplitLine(unittest.TestCase):
    def test_no_comment(self):
        ind, tail = m.split_edl_line("10.0.0.1\n")
        self.assertEqual(ind, "10.0.0.1")
        self.assertIsNone(tail)

    def test_with_comment(self):
        ind, tail = m.split_edl_line(
            "10.0.0.0/24 # dag=G|orig=2026-01-01|last=2026-01-02|cnt=1\n"
        )
        self.assertEqual(ind, "10.0.0.0/24")
        self.assertTrue(tail.startswith("#"))


class TestParseEdlFile(unittest.TestCase):
    def test_verbatim_preserved_for_bad_meta(self):
        lines = [
            "10.0.0.1 # dag=G|orig=2026-01-01|last=2026-01-02|cnt=1\n",
            "10.0.0.2   garbage comment\n",
        ]
        parsed, verbatim, warns = m.parse_edl_file(lines)
        self.assertIn("10.0.0.1", parsed)
        self.assertIn("10.0.0.2", verbatim)
        self.assertTrue(any("unparseable" in w for w in warns))


class TestMerge(unittest.TestCase):
    def test_new_only(self):
        today = "2026-06-20"
        existing = {}
        fetch = {"10.0.0.1": {"A"}, "10.0.0.2": {"B", "Z"}}
        out = m.merge_edl(existing, fetch, today)
        self.assertEqual(out["10.0.0.1"].cnt, 1)
        self.assertEqual(out["10.0.0.1"].orig, today)
        self.assertEqual(out["10.0.0.2"].dag, "B")  # min of B, Z

    def test_repeat_increment(self):
        today = "2026-06-21"
        existing = {
            "10.0.0.1": m.EntryMeta(
                dag="A", orig="2026-06-01", last="2026-06-10", cnt=2
            )
        }
        fetch = {"10.0.0.1": {"Z", "A"}}
        out = m.merge_edl(existing, fetch, today)
        self.assertEqual(out["10.0.0.1"].orig, "2026-06-01")
        self.assertEqual(out["10.0.0.1"].last, today)
        self.assertEqual(out["10.0.0.1"].cnt, 3)
        self.assertEqual(out["10.0.0.1"].dag, "A")

    def test_missing_from_fetch_unchanged(self):
        today = "2026-06-30"
        existing = {
            "10.0.0.1": m.EntryMeta(
                dag="A", orig="2026-06-01", last="2026-06-10", cnt=2
            )
        }
        fetch: dict = {}
        out = m.merge_edl(existing, fetch, today)
        self.assertEqual(out["10.0.0.1"].last, "2026-06-10")
        self.assertEqual(out["10.0.0.1"].cnt, 2)


class TestParseDagXml(unittest.TestCase):
    def test_success_sample(self):
        xml = """<?xml version="1.0"?>
<response status="success" code="19">
  <result>
    <dyn-addr-grp>
      <entry>
        <member-list>
          <entry name="192.0.2.10" type="registered-ip"/>
          <entry name="192.0.2.11" type="registered-ip"/>
        </member-list>
      </entry>
    </dyn-addr-grp>
  </result>
</response>"""
        status, names = m.parse_dag_op_xml(xml)
        self.assertEqual(status, "success")
        self.assertEqual(set(names), {"192.0.2.10", "192.0.2.11"})

    def test_error(self):
        xml = """<response status="error"><msg>broken</msg></response>"""
        status, names = m.parse_dag_op_xml(xml)
        self.assertEqual(status, "error")
        self.assertEqual(names, ["broken"])


class TestWriteAtomic(unittest.TestCase):
    def test_writes_merged_and_verbatim(self):
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "out.txt")
            merged = {
                "10.0.0.1": m.EntryMeta(
                    dag="G", orig="2026-01-01", last="2026-01-02", cnt=1
                )
            }
            verbatim = {"10.9.9.9": "10.9.9.9 legacy-line"}
            fetched = {"10.0.0.1"}
            m.write_edl_atomic(path, merged, verbatim, fetched)
            text = Path(path).read_text(encoding="utf-8")
            self.assertIn("10.0.0.1", text)
            self.assertIn("10.9.9.9 legacy-line", text)
            self.assertTrue(text.endswith("\n") or len(text) > 0)


class TestOutputOrder(unittest.TestCase):
    def test_oldest_orig_first_then_verbatim(self):
        meta = {
            "10.0.0.2": m.EntryMeta(
                dag="G", orig="2026-02-01", last="2026-02-01", cnt=1
            ),
            "10.0.0.1": m.EntryMeta(
                dag="G", orig="2026-01-01", last="2026-01-01", cnt=1
            ),
        }
        verbatim = {"10.0.0.9": "10.0.0.9 no-meta"}
        keys = m.output_keys_ordered(meta, verbatim, set())
        self.assertEqual(keys, ["10.0.0.1", "10.0.0.2", "10.0.0.9"])


class TestEviction(unittest.TestCase):
    def test_drops_oldest_and_archives(self):
        meta = {
            "10.0.0.1": m.EntryMeta(
                dag="G", orig="2026-01-01", last="2026-01-01", cnt=1
            ),
            "10.0.0.2": m.EntryMeta(
                dag="G", orig="2026-02-01", last="2026-02-01", cnt=1
            ),
        }
        verbatim = {}
        ordered, expired = m.apply_max_entry_eviction(
            meta, verbatim, set(), max_entries=1, removal_date="2026-06-20"
        )
        self.assertEqual(ordered, ["10.0.0.2"])
        self.assertEqual(len(expired), 1)
        self.assertIn("10.0.0.1", expired[0])
        self.assertIn("|rem=2026-06-20", expired[0])
        self.assertNotIn("10.0.0.1", meta)


class TestExpiredArchive(unittest.TestCase):
    def test_sort_by_removal_date(self):
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "exp.txt")
            Path(p).write_text(
                "1.1.1.1 # x|rem=2026-06-02\n", encoding="utf-8"
            )
            m.merge_and_write_expired_archive(
                p,
                ["9.9.9.9 # y|rem=2026-06-01", "2.2.2.2 # z|rem=2026-06-02"],
            )
            lines = Path(p).read_text(encoding="utf-8").strip().split("\n")
            self.assertTrue(lines[0].startswith("9.9.9.9"))


class TestDefaultExpiredPath(unittest.TestCase):
    def test_stem_expired_suffix(self):
        p = m.default_expired_output_path(str(Path("/tmp/list.txt")))
        self.assertTrue(p.endswith("list.expired.txt"))


class TestVarFiles(unittest.TestCase):
    def test_parse_group_duplicates_become_list(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".var", delete=False, encoding="utf-8"
        ) as f:
            f.write("GROUP=A\nGROUP=B\n")
            path = f.name
        try:
            d = m.parse_var_file(path)
            self.assertEqual(d["GROUP"], ["A", "B"])
        finally:
            Path(path).unlink(missing_ok=True)

    def test_custom_overrides_default(self):
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".var", delete=False, encoding="utf-8"
        ) as d1:
            d1.write("MAX_ENTRIES=100\nTIMEOUT=30\n")
            p1 = d1.name
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".var", delete=False, encoding="utf-8"
        ) as d2:
            d2.write("MAX_ENTRIES=50\n")
            p2 = d2.name
        try:
            merged = m.merge_var_settings(p1, p2)
            self.assertEqual(merged["MAX_ENTRIES"], "50")
            self.assertEqual(merged["TIMEOUT"], "30")
        finally:
            Path(p1).unlink(missing_ok=True)
            Path(p2).unlink(missing_ok=True)


class TestAgeExpiration(unittest.TestCase):
    def test_removes_when_last_old_enough(self):
        meta = {
            "10.0.0.1": m.EntryMeta(
                dag="G", orig="2026-01-01", last="2026-06-01", cnt=1
            ),
            "10.0.0.2": m.EntryMeta(
                dag="G", orig="2026-06-10", last="2026-06-14", cnt=1
            ),
        }
        verbatim: dict = {}
        expired = m.apply_age_expiration(
            meta, verbatim, expire_days=7, today="2026-06-15", removal_date="2026-06-15"
        )
        self.assertEqual(len(expired), 1)
        self.assertIn("10.0.0.1", expired[0])
        self.assertNotIn("10.0.0.1", meta)
        self.assertIn("10.0.0.2", meta)

    def test_disabled_when_zero(self):
        meta = {
            "10.0.0.1": m.EntryMeta(
                dag="G", orig="2026-01-01", last="2026-01-01", cnt=1
            ),
        }
        verbatim: dict = {}
        expired = m.apply_age_expiration(
            meta, verbatim, expire_days=0, today="2026-06-15", removal_date="2026-06-15"
        )
        self.assertEqual(expired, [])
        self.assertIn("10.0.0.1", meta)


class TestRotateNumberedBackups(unittest.TestCase):
    def test_no_op_missing_file(self):
        with tempfile.TemporaryDirectory() as td:
            p = str(Path(td) / "missing.txt")
            m.rotate_numbered_backups(p, 5)
            self.assertFalse((Path(td) / "missing.txt").exists())

    def test_no_op_zero_count(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.txt"
            p.write_text("a\n", encoding="utf-8")
            m.rotate_numbered_backups(str(p), 0)
            self.assertEqual(p.read_text(encoding="utf-8"), "a\n")

    def test_rotates_chain(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.txt"
            p.write_text("current\n", encoding="utf-8")
            (Path(td) / "out.txt.1").write_text("one\n", encoding="utf-8")
            m.rotate_numbered_backups(str(p), 3)
            self.assertFalse(p.exists())
            self.assertEqual(
                (Path(td) / "out.txt.1").read_text(encoding="utf-8"), "current\n"
            )
            self.assertEqual((Path(td) / "out.txt.2").read_text(encoding="utf-8"), "one\n")
            self.assertFalse((Path(td) / "out.txt.3").exists())

    def test_drops_oldest_at_max(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "out.txt"
            p.write_text("new\n", encoding="utf-8")
            for i, content in [(1, "a\n"), (2, "b\n"), (3, "c\n")]:
                (Path(td) / f"out.txt.{i}").write_text(content, encoding="utf-8")
            m.rotate_numbered_backups(str(p), 3)
            self.assertEqual((Path(td) / "out.txt.1").read_text(encoding="utf-8"), "new\n")
            self.assertEqual((Path(td) / "out.txt.2").read_text(encoding="utf-8"), "a\n")
            self.assertEqual((Path(td) / "out.txt.3").read_text(encoding="utf-8"), "b\n")
            self.assertFalse((Path(td) / "out.txt.4").exists())


if __name__ == "__main__":
    unittest.main()
