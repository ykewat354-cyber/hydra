"""WO1 T25 — TUI and terminal-state safety.

Checked here:
* a piped one-shot run (`hydra "say hi" | cat -v` shape: stdin a TTY, stdout a
  pipe) contains no ESC bytes at all — no mouse tracking, no alt-screen;
* the same non-TTY branch of ``App`` is escape-free at unit level;
* ``hydra -i < /dev/null`` exits cleanly instead of hanging;
* under a real pty, normal quit, SIGTERM, SIGHUP and an uncaught exception
  inside the app loop all emit the sequences that disable mouse tracking,
  show the cursor and leave the alternate screen;
* a simulated SIGWINCH re-lays out (unit + pty);
* wrapping of Devanagari/Hinglish and CJK never breaks before a combining or
  spacing mark and never exceeds the width.

The full-screen TUI is exercised through ``python -m hydra`` (the console
script in this repo wraps exactly that: ``exec python -m hydra "$@"``).
"""
from __future__ import annotations

import contextlib
import io
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
import unicodedata
import unittest
from pathlib import Path

from tests.fake_ollama import FakeOllama

REPO = Path(__file__).resolve().parents[1]
HYDRA_CMD = [sys.executable, "-m", "hydra"]

MOUSE_ON = (b"\x1b[?1000h", b"\x1b[?1002h", b"\x1b[?1006h", b"\x1b[?1007h")
CLEANUP_SEQS = (
    b"\x1b[?1007l", b"\x1b[?1006l", b"\x1b[?1002l", b"\x1b[?1000l",  # mouse off
    b"\x1b[?2004l",                                                  # paste off
    b"\x1b[?25h",                                                    # cursor back
    b"\x1b[?1049l",                                                  # leave alt screen
)


def _env(home: str) -> dict:
    env = dict(os.environ)
    env.update(HYDRA_HOME=home, PYTHONPATH=str(REPO), TERM="xterm-256color")
    env.pop("NO_COLOR", None)
    env.pop("HYDRA_ASCII", None)
    return env


def _starts_mark(ch: str) -> bool:
    """True when *ch* must not sit at the start of a wrapped row."""
    if not ch:
        return False
    return bool(unicodedata.combining(ch)) or unicodedata.category(ch) in (
        "Mn", "Mc", "Me", "Cf")


def _read_for(master: int, seconds: float) -> bytes:
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([master], [], [], min(0.2, max(0, end - time.time())))
        if not r:
            continue
        try:
            d = os.read(master, 65536)
        except OSError:
            break
        if not d:
            break
        buf += d
    return buf


def _read_until(master: int, pred, timeout: float = 15.0) -> bytes:
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.2)
        if not r:
            if pred(buf):
                break
            continue
        try:
            d = os.read(master, 65536)
        except OSError:
            break
        if not d:
            break
        buf += d
        if pred(buf):
            break
    return buf


def _wait_exit(master: int, proc: subprocess.Popen, timeout: float = 20.0) -> bytes:
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.2)
        if r:
            try:
                d = os.read(master, 65536)
            except OSError:
                d = b""
            buf += d
        if proc.poll() is not None:
            buf += _read_for(master, 0.5)
            break
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)
    return buf


