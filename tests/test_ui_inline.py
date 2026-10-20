"""WO2 M2 — inline rendering: split logic, paint bytes and the pty go/no-go.

Inline mode keeps the transcript in the terminal's native scrollback (so
Termux keeps swipe-scroll and long-press copy) and only repaints a live region
at the bottom. The pure half is exercised directly; the real TUI is then run
through a pty and fed into ``tests/vt.py``, a small terminal emulator.
"""
from __future__ import annotations

import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests import vt as vtmod                  # noqa: E402
from hydra import config as C                 # noqa: E402
from hydra import tui_inline as TI            # noqa: E402
from hydra.tui_app import App                 # noqa: E402

HYDRA_CMD = [sys.executable, "-m", "hydra"]


def _cfg(tmp: Path):
    return C.load_config(overrides=[
        "api.host=http://127.0.0.1:1",
        "termux.wake_lock=false",
        "termux.notify_on_finish=false",
        "termux.notify_on_park=false",
        "ui.mode=inline",
    ], project_dir=str(tmp))


def _env(home: str) -> dict:
    env = dict(os.environ)
    env.update(HYDRA_HOME=home, PYTHONPATH=str(REPO), TERM="xterm-256color",
               NO_COLOR="1")
    env.pop("HYDRA_ASCII", None)
    return env


def _set_size(fd: int, rows: int, cols: int):
    fcntl.ioctl(fd, termios.TIOCSWINSZ,
                struct.pack("HHHH", int(rows), int(cols), 0, 0))


def _read_until(master: int, pred, timeout: float = 20.0) -> bytes:
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.2)
        if r:
            try:
                d = os.read(master, 65536)
            except OSError:
                break
            buf += d
        if pred(buf):
            break
    return buf


def _read_for(master: int, seconds: float) -> bytes:
    buf = b""
    end = time.time() + seconds
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.1)
        if r:
            try:
                buf += os.read(master, 65536)
            except OSError:
                break
    return buf


def _settle(master: int, buf: bytes, quiet: float = 0.8,
            timeout: float = 40.0) -> bytes:
    """Read until the stream has been quiet for *quiet* seconds."""
    end = time.time() + timeout
    last = time.time()
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.2)
        if r:
            try:
                d = os.read(master, 65536)
            except OSError:
                break
            if d:
                buf += d
                last = time.time()
        elif time.time() - last > quiet:
            break
    return buf


def _wait_exit(master: int, proc: subprocess.Popen,
               timeout: float = 25.0) -> bytes:
    buf = b""
    end = time.time() + timeout
    while time.time() < end:
        r, _, _ = select.select([master], [], [], 0.2)
        if r:
            try:
                buf += os.read(master, 65536)
            except OSError:
                pass
        if proc.poll() is not None:
            buf += _read_for(master, 0.4)
            break
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=5)
    return buf


