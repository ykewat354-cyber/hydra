"""WO2 M3 — input features: @ files, ! shell, /expand, /why, /export, /ui.

Everything here runs against the real ``App`` with a local config: no network,
no API key. The pty behaviour of these inputs is covered in test_ui_inline.py.
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(REPO))

from hydra import config as C                 # noqa: E402
from hydra import ui_format as U              # noqa: E402
from hydra.tui_app import App                 # noqa: E402


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cwd = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._old_home = os.environ.get("HYDRA_HOME")
        self._old_nocolor = os.environ.get("NO_COLOR")
        os.environ["HYDRA_HOME"] = self.home.name
        os.environ["NO_COLOR"] = "1"
        root = Path(self.cwd.name)
        (root / "src").mkdir()
        (root / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
        (root / "src" / "util.py").write_text("x = 1\n", encoding="utf-8")
        (root / "docs").mkdir()
        (root / "README.md").write_text("# readme\n", encoding="utf-8")
        self.app = self._app()

    def tearDown(self):
        # restore the environment *before* touching the temp dirs, so a stray
        # writer (or a failed cleanup) can never leak HYDRA_HOME into the next
        # test file
        for key, old in (("HYDRA_HOME", self._old_home),
                         ("NO_COLOR", self._old_nocolor)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.home.cleanup()
        self.cwd.cleanup()

    def _app(self):
        cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:1",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ], project_dir=self.cwd.name)
        return App(cfg, cwd=self.cwd.name)

    def _text(self) -> str:
        return self.app.transcript_text()


class MentionTests(Base):
    def test_lists_and_completes_files(self):
        self.app.input = "@src"
        self.app.cursor = len(self.app.input)
        matches = self.app._ac_matches()
        self.assertIn("src/main.py", matches)
        self.assertIn("src/util.py", matches)
        self.assertTrue(self.app._palette_active())
        rows = self.app._palette_rows(60)
        self.assertTrue(rows)
        self.assertTrue(any("file" in t for spans in rows for t, _ in spans))

    def test_completion_keeps_the_mention_in_place(self):
        self.app.input = "fix @util"
        self.app.cursor = len(self.app.input)
        matches = self.app._ac_matches()
        self.assertEqual(["src/util.py"], matches)
        self.app._complete()
        self.assertEqual("fix @src/util.py ", self.app.input)
        self.assertEqual(len(self.app.input), self.app.cursor)

    def test_directory_completion_keeps_slash(self):
        self.app.input = "@doc"
        self.app.cursor = len(self.app.input)
        self.app._complete()
        self.assertEqual("@docs/", self.app.input)

    def test_no_mention_when_word_is_done(self):
        self.app.input = "fix @src/main.py and more"
        self.app.cursor = len(self.app.input)
        self.assertIsNone(self.app._mention())
        self.assertEqual([], self.app._ac_matches())

    def test_slash_palette_still_works(self):
        self.app.input = "/ex"
        self.app.cursor = len(self.app.input)
        self.assertIn("/expand", self.app._ac_matches())


class ShellTests(Base):
    def test_local_shell_card(self):
        self.app.submit("!echo wood-ok")
        kinds = [b.kind for b in self.app.blocks]
        self.assertIn("user", kinds)
        tool = [b for b in self.app.blocks if b.kind == "tool"]
        self.assertEqual(1, len(tool))
        self.assertTrue(tool[0].meta.get("ok"))
        self.assertIn("wood-ok", str(tool[0].meta.get("preview") or ""))
        self.assertFalse(self.app.running)
        self.assertIsNone(self.app.rt)

    def test_nonzero_exit_is_honest(self):
        self.app.submit("!exit 3")
        tool = [b for b in self.app.blocks if b.kind == "tool"][0]
        self.assertIs(tool.meta.get("ok"), False)
        self.assertIn("exit 3", self._text())

    def test_empty_shell_is_a_hint(self):
        self.app.submit("!")
        self.assertEqual([], [b for b in self.app.blocks if b.kind == "tool"])
        self.assertIn("usage: !command", self._text())


class ExpandTests(Base):
    def _tool(self):
        now = time.time()
        self.app.event({"type": "tool_call", "agent": "a0", "tool": "bash",
                        "args": {"cmd": "echo one"}})
        self.app.event({"type": "tool_result", "agent": "a0", "tool": "bash",
                        "preview": "one\ntwo\nthree", "chars": 13, "ms": 20,
                        "lines": 3})
        self.app.event({"type": "tool_call", "agent": "a0", "tool": "bash",
                        "args": {"cmd": "echo two"}})
        self.app.event({"type": "tool_result", "agent": "a0", "tool": "bash",
                        "preview": "alpha", "chars": 5, "ms": 10, "lines": 1})
        self.app.drain()

    def test_expand_shows_args_and_output(self):
        self._tool()
        self.app.handle_command("/expand")
        rows = "\n".join(t for t, _ in
                         [r for _b, rs in self.app._block_rows(80) for r in rs])
        self.assertIn("args:", rows)
        self.assertIn("out:", rows)
        self.app.handle_command("/collapse")
        rows = "\n".join(t for t, _ in
                         [r for _b, rs in self.app._block_rows(80) for r in rs])
        self.assertNotIn("args:", rows)

    def test_expand_nth_from_the_end(self):
        self._tool()
        self.app.handle_command("/expand 2")
        tools = [b for b in self.app.blocks if b.kind == "tool"]
        self.assertTrue(tools[0].meta.get("expanded"))
        self.assertFalse(tools[1].meta.get("expanded"))

    def test_collapse_without_cards(self):
        self.app.handle_command("/collapse")
        self.assertIn("no tool cards yet", self._text())


class WhyTests(Base):
    def test_why_on_empty_state(self):
        self.app.handle_command("/why")
        self.assertIn("nothing to explain", self._text())

    def test_why_reports_verify_and_failure(self):
        self.app.event({"type": "run_start", "run": "r_x", "task": "t",
                        "cwd": self.cwd.name, "ts": time.time()})
        self.app.event({"type": "verify", "agent": "a0", "verdict": "fail",
                        "model": "gemma4:31b",
                        "issues": [{"kind": "claim", "detail": "no artifact"}],
                        "reason": "claim without artifact"})
        self.app.event({"type": "report", "agent": "a0", "report": {
            "status": "failed", "summary": "tests still red",
            "files_changed": [], "artifacts": [], "open_issues": []}})
        self.app.drain()
        self.app.handle_command("/why")
        text = self._text()
        self.assertIn("r_x", text)
        self.assertIn("tests still red", text)
        self.assertIn("verify fail", text)
        self.assertIn("claim without artifact", text)


class ExportTests(Base):
    def test_export_writes_plain_transcript(self):
        self.app.add_user("hello export")
        self.app.drain()
        self.app.handle_command("/export notes.txt")
        path = Path(self.cwd.name) / "notes.txt"
        self.assertTrue(path.is_file())
        data = path.read_text(encoding="utf-8")
        self.assertIn("hello export", data)
        self.assertNotIn("\x1b", data)
        self.assertIn("exported", self._text())

    def test_export_default_name(self):
        self.app.add_user("hello default")
        self.app.drain()
        self.app.handle_command("/export")
        hits = list(Path(self.cwd.name).glob("hydra-transcript-*.txt"))
        self.assertEqual(1, len(hits))
        self.assertIn("hello default", hits[0].read_text(encoding="utf-8"))


class UiModeTests(Base):
    def test_ui_shows_mode(self):
        self.app.handle_command("/ui")
        self.assertIn("ui mode: inline", self._text())

    def test_ui_rejects_garbage(self):
        self.app.handle_command("/ui sideways")
        self.assertIn("usage: /ui", self._text())
        self.assertEqual("inline", self.app.ui_mode)

    def test_ui_saves_for_next_launch(self):
        self.app.handle_command("/ui fullscreen")
        self.assertEqual("fullscreen", C.get(self.app.cfg, "ui.mode"))
        p = Path(self.home.name) / "config.toml"
        self.assertTrue(p.is_file())
        self.assertIn("[ui]", p.read_text(encoding="utf-8"))
        again = C.load_config(project_dir=self.cwd.name)
        self.assertEqual("fullscreen", C.get(again, "ui.mode"))
        self.assertIn("next launch", self._text())

    def test_ui_same_mode_is_noop(self):
        self.app.handle_command("/ui inline")
        self.assertIn("already inline", self._text())


class GlyphAndPasteTests(Base):
    def test_glyphs_command_is_escape_free(self):
        self.app.handle_command("/glyphs")
        text = self._text()
        self.assertIn("safe", text)
        self.assertIn("rich", text)
        self.assertNotIn("\x1b", text)

    def test_paste_chip(self):
        self.app.feed_bytes(
            b"\x1b[200~line one\nline two\nline three\x1b[201~")
        self.assertEqual("[pasted 3 lines]", self.app.paste_chip)
        self.assertEqual("line one\nline two\nline three", self.app.input)

    def test_small_paste_has_no_chip(self):
        self.app.feed_bytes(b"\x1b[200~hi there\x1b[201~")
        self.assertIsNone(self.app.paste_chip)
        self.assertEqual("hi there", self.app.input)

    def test_chip_clears_on_submit(self):
        self.app.feed_bytes(b"\x1b[200~a\nb\nc\x1b[201~")
        self.assertIsNotNone(self.app.paste_chip)
        self.app.input = "task with the paste"
        self.app.submit_input()
        self.assertIsNone(self.app.paste_chip)


class OpenTests(Base):
    def test_open_without_url(self):
        self.app.handle_command("/open")
        self.assertIn("no link to open yet", self._text())

    def test_open_without_opener_is_honest(self):
        self.app.last_url = "http://127.0.0.1:19999/index.html"
        real_which = __import__("shutil").which
        try:
            __import__("shutil").which = lambda name: None
            self.app.handle_command("/open")
        finally:
            __import__("shutil").which = real_which
        self.assertIn("no opener", self._text())
        self.assertIn("127.0.0.1:19999", self._text())


class AliasAndCompactTests(Base):
    def test_agents_is_tree_alias(self):
        self.app.handle_command("/agents")
        self.assertIn("no run yet", self._text())

    def test_compact_without_lead(self):
        self.app.handle_command("/compact")
        self.assertIn("no lead yet", self._text())


if __name__ == "__main__":
    unittest.main()