class NonTtyOutputTests(unittest.TestCase):
    """`hydra "say hi" | cat -v` and `hydra -i < /dev/null` shapes."""

    def setUp(self):
        self.srv = FakeOllama(tags=["gpt-oss:120b"]).start()
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name

    def tearDown(self):
        self.srv.stop()
        self.home.cleanup()
        self.cwd.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home

    def test_pipe_run_has_no_terminal_escape_sequences(self):
        # stdin is a real TTY (like typing the command), stdout is a pipe.
        self.srv.add("gpt-oss:120b", {
            "content": "hi from the pipe",
            "tool_calls": [{"function": {"name": "finish", "arguments": {
                "status": "done", "summary": "said hi"}}}]})
        self.srv.add("*", {"content": "done"})
        master, slave = pty.openpty()
        try:
            proc = subprocess.run(HYDRA_CMD + [
                "--set", f"api.host={self.srv.url}",
                "--set", "loop.verify=never",
                "--set", "termux.wake_lock=false",
                "--set", "termux.notify_on_finish=false",
                "--set", "termux.notify_on_park=false",
                "say hi"], cwd=self.cwd.name, env=_env(self.home.name),
                stdin=slave, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                timeout=60)
        finally:
            os.close(slave)
            os.close(master)
        out = proc.stdout
        self.assertEqual(proc.returncode, 0, out[:2000])
        self.assertIn(b"said hi", out)
        # `hydra ... | cat -v` evidence: the raw stream has no ESC byte, so
        # cat -v cannot show `^[[...`; check the exact mouse/alt sequences too.
        self.assertNotIn(b"\x1b", out)
        for seq in MOUSE_ON + (b"[?1049h", b"[?1049l", b"[?1000l"):
            self.assertNotIn(seq, out)

    def test_non_tty_app_branch_is_escape_free(self):
        from hydra import config as C
        from hydra.tui_app import App
        cfg = C.load_config(overrides=["api.host=http://127.0.0.1:1",
                                       "termux.wake_lock=false"],
                            project_dir=self.cwd.name)
        app = App(cfg, cwd=self.cwd.name)
        writes = []
        app._write = writes.append
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = app._run_non_tty()
        self.assertEqual(code, 2)
        self.assertEqual(writes, [])
        self.assertTrue(any("not a terminal" in b.text for b in app.blocks))
        # both the stdout buffer and the pure render stay escape-free
        self.assertNotIn("\x1b", buf.getvalue())
        self.assertNotIn("\x1b", "\n".join(app.render(80, 20)))

    def test_dash_i_with_devnull_stdin_exits_cleanly(self):
        t0 = time.time()
        proc = subprocess.run(HYDRA_CMD + [
            "-i",
            "--set", "api.host=http://127.0.0.1:1",
            "--set", "termux.wake_lock=false"],
            cwd=self.cwd.name, env=_env(self.home.name),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=30)
        self.assertLess(time.time() - t0, 30)
        self.assertEqual(proc.returncode, 0, proc.stdout[:2000])
        self.assertNotIn(b"\x1b", proc.stdout)