class SplitTests(unittest.TestCase):
    """The commit/live decision, against the real renderer."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.app = App(_cfg(Path(self.tmp.name)), cwd=self.tmp.name)

    def test_welcome_is_live_until_the_first_message(self):
        committed, live = TI.split_blocks(self.app._block_rows(54))
        self.assertEqual([], committed)
        self.assertIn("HYDRA", "\n".join(t for t, _ in live))

    def test_welcome_commits_once_tips_hide(self):
        self.app.add_user("hi")
        committed, live = TI.split_blocks(self.app._block_rows(54))
        self.assertEqual([], live)
        text = "\n".join(t for t, _ in committed)
        self.assertIn("HYDRA", text)
        self.assertNotIn("@file attach", text)   # tips line hidden

    def test_pending_tool_row_is_live_and_then_commits(self):
        self.app.add_user("hi")
        self.app.event({"type": "tool_call", "agent": "a0", "tool": "bash",
                        "args": {"cmd": "sleep 1"}})
        self.app.drain()
        committed, live = TI.split_blocks(self.app._block_rows(54))
        self.assertTrue(committed)
        self.assertIn("bash", "\n".join(t for t, _ in live))
        self.app.event({"type": "tool_result", "agent": "a0", "tool": "bash",
                        "preview": "ok", "chars": 2, "ms": 30, "lines": 1})
        self.app.drain()
        committed, live = TI.split_blocks(self.app._block_rows(54))
        self.assertEqual([], live)
        self.assertIn("bash", "\n".join(t for t, _ in committed))

    def test_streaming_answer_commits_all_but_the_last_row(self):
        self.app.add_user("write something")
        self.app.add_assistant("word " * 40, final=False)
        self.app.drain()
        committed, live = TI.split_blocks(self.app._block_rows(30))
        self.assertEqual(1, len(live))
        self.assertIn("word", live[0][0])
        self.assertTrue(any("word" in t for t, _ in committed))

    def test_final_answer_commits_whole(self):
        self.app.add_user("write something")
        self.app.add_assistant("word " * 40, final=True)
        self.app.drain()
        committed, live = TI.split_blocks(self.app._block_rows(30))
        self.assertEqual([], live)
        self.assertTrue(any("word" in t for t, _ in committed))

    def test_running_spawn_row_is_live_then_commits(self):
        self.app.add_user("hi")
        self.app.event({"type": "agent_spawn", "agent": "a1", "role": "grep",
                        "model": "gemma4:31b", "task": "look"})
        self.app.drain()
        _committed, live = TI.split_blocks(self.app._block_rows(54))
        self.assertIn("a1", "\n".join(t for t, _ in live))
        self.app.event({"type": "agent_end", "agent": "a1",
                        "status": "done"})
        self.app.drain()
        _committed, live = TI.split_blocks(self.app._block_rows(54))
        self.assertNotIn("a1", "\n".join(t for t, _ in live))


class PaintTests(unittest.TestCase):
    def test_first_frame_has_no_moves(self):
        chunks, drawn, below = TI.paint(["a"], 0, 0, (0, 0), 10)
        text = "".join(chunks)
        self.assertNotIn("\x1b[0A", text)
        self.assertEqual(1, drawn)
        self.assertEqual(0, below)
        self.assertIn("\x1b[?25h", text)

    def test_repaint_steps_down_then_up(self):
        chunks, drawn, below = TI.paint(["a", "b", "c"], 3, 2, (0, 0), 10)
        text = "".join(chunks)
        self.assertIn("\x1b[2B", text)          # back to the region bottom
        self.assertIn("\x1b[2A", text)          # up to its top (3 rows)
        self.assertEqual(3, drawn)
        self.assertEqual(2, below)

    def test_shrinking_region_clears_leftover_rows(self):
        chunks, drawn, below = TI.paint(["x"], 4, 0, None, 10)
        text = "".join(chunks)
        self.assertEqual(4, drawn)
        self.assertEqual(4, text.count("\x1b[2K"))

    def test_unprint_clears_the_region(self):
        self.assertEqual([], TI.unprint(0, 3))
        text = "".join(TI.unprint(3, 1))
        self.assertIn("\x1b[1B", text)
        self.assertIn("\x1b[2A\x1b[J", text)

    def test_one_line_region_needs_no_cursor_moves(self):
        chunks, drawn, below = TI.paint(["x"], 1, 0, (0, 3), 10)
        self.assertEqual(1, drawn)
        self.assertEqual(0, below)
        self.assertIn("\x1b[3C", "".join(chunks))


class InlinePtyTests(unittest.TestCase):
    """§7 go/no-go: the real TUI under a real pty in inline mode."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self.addCleanup(self.home.cleanup)
        self.addCleanup(self.cwd.cleanup)
        self.env = _env(self.home.name)

    def _start(self, rows: int = 31, cols: int = 54):
        master, slave = pty.openpty()
        _set_size(master, rows, cols)
        cmd = HYDRA_CMD + [
            "-i",
            "--set", "api.host=http://127.0.0.1:1",
            "--set", "termux.wake_lock=false",
            "--set", "termux.notify_on_finish=false",
            "--set", "termux.notify_on_park=false",
            "--set", "ui.mode=inline",
        ]
        try:
            proc = subprocess.Popen(cmd, cwd=self.cwd.name, env=self.env,
                                    stdin=slave, stdout=slave, stderr=slave)
        finally:
            os.close(slave)
        buf = _read_until(master, lambda b: "╭".encode() in b, timeout=25)
        buf += _read_for(master, 0.3)
        return proc, master, buf

    def _finish(self, proc, master, buf):
        buf += _wait_exit(master, proc)
        try:
            os.close(master)
        except OSError:
            pass
        return buf

    def test_go_no_go_no_alt_screen_no_mouse(self):
        proc, master, buf = self._start()
        self.assertIn(b"\x1b[?2004h", buf)          # bracketed paste on
        self.assertIn(b"\x1b[?1007h", buf)          # alternate scroll on
        self.assertNotIn(b"\x1b[?1049h", buf)       # never the alt screen
        self.assertNotIn(b"\x1b[?1049l", buf)
        self.assertNotIn(b"\x1b[?1000h", buf)       # no mouse capture
        self.assertNotIn(b"\x1b[?1002h", buf)
        self.assertNotIn(b"\x1b[?1006h", buf)
        os.write(master, b"/quit\r")
        buf = self._finish(proc, master, buf)
        self.assertEqual(0, proc.returncode, buf[-800:])
        self.assertIn(b"\x1b[?2004l", buf)
        self.assertIn(b"\x1b[?25h", buf)

    def test_screen_keeps_exactly_one_input_box(self):
        proc, master, buf = self._start(rows=31, cols=54)
        vt = vtmod.VT(54, 31)
        vt.feed(buf)
        self.assertEqual(1, vt.count_screen("╭"))
        self.assertEqual(1, vt.count_screen("╰"))
        from hydra import __version__
        self.assertEqual(1, vt.count_screen(f"HYDRA {__version__}"))
        os.write(master, b"/quit\r")
        buf = self._finish(proc, master, buf)
        self.assertEqual(0, proc.returncode, buf[-800:])

    def test_resize_repaints_without_residue(self):
        proc, master, buf = self._start(rows=31, cols=54)
        _set_size(master, 20, 36)
        os.kill(proc.pid, signal.SIGWINCH)
        post = _read_for(master, 1.2)
        vt = vtmod.VT(36, 20)
        vt.feed(post)
        self.assertEqual(1, vt.count_screen("╭"))
        self.assertEqual(1, vt.count_screen("╰"))
        os.write(master, b"/quit\r")
        buf = self._finish(proc, master, buf)
        self.assertEqual(0, proc.returncode, buf[-800:])

    def test_multi_step_resize_54_36_80(self):
        """T-UI3: two resizes in a row; the idle loop must wake on SIGWINCH.

        The loop blocks in select with no timeout when idle (§5.8), so a
        repaint here also proves the signal self-pipe works."""
        proc, master, buf = self._start(rows=31, cols=54)
        for rows, cols in ((20, 36), (40, 80)):
            _set_size(master, rows, cols)
            os.kill(proc.pid, signal.SIGWINCH)
            post = _read_for(master, 1.5)
            self.assertTrue(post, f"no repaint after SIGWINCH {cols}x{rows}")
            vt = vtmod.VT(cols, rows)
            vt.feed(post)
            self.assertEqual(1, vt.count_screen("╭"),
                             f"{cols}x{rows}: box top")
            self.assertEqual(1, vt.count_screen("╰"),
                             f"{cols}x{rows}: box bottom")
            self.assertNotIn(b"\x1b[?1049h", post)
        os.write(master, b"/quit\r")
        buf = self._finish(proc, master, buf)
        self.assertEqual(0, proc.returncode, buf[-800:])
        self.assertIn(b"\x1b[?25h", buf)

    def test_transcript_row_prints_once(self):
        proc, master, buf = self._start()
        os.write(master, b"hello there\r")
        buf = _settle(master, buf)
        vt = vtmod.VT(54, 31)
        vt.feed(buf)
        self.assertEqual(1, vt.all_text().count("hello there"),
                         vt.all_text()[-500:])
        self.assertNotIn(b"\x1b[?1049h", buf)
        os.write(master, b"/quit\r")
        buf = self._finish(proc, master, buf)
        self.assertEqual(0, proc.returncode, buf[-800:])

    def _signal_restore(self, sig):
        proc, master, buf = self._start()
        os.kill(proc.pid, sig)
        buf = self._finish(proc, master, buf)
        self.assertEqual(0, proc.returncode, buf[-800:])
        self.assertIn(b"\x1b[?2004l", buf)     # bracketed paste off
        self.assertIn(b"\x1b[?1007l", buf)     # alternate scroll off
        self.assertIn(b"\x1b[?25h", buf)       # cursor visible again
        self.assertNotIn(b"\x1b[?1049h", buf)  # never entered the alt screen
        return buf

    def test_sigterm_restores_the_terminal(self):
        self._signal_restore(signal.SIGTERM)

    def test_sighup_restores_the_terminal(self):
        self._signal_restore(signal.SIGHUP)


if __name__ == "__main__":
    unittest.main()
