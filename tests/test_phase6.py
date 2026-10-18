"""Phase 6 acceptance: Termux integration (§11, §13).

Acceptance (§17):
* `hydra --detach` survives closing the Termux UI (tmux when present,
  plain new-session otherwise)
* a notification fires on finish and on park (via termux-notification)
* dropping a *.md file in ~/.hydra/queue/ produces done/<name>.result.md
* the REPL steers a live run without blocking it
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from tests.test_phase1 import Base
from tests.test_phase2 import finish_node


def _env(home):
    env = dict(os.environ)
    env["HYDRA_HOME"] = home
    env["PYTHONPATH"] = str(REPO)
    return env


def _base_args(srv, *sets):
    args = [sys.executable, "-m", "hydra",
            "--set", f"api.host={srv.url}",
            "--set", "api.retries=2",
            "--set", "journal.fsync=event",
            "--set", "termux.wake_lock=false",
            "--set", "termux.notify_on_finish=false",
            "--set", "termux.notify_on_park=false"]
    for e in sets:
        args += ["--set", e]
    return args


def _wait_run(home, timeout=45):
    """Wait for the newest run under HYDRA_HOME to reach a final status."""
    runs = Path(home) / "runs"
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        if runs.exists():
            dirs = sorted((d for d in runs.iterdir() if d.is_dir()),
                          key=lambda d: d.stat().st_mtime)
            if dirs:
                last = dirs[-1]
                meta = last / "run.json"
                if meta.exists():
                    import json
                    try:
                        d = json.loads(meta.read_text())
                    except Exception:
                        d = {}
                    if d.get("status") in ("done", "partial", "failed",
                                           "parked", "killed", "interrupted",
                                           "auth-error"):
                        return last, d
        time.sleep(0.2)
    return last, None


class NotifyTests(Base):
    def test_notify_on_finish_and_park(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        bindir = Path(self.cwd.name) / "fakebin"
        bindir.mkdir()
        log = Path(self.cwd.name) / "notify.log"
        script = bindir / "termux-notification"
        script.write_text(f'#!/bin/sh\necho "$@" >> {log}\n')
        script.chmod(0o755)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{bindir}{os.pathsep}{old_path}"
        self.addCleanup(lambda: os.environ.__setitem__("PATH", old_path))
        try:
            # finish -> "HYDRA done"
            self.srv.add("gpt-oss:120b", finish_node("notify me"))
            rt = Runtime(self.cfg("termux.notify_on_finish=true",
                                  "termux.notify_on_park=true"),
                         cwd=self.cwd.name, quiet=True)
            try:
                code, report = rt.run("notify finish")
            finally:
                rt.close()
            self.assertEqual(code, 0, report)
            text = log.read_text()
            self.assertIn("HYDRA done", text)

            # park -> "HYDRA parked" + exit 75
            for m in ("gpt-oss:120b", "nemotron-3-ultra", "gemma4:31b"):
                self.srv.add(m, {"status": 402, "body": {"error": "paid"}})
            rt2 = Runtime(self.cfg("termux.notify_on_finish=true",
                                   "termux.notify_on_park=true"),
                          cwd=self.cwd.name, quiet=True)
            try:
                code2, _ = rt2.run("notify park")
            finally:
                rt2.close()
            self.assertEqual(code2, 75)
            self.assertIn("HYDRA parked", log.read_text())
        finally:
            os.environ["PATH"] = old_path


class DetachTests(Base):
    def test_detach_survives_parent_exit(self):
        # the run sleeps long enough that we can prove the parent returned first
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {"name": "bash",
                                                   "arguments": {"cmd": "sleep 3"}}}]},
                     finish_node("detached run done"))
        env = _env(self.home.name)
        t0 = time.time()
        proc = subprocess.run(_base_args(self.srv) + ["--detach", "detached task"],
                              cwd=self.cwd.name, env=env,
                              capture_output=True, text=True, timeout=30)
        elapsed = time.time() - t0
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("detached", proc.stdout.lower())
        self.assertLess(elapsed, 2.5, "detach must return immediately")
        rid_dir, meta = _wait_run(self.home.name, timeout=45)
        self.assertIsNotNone(meta, f"run never finished: {rid_dir}")
        self.assertEqual(meta.get("status"), "done")
        self.assertEqual(meta.get("exit_code"), 0)


class DaemonTests(Base):
    def test_queue_file_produces_result(self):
        self.srv.add("gpt-oss:120b", finish_node("daemon done"))
        qdir = Path(self.home.name) / "queue"
        qdir.mkdir(parents=True, exist_ok=True)
        (qdir / "hello.md").write_text("write the daemon note\n", encoding="utf-8")
        env = _env(self.home.name)
        proc = subprocess.run(_base_args(self.srv, "daemon.poll_secs=0.2")
                              + ["daemon", "--once"],
                              cwd=self.cwd.name, env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        out = qdir / "done" / "hello.result.md"
        self.assertTrue(out.exists(), proc.stdout + proc.stderr)
        body = out.read_text()
        self.assertIn("status: done", body)
        self.assertIn("exit_code: 0", body)
        self.assertIn("daemon done", body)
        # the source file is consumed (no double runs)
        self.assertFalse((qdir / "hello.md").exists())


class ReplTests(Base):
    def test_repl_steers_a_live_run(self):
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {"name": "bash",
                                                   "arguments": {"cmd": "sleep 1.2"}}}]},
                     finish_node("repl done"))
        env = _env(self.home.name)
        proc = subprocess.Popen(_base_args(self.srv) + ["-i"],
                                cwd=self.cwd.name, env=env,
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        out = ""
        try:
            proc.stdin.write("repl steering task\n")
            proc.stdin.flush()
            time.sleep(0.5)
            proc.stdin.write("extra context please\n")
            proc.stdin.flush()
            time.sleep(0.2)
            proc.stdin.write("/note hello from repl\n")
            proc.stdin.flush()
            proc.wait(timeout=60)
        finally:
            if proc.poll() is None:
                proc.kill()
            try:
                out = proc.stdout.read() if proc.stdout else ""
            except Exception:
                pass
            for f in (proc.stdin, proc.stdout):
                try:
                    f.close()
                except Exception:
                    pass
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("→ steered", out)
        self.assertIn("noted", out)
        rid_dir, meta = _wait_run(self.home.name, timeout=20)
        self.assertIsNotNone(meta, f"run never finished: {rid_dir}")
        self.assertEqual(meta.get("status"), "done")
        bb = rid_dir / "blackboard.jsonl"
        self.assertTrue(bb.exists(), "blackboard missing")
        self.assertIn("hello from repl", bb.read_text())


if __name__ == "__main__":
    unittest.main()