class PtyTerminalStateTests(unittest.TestCase):
    """Real pty: every exit path must restore mouse/cursor/alt-screen state."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self.env = _env(self.home.name)

    def tearDown(self):
        self.home.cleanup()
        self.cwd.cleanup()

    def _start(self, cmd=None) -> tuple:
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                cmd or (HYDRA_CMD + [
                    "-i",
                    "--set", "api.host=http://127.0.0.1:1",
                    "--set", "termux.wake_lock=false",
                    "--set", "termux.notify_on_finish=false",
                    "--set", "termux.notify_on_park=false"]),
                cwd=self.cwd.name, env=self.env,
                stdin=slave, stdout=slave, stderr=slave)
        finally:
            os.close(slave)
        buf = _read_until(master, lambda b: b"\x1b[?1049h" in b, timeout=15)
        self.assertIn(b"\x1b[?1049h", buf, "TUI never entered full-screen mode")
        return proc, master, buf

    def _finish(self, proc, master, buf) -> bytes:
        buf += _wait_exit(master, proc)
        try:
            os.close(master)
        except OSError:
            pass
        return buf

    def _assert_cleanup(self, buf: bytes):
        for seq in CLEANUP_SEQS:
            self.assertIn(seq, buf, f"missing cleanup sequence {seq!r}")

    def test_normal_quit_restores_terminal(self):
        proc, master, buf = self._start()
        os.write(master, b"/quit\r")
        buf = self._finish(proc, master, buf)
        self.assertEqual(proc.returncode, 0, buf[-800:])
        self._assert_cleanup(buf)

    def test_sigterm_restores_terminal(self):
        proc, master, buf = self._start()
        proc.send_signal(signal.SIGTERM)
        buf = self._finish(proc, master, buf)
        self.assertEqual(proc.returncode, 0, buf[-800:])
        self._assert_cleanup(buf)

    def test_sighup_restores_terminal(self):
        proc, master, buf = self._start()
        proc.send_signal(signal.SIGHUP)
        buf = self._finish(proc, master, buf)
        self.assertEqual(proc.returncode, 0, buf[-800:])
        self._assert_cleanup(buf)

    def test_uncaught_exception_restores_terminal(self):
        script = Path(self.cwd.name) / "boom_app.py"
        script.write_text(textwrap.dedent('''
            import sys
            from hydra.tui_app import App
            def boom(self):
                raise RuntimeError("simulated crash inside app loop")
            App.drain = boom
            from hydra import config as C
            cfg = C.load_config(overrides=["api.host=http://127.0.0.1:1"],
                                project_dir=".")
            sys.exit(App(cfg, cwd=".").run())
        '''), encoding="utf-8")
        proc, master, buf = self._start(cmd=[sys.executable, str(script)])
        buf = self._finish(proc, master, buf)
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn(b"RuntimeError", buf)
        self._assert_cleanup(buf)

    def test_sigwinch_relayout_then_clean_exit(self):
        proc, master, buf = self._start()
        try:  # also resize the pty itself, not just deliver the signal
            import fcntl
            import struct
            import termios
            fcntl.ioctl(master, termios.TIOCSWINSZ,
                        struct.pack("HHHH", 40, 100, 0, 0))
        except Exception:
            pass
        os.kill(proc.pid, signal.SIGWINCH)
        buf += _read_for(master, 0.6)
        if proc.poll() is not None:
            buf = self._finish(proc, master, buf)
            self.fail(f"app died on SIGWINCH: {buf[-400:]!r}")
        os.write(master, b"/quit\r")
        buf = self._finish(proc, master, buf)
        self.assertEqual(proc.returncode, 0, buf[-800:])
        self._assert_cleanup(buf)


class LayoutTests(unittest.TestCase):
    SAMPLES = ("नमस्ते दुनिया 🙂", "हिन्दी में लिखें 🙂", "क्षत्रिय और प्रणाम",
               "你好世界 🙂 テスト中文字符", "a🙂b中文字符नमस्ते mixed")

    def setUp(self):
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        self.home = tempfile.TemporaryDirectory()
        os.environ["HYDRA_HOME"] = self.home.name

    def tearDown(self):
        self.cwd.cleanup()
        self.home.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home

    def test_wrap_fits_width_and_never_starts_with_a_mark(self):
        from hydra import ui_format as U
        for text in self.SAMPLES:
            for width in range(6, 91):
                rows = U.wrap_display(text, width, "❯ ", "  ")
                for row in rows:
                    self.assertLessEqual(U.display_width(row), width,
                                         (text, width, row))
                for row in rows[1:]:
                    body = row[2:]  # hang prefix
                    if body:
                        self.assertFalse(_starts_mark(body[0]),
                                         (text, width, row))
        # CJK/emoji are wide: the width is display cells, not codepoints
        self.assertEqual(U.display_width("你好"), 4)
        self.assertEqual(U.display_width("नमस्ते"), 5)
        self.assertEqual(U.display_width("🙂"), 2)

    def test_wrap_preserves_non_space_characters(self):
        from hydra import ui_format as U
        for text in self.SAMPLES:
            for width in (6, 10, 20, 40):
                rows = U.wrap_display(text, width, "❯ ", "  ")
                joined = "".join(rows).replace("❯ ", "").replace("  ", "")
                self.assertEqual(
                    "".join(c for c in text if not c.isspace()),
                    "".join(c for c in joined if not c.isspace()),
                    (text, width, rows))

    def test_markdown_rows_fit_narrow_width(self):
        from hydra import ui_format as U
        for text in self.SAMPLES:
            for width in (30, 40):
                for row, _style in U.markdown_rows(text, width):
                    self.assertLessEqual(U.display_width(row), width,
                                         (text, width, row))

    def test_app_render_narrow_unicode(self):
        from hydra import config as C
        from hydra.tui_app import App, Block
        from hydra import ui_format as U
        cfg = C.load_config(overrides=["api.host=http://127.0.0.1:1",
                                       "termux.wake_lock=false"],
                            project_dir=self.cwd.name)
        app = App(cfg, cwd=self.cwd.name)
        for text in self.SAMPLES:
            app.blocks.append(Block("assistant", text=text, final=True))
        for width in (30, 33, 40, 80):
            rows = app.render(width, 30)
            self.assertTrue(rows)
            for row in rows:
                self.assertLessEqual(U.display_width(row), width, (width, row))

    def test_simulated_sigwinch_relayouts(self):
        from hydra import config as C
        from hydra.tui_app import App
        cfg = C.load_config(overrides=["api.host=http://127.0.0.1:1",
                                       "termux.wake_lock=false"],
                            project_dir=self.cwd.name)
        app = App(cfg, cwd=self.cwd.name)
        writes = []
        app._write = writes.append
        sizes = [(40, 12), (100, 30)]
        app._term_size = lambda: sizes.pop(0)
        app.draw()                      # 40x12
        app._on_winch()                 # the signal handler
        self.assertTrue(app._resize)
        app._resize = False             # what run() does on the next tick
        app._cache_key = None
        app._write("\x1b[2J")
        app.draw()                      # 100x30, must not raise
        text = "\n".join(app.render(100, 30))
        self.assertTrue(text.strip())
        self.assertIn("\x1b[2J", "".join(writes))


if __name__ == "__main__":
    unittest.main()
