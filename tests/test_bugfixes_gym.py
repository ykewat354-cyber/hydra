"""Regression tests for the gym-site failures (Oct 2026).

The run hung because a foreground shell exited while its background server
kept the stdout pipe open, the model repeated the same command for 40 minutes,
and the LLM verifier passed a claim that the server was hosted when it was not.
"""
from __future__ import annotations

import os
import shutil
import signal
import socket
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

from hydra import verifier
from hydra.tools.shell import run_shell

from tests.test_phase1 import Base


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split()[2]
        return state != "Z"
    except Exception:
        return True


def _closed_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])
    finally:
        s.close()


class ShellDeadlineTests(unittest.TestCase):
    def test_background_child_holding_pipe_still_times_out(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            # The shell exits immediately, but its child keeps writing to the
            # inherited stdout pipe.  Before the fix this loop waited forever.
            cmd = ("sh -c 'while :; do echo tick; sleep 0.05; done' "
                   "& echo $! > bg.pid")
            pid = None
            try:
                started = time.monotonic()
                out = run_shell(None, cwd, cmd, timeout=0.8)
                elapsed = time.monotonic() - started
                self.assertLess(elapsed, 5.0, out)
                self.assertIn("[timeout after 0.8s", out)
                self.assertIn("background=true", out)
                pid = int((cwd / "bg.pid").read_text().strip())
                for _ in range(40):
                    if not _alive(pid):
                        break
                    time.sleep(0.05)
                self.assertFalse(_alive(pid), f"child {pid} survived the timeout")
            finally:
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass

    @unittest.skipUnless(shutil.which("setsid"), "setsid is required")
    def test_daemonized_child_cannot_block_the_final_read(self):
        with tempfile.TemporaryDirectory() as td:
            cwd = Path(td)
            # setsid escapes the shell's process group, so the group kill
            # cannot reach it.  The final pipe drain must still be bounded.
            cmd = ("setsid sh -c 'while :; do echo tick; sleep 0.05; done' "
                   "& echo $! > bg.pid")
            pid = None
            try:
                started = time.monotonic()
                out = run_shell(None, cwd, cmd, timeout=0.8)
                self.assertLess(time.monotonic() - started, 5.0, out)
                self.assertIn("[timeout after 0.8s", out)
                pid = int((cwd / "bg.pid").read_text().strip())
            finally:
                if pid:
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except Exception:
                        pass


class HardStopTests(Base):
    def test_repeated_calls_stop_with_partial_report(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        # The real gym run repeated `killall python3`; the regression test must
        # not actually run that (it would SIGTERM this test runner on any
        # machine where killall exists).  Identical + harmless is enough.
        same = {"function": {"name": "bash",
                             "arguments": {"cmd": "echo hydra-stall-tick"}}}
        for _ in range(4):
            self.srv.add("*", {"tool_calls": [same]})

        rt = Runtime(self.cfg("loop.verify=never", "loop.stall_hard_stop=4"),
                     cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("loop breaker test")
            rid = rt.run_id
        finally:
            rt.close()

        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "partial")
        events = J.read_events(rid)
        hard = [e for e in events if e.get("type") == "stall_hard_stop"]
        self.assertTrue(hard, "hard loop breaker must fire")
        self.assertEqual(hard[0].get("repeated"), 4)
        self.assertEqual(len(self.srv.seen_models), 4,
                         "the model must not be called after the hard stop")
        self.assertIn("Loop breaker", report.get("summary", ""))


class DeterministicVerifierTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, summary: str, artifacts=("index.html",)):
        return SimpleNamespace(
            workdir=self.workdir,
            report={"status": "done", "summary": summary,
                    "files_changed": list(artifacts),
                    "artifacts": list(artifacts)},
            data={},
            task="build a website",
        )

    def _page(self, body: str = "<h1>Hi</h1>") -> None:
        (self.workdir / "index.html").write_text(
            "<!doctype html><html><head>"
            '<link rel="stylesheet" href="style.css">'
            '<script src="app.js"></script>'
            f"</head><body>{body}</body></html>",
            encoding="utf-8")
        (self.workdir / "style.css").write_text("body { color: #111; }\n",
                                                encoding="utf-8")
        (self.workdir / "app.js").write_text("const ready = true;\n",
                                             encoding="utf-8")

    def test_missing_local_asset_is_flagged_before_llm(self):
        (self.workdir / "index.html").write_text(
            "<html><body><link rel=\"stylesheet\" href=\"missing.css\">"
            "</body></html>", encoding="utf-8")
        agent = self._agent("built the site")
        issues = verifier.deterministic_issues(agent)
        self.assertTrue(any("missing local asset" in str(i) for i in issues),
                        issues)

        # Integration: a deterministic failure returns without a model call.
        res = verifier.check(SimpleNamespace(cfg={}, journal=None, router=None),
                             agent)
        self.assertEqual(res["verdict"], "fail")
        self.assertEqual(res["model"], "deterministic")

    def test_empty_javascript_is_flagged(self):
        (self.workdir / "index.html").write_text(
            "<html><body><script src=\"app.js\"></script></body></html>",
            encoding="utf-8")
        (self.workdir / "app.js").write_text("", encoding="utf-8")
        issues = verifier.deterministic_issues(
            self._agent("built the site", ("index.html", "app.js")))
        self.assertTrue(any("is empty" in str(i) for i in issues), issues)

    def test_dead_localhost_claim_is_flagged(self):
        self._page()
        port = _closed_port()
        issues = verifier.deterministic_issues(self._agent(
            f"Site is hosted at http://127.0.0.1:{port}"))
        self.assertTrue(any("not reachable" in str(i) for i in issues), issues)

    def test_valid_static_site_passes_offline_checks(self):
        self._page()
        self.assertEqual(verifier.deterministic_issues(
            self._agent("Built a static site.")), [])


if __name__ == "__main__":
    unittest.main()
