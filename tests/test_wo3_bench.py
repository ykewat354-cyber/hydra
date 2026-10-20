"""WO3 C4: B3 mismatch detail is a content diff, not just character counts."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from hydra import bench


class BenchDiffTests(unittest.TestCase):
    def test_b3_mismatch_shows_expected_and_actual_lines(self):
        wd = Path(tempfile.mkdtemp())
        want = "1. notes/a.txt: TODO: one\n2. notes/b.txt: TODO: two\n"
        (wd / "todos.md").write_text(
            "1. notes/a.txt: TODO: one\n2. notes/b.txt: TODO: wrong\n"
            "3. notes/c.txt: extra\n", encoding="utf-8")
        ok, detail = bench.check_fixture(
            "B3", wd, {"expected": want})
        self.assertFalse(ok)
        self.assertIn("-2. notes/b.txt: TODO: two", detail)
        self.assertIn("+2. notes/b.txt: TODO: wrong", detail)
        self.assertIn("+3. notes/c.txt: extra", detail)
        diff_lines = [ln for ln in detail.splitlines()
                      if ln.startswith(("-", "+"))]
        self.assertLessEqual(len(diff_lines), 10)
        # A totally different file must still show the actual side.
        ok2, detail2 = bench.check_fixture(
            "B3", wd, {"expected": "1. notes/z.txt: TODO: zzz\n"})
        self.assertFalse(ok2)
        self.assertIn("+1. notes/a.txt: TODO: one", detail2)

    def test_b3_matching_output_still_passes(self):
        wd = Path(tempfile.mkdtemp())
        want = "1. notes/a.txt: TODO: one\n"
        (wd / "todos.md").write_text(want, encoding="utf-8")
        ok, detail = bench.check_fixture("B3", wd, {"expected": want})
        self.assertTrue(ok, detail)


if __name__ == "__main__":
    unittest.main()
