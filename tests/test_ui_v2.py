"""WO2 UI v2 tests (M1): goldens, invariants, honesty and non-interactive modes.

    python -m pytest tests/test_ui_v2.py
    UPDATE_GOLDEN=1 python -m pytest tests/test_ui_v2.py   # refresh snapshots
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hydra import config as C                     # noqa: E402
from hydra import ui_demo as D                    # noqa: E402
from hydra import ui_format as U                  # noqa: E402
from hydra import ui_theme as UT                   # noqa: E402
from hydra.tui_app import App, Theme              # noqa: E402

GOLDEN = Path(__file__).parent / "ui_golden"
GOLDEN_WIDTHS = (36, 48, 54, 80, 120)
GOLDEN_SCENES = ("home", "running", "done", "partial", "failed", "palette",
                 "stream", "unicode")
ALL_SCENES = GOLDEN_SCENES + ("glyphs",)


_FAKE_KEY = Path(tempfile.mkdtemp(prefix="hydra-ui-fake-key-")) / "key"
_FAKE_KEY.write_text("test-key-not-secret\n", encoding="utf-8")


def _cfg(tmp: Path = None):
    return C.load_config(overrides=[
        "api.host=http://127.0.0.1:1",
        # deterministic welcome line: a fake key file, never the real one
        "api.key_env=",
        f"api.key_file={_FAKE_KEY}",
        "termux.wake_lock=false",
        "termux.notify_on_finish=false",
        "termux.notify_on_park=false",
    ], project_dir=str(tmp or REPO))


def _plain(rows) -> str:
    return "\n".join("".join(t for t, _ in r) for r in rows)


class GoldenSnapshotTests(unittest.TestCase):
    """T-UI1: ANSI-stripped snapshots at five widths."""

    def test_goldens_match(self):
        GOLDEN.mkdir(parents=True, exist_ok=True)
        cfg = _cfg()
        update = bool(os.environ.get("UPDATE_GOLDEN"))
        for width in GOLDEN_WIDTHS:
            for scene in GOLDEN_SCENES:
                with self.subTest(scene=scene, width=width):
                    rows, _ = D.build_scene(scene, width, 31, cfg, "safe",
                                            "fullscreen")
                    text = _plain(rows)
                    path = GOLDEN / f"{scene}_{width}.txt"
                    if update or not path.exists():
                        path.write_text(text + "\n", encoding="utf-8")
                    self.assertEqual(path.read_text(encoding="utf-8"),
                                     text + "\n",
                                     f"golden changed: {path.name} "
                                     f"(UPDATE_GOLDEN=1 to refresh)")


class InvariantTests(unittest.TestCase):
    """T-UI2: width, SGR and glyph-tier invariants over every frame."""

    def test_no_line_wider_than_terminal(self):
        cfg = _cfg()
        for width in (30, 36, 43, 44, 48, 54, 59, 60, 80, 100, 120, 160):
            for scene in GOLDEN_SCENES:
                with self.subTest(scene=scene, width=width):
                    rows, _ = D.build_scene(scene, width, 31, cfg, "safe",
                                            "fullscreen")
                    for i, r in enumerate(rows):
                        line = "".join(t for t, _ in r)
                        self.assertLessEqual(
                            U.display_width(line), width,
                            f"{scene}@{width} row {i} overflows: {line!r}")

    def test_no_banned_sgr(self):
        cfg = _cfg()
        for width in (36, 48, 54, 80, 120):
            for scene in GOLDEN_SCENES:
                with self.subTest(scene=scene, width=width):
                    rows, _ = D.build_scene(scene, width, 31, cfg, "safe",
                                            "fullscreen")
                    theme = Theme(color=True, glyphs="safe", depth="256")
                    out = D._render_rows(rows, theme)
                    self.assertEqual([], UT.banned_sgr(out))

    def test_banned_sgr_parser(self):
        self.assertTrue(UT.banned_sgr("\x1b[3mitalic\x1b[0m"))
        self.assertTrue(UT.banned_sgr("\x1b[4munderline\x1b[0m"))
        self.assertTrue(UT.banned_sgr("\x1b[48;5;10mbg\x1b[0m"))
        self.assertTrue(UT.banned_sgr("\x1b[41mred bg\x1b[0m"))
        self.assertTrue(UT.banned_sgr("\x1b[100mbright bg\x1b[0m"))
        self.assertFalse(UT.banned_sgr("\x1b[38;5;81mok\x1b[0m"))
        self.assertFalse(UT.banned_sgr("\x1b[38;2;95;215;255mok\x1b[0m"))

    def test_plain_rows_contain_no_escapes(self):
        cfg = _cfg()
        for scene in GOLDEN_SCENES:
            rows, _ = D.build_scene(scene, 54, 24, cfg, "safe", "fullscreen")
            self.assertNotIn("\x1b", _plain(rows))

    def test_glyph_tiers(self):
        self.assertIn(UT.LOGO[0], "\n".join(UT.LOGO))
        safe = Theme(color=False, glyphs="safe", depth="none")
        rich = Theme(color=False, glyphs="rich", depth="none")
        ascii_t = Theme(color=True, glyphs="ascii", depth="none")
        for theme in (safe, rich, ascii_t):
            for key in ("prompt", "tool", "ok", "bad", "conn", "box_tl",
                        "box_h"):
                self.assertTrue(theme.sym(key))
        self.assertEqual("❯", safe.sym("prompt"))
        self.assertEqual(">", ascii_t.sym("prompt"))
        self.assertEqual("+", ascii_t.sym("ok"))
        self.assertEqual("x", ascii_t.sym("bad"))
        # ascii tier must stay inside ASCII
        for key in ("prompt", "tool", "ok", "bad", "conn", "tree_tee",
                    "box_tl", "box_tr", "box_bl", "box_br", "box_h", "box_v",
                    "arrow", "sep", "bullet"):
            self.assertTrue(ascii_t.sym(key).isascii(), key)


class HonestyTests(unittest.TestCase):
    """T-UI9: the closing card never invents facts."""

    def _app(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return App(_cfg(Path(tmp.name)), cwd=tmp.name)

    def test_files_not_tracked_when_null(self):
        app = self._app()
        app.event({"type": "run_start", "run": "r_t", "task": "x",
                   "cwd": ".", "ts": time.time()})
        app.event({"type": "report", "agent": "a0", "report": {
            "status": "done", "summary": "done", "files_changed": None}})
        app.drain()
        text = "\n".join(app.render(80, 30))
        self.assertIn("files   not tracked", text)

    def test_files_none_when_empty_list(self):
        app = self._app()
        app.event({"type": "run_start", "run": "r_t", "task": "x",
                   "cwd": ".", "ts": time.time()})
        app.event({"type": "report", "agent": "a0", "report": {
            "status": "done", "summary": "done", "files_changed": []}})
        app.drain()
        text = "\n".join(app.render(80, 30))
        self.assertIn("files   none", text)

    def test_verifier_failure_shows_reason(self):
        app = self._app()
        app.event({"type": "run_start", "run": "r_t", "task": "x",
                   "cwd": ".", "ts": time.time()})
        app.event({"type": "verify", "agent": "a0", "verdict": "fail",
                   "model": "gemma4:31b",
                   "issues": ["no HTTP 200 evidence for the page"]})
        app.event({"type": "report", "agent": "a0", "report": {
            "status": "partial", "summary": "",
            "files_changed": ["js/script.js"]}})
        app.drain()
        text = "\n".join(app.render(80, 40))
        self.assertIn("no HTTP 200 evidence for the page", text)
        self.assertIn("Partial", text)

    def test_failed_run_shows_cause(self):
        app = self._app()
        app.event({"type": "run_start", "run": "r_t", "task": "x",
                   "cwd": ".", "ts": time.time()})
        app._on_done({"code": 1,
                      "report": {"status": "failed",
                                 "summary": "401 invalid key — run hydra doctor"},
                      "usage": {"in": 1, "out": 1, "est_usd": 0.001},
                      "turns": 2, "agents": 1})
        app.drain()
        text = "\n".join(app.render(80, 30))
        self.assertIn("✗ Failed", text)
        self.assertIn("401 invalid key", text)

    def test_file_diffstats_come_from_real_tools(self):
        app = self._app()
        app.event({"type": "run_start", "run": "r_t", "task": "x",
                   "cwd": ".", "ts": time.time()})
        app.event({"type": "tool_call", "agent": "a0", "tool": "edit_file",
                   "args": {"path": "calc.py", "old": "a\n",
                            "new": "a\nb\n"}})
        app.event({"type": "tool_result", "agent": "a0", "tool": "edit_file",
                   "preview": "ok", "chars": 2, "ms": 50})
        app.event({"type": "report", "agent": "a0", "report": {
            "status": "done", "summary": "done",
            "files_changed": ["calc.py"]}})
        app.drain()
        self.assertIn("calc.py +1", "\n".join(app.render(80, 40)))


class ModeTests(unittest.TestCase):
    """WO2 §5.6: plain/ascii/no-color have no escapes."""

    def test_snapshot_has_no_escapes(self):
        cfg = _cfg()
        for scene in ("home", "running", "done"):
            rows, _ = D.build_scene(scene, 54, 24, cfg, "safe", "fullscreen")
            self.assertNotIn("\x1b", _plain(rows))

    def test_ascii_scene_has_no_box_glyphs(self):
        cfg = _cfg()
        rows, _ = D.build_scene("home", 54, 24, cfg, "ascii", "fullscreen")
        text = _plain(rows)
        for ch in ("╭", "╮", "╰", "╯", "│", "─", "❯", "●", "└"):
            self.assertNotIn(ch, text)

    def test_ui_demo_cli_snapshot_runs(self):
        import io
        import contextlib
        from hydra import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--set", "api.host=http://127.0.0.1:1",
                             "ui-demo", "home", "--snapshot", "--no-color"])
            self.assertEqual(code, 0)
        self.assertNotIn("\x1b", buf.getvalue())
        self.assertIn("HYDRA", buf.getvalue())

    def test_ui_demo_all_runs(self):
        import io
        import contextlib
        from hydra import cli
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["--set", "api.host=http://127.0.0.1:1",
                             "ui-demo", "all", "--no-color"])
        self.assertEqual(code, 0)
        out = buf.getvalue()
        for scene in ("home", "running", "done", "partial", "failed",
                      "palette", "stream", "unicode"):
            self.assertIn(f"── {scene}", out)
        self.assertNotIn("\x1b", out)


class WelcomeTests(unittest.TestCase):
    def test_welcome_has_three_row_lockup(self):
        cfg = _cfg()
        rows, _ = D.build_scene("home", 48, 24, cfg, "safe", "fullscreen")
        text = _plain(rows)
        for line in UT.LOGO:
            self.assertIn(line, text)
        self.assertEqual(3, len(UT.LOGO))

    def test_narrow_welcome_drops_logo(self):
        cfg = _cfg()
        rows, _ = D.build_scene("home", 36, 24, cfg, "safe", "fullscreen")
        text = _plain(rows)
        self.assertIn("HYDRA", text)
        self.assertNotIn(UT.LOGO[0], text)

    def test_tips_hidden_after_first_user_message(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = App(_cfg(Path(tmp.name)), cwd=tmp.name)
        app.add_user("hello")
        text = "\n".join(app.render(80, 30))
        self.assertNotIn("@file attach", text)


class CursorTests(unittest.TestCase):
    def test_cursor_sits_inside_input_box(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app = App(_cfg(Path(tmp.name)), cwd=tmp.name)
        app.input = "abc"
        app.cursor = 1
        rows, (row, col) = app._compose(54, 24)
        line = "".join(t for t, _ in rows[row])
        self.assertEqual("│", line[0])           # input content row
        self.assertEqual("❯", line[2])
        self.assertEqual("abc", line[4:7])       # after "│ ❯ "
        self.assertEqual(5, col)                 # 0-based: between a and b
        self.assertEqual("b", line[col])


if __name__ == "__main__":
    unittest.main()
