"""WO5 W5-2 — `hydra diff`, TUI /diff, and tolerant hunk editing."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hydra import config as C                    # noqa: E402


class DiffTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name
        self.root = Path(self.cwd.name)
        self._git("init", "-q")
        (self.root / "calc.py").write_text("alpha = 1\n", encoding="utf-8")
        self._git("add", "calc.py")
        self._git("-c", "user.name=Hydra Test", "-c", "user.email=hydra@example.test",
                  "commit", "-qm", "base")

    def tearDown(self):
        self.home.cleanup()
        self.cwd.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home

    def _git(self, *args):
        return subprocess.run(["git", "-C", str(self.root), *args],
                              capture_output=True, text=True, timeout=20)

    def _run(self, *args):
        env = dict(os.environ)
        env["HYDRA_HOME"] = self.home.name
        env["PYTHONPATH"] = str(REPO)
        env["NO_COLOR"] = "1"
        return subprocess.run([sys.executable, "-m", "hydra", *args],
                              cwd=self.root, env=env, capture_output=True,
                              text=True, timeout=30)

    def test_cli_diff_and_no_changes_notice(self):
        (self.root / "calc.py").write_text("alpha = 2\n", encoding="utf-8")
        out = self._run("diff")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("-alpha = 1", out.stdout)
        self.assertIn("+alpha = 2", out.stdout)
        self._git("checkout", "--", "calc.py")
        out = self._run("diff")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("no uncommitted changes", out.stdout)

    def test_cli_diff_outside_git(self):
        with tempfile.TemporaryDirectory() as d:
            env = dict(os.environ)
            env["HYDRA_HOME"] = self.home.name
            env["PYTHONPATH"] = str(REPO)
            out = subprocess.run([sys.executable, "-m", "hydra", "diff"],
                                 cwd=d, env=env, capture_output=True,
                                 text=True, timeout=30)
            self.assertEqual(out.returncode, 0, out.stderr)
            self.assertIn("not a git repository", out.stdout)

    def test_tui_diff_card_has_coloured_rows(self):
        from hydra.tui_app import App
        (self.root / "calc.py").write_text("alpha = 2\n", encoding="utf-8")
        cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:1",
            "api.key_env=", "api.key_file=/nonexistent/hydra-key",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ], project_dir=self.cwd.name)
        app = App(cfg, cwd=self.cwd.name)
        app.handle_command("/diff")
        blocks = [b for b in app.blocks if b.kind == "diff"]
        self.assertEqual(len(blocks), 1)
        rows = blocks[0].meta["rows"]
        self.assertTrue(any(style == "added" for _text, style in rows))
        self.assertTrue(any(style == "removed" for _text, style in rows))
        self.assertIn("+alpha = 2", app.transcript_text())


class TolerantEditTests(unittest.TestCase):
    def _ctx(self, root):
        return SimpleNamespace(cwd=Path(root), cfg={}, journal=None, rt=None)

    def test_trailing_whitespace_and_indent_shift(self):
        from hydra.tools.files import _tool_edit_file

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.py"
            p.write_text("def f():   \n    return 1\n", encoding="utf-8")
            out = _tool_edit_file({"path": "a.py",
                                   "old": "def f():\n    return 1",
                                   "new": "def f():\n    return 2"},
                                  self._ctx(d))
            self.assertIn("edited", out)
            self.assertEqual(p.read_text(encoding="utf-8"),
                             "def f():\n    return 2\n")

            p.write_text("    alpha = 1\n    beta = 2\n", encoding="utf-8")
            out = _tool_edit_file({"path": "a.py",
                                   "old": "  alpha = 1\n  beta = 2",
                                   "new": "  alpha = 10\n  beta = 20"},
                                  self._ctx(d))
            self.assertIn("edited", out)
            self.assertEqual(p.read_text(encoding="utf-8"),
                             "    alpha = 10\n    beta = 20\n")

    def test_three_closest_candidates_on_failure(self):
        from hydra.tools.files import _tool_edit_file

        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "a.py"
            p.write_text("alpha = 10\nbeta = 2\n", encoding="utf-8")
            out = _tool_edit_file({"path": "a.py",
                                   "old": "alpha = 10\nmissing = 0",
                                   "new": "x"}, self._ctx(d))
            self.assertIn("Closest matches", out)
            self.assertIn("1: alpha = 10", out)
            self.assertIn("2: beta = 2", out)
            self.assertEqual(p.read_text(encoding="utf-8"),
                             "alpha = 10\nbeta = 2\n")


if __name__ == "__main__":
    unittest.main()
