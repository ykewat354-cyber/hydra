"""T21 regressions: --notify forwarding and wake-lock/notification evidence."""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hydra import cli
from hydra import config as C
from hydra import journal as J


class NotifyFlagTests(unittest.TestCase):
    def test_flag_parsed_and_forwarded_to_detached_child(self):
        o, rest = cli._parse(["--notify", "--detach", "do work"])
        self.assertTrue(o.notify)
        self.assertTrue(o.detach)
        self.assertEqual(rest, ["do work"])

        with tempfile.TemporaryDirectory() as home:
            old = os.environ.get("HYDRA_HOME")
            os.environ["HYDRA_HOME"] = home
            try:
                with mock.patch.object(cli.shutil, "which",
                                       return_value="/usr/bin/tmux"), \
                        mock.patch.object(cli.subprocess, "run") as run:
                    self.assertEqual(cli._spawn_detached({}, "do work", o), 0)
            finally:
                if old is None:
                    os.environ.pop("HYDRA_HOME", None)
                else:
                    os.environ["HYDRA_HOME"] = old
        inner = run.call_args.args[0][-1]
        self.assertIn("--notify", inner)
        self.assertIn("do work", inner)

    def test_detach_tmux_forwards_caller_environment(self):
        # A tmux server keeps the environment it was started with; a detached
        # run must still use this caller's HYDRA_HOME / PYTHONPATH (the T23
        # suite flake: a stale server made the run land in the wrong home).
        o, _rest = cli._parse(["--detach", "do work"])
        with tempfile.TemporaryDirectory() as home:
            old_home = os.environ.get("HYDRA_HOME")
            old_path = os.environ.get("PYTHONPATH")
            os.environ["HYDRA_HOME"] = home
            os.environ["PYTHONPATH"] = "/tmp/hydra-repo-test"
            try:
                with mock.patch.object(cli.shutil, "which",
                                       return_value="/usr/bin/tmux"), \
                        mock.patch.object(cli.subprocess, "run") as run:
                    self.assertEqual(cli._spawn_detached({}, "do work", o), 0)
            finally:
                if old_home is None:
                    os.environ.pop("HYDRA_HOME", None)
                else:
                    os.environ["HYDRA_HOME"] = old_home
                if old_path is None:
                    os.environ.pop("PYTHONPATH", None)
                else:
                    os.environ["PYTHONPATH"] = old_path
        cmd = run.call_args.args[0]
        self.assertIn("-e", cmd)
        self.assertIn(f"HYDRA_HOME={home}", cmd)
        self.assertIn("/tmp/hydra-repo-test", " ".join(cmd))
        self.assertNotIn("OLLAMA_API_KEY", " ".join(cmd))
        self.assertIn("-c", cmd)
        self.assertEqual(cmd[cmd.index("-c") + 1], os.getcwd())
        self.assertIn("cd " + os.getcwd(), cmd[-1])

    def test_flag_forces_notify_on_finish_for_the_run(self):
        o = cli.Opts()
        o.notify = True
        o.detach = True
        cfg = {"termux": {"notify_on_finish": False}}
        seen = {}

        def fake_spawn(c, task, opts):
            seen["cfg"] = c
            return 0

        with mock.patch.object(cli, "_key_preflight", return_value=True), \
                mock.patch.object(cli, "_spawn_detached", side_effect=fake_spawn):
            self.assertEqual(cli.cmd_run(cfg, "task", o), 0)
        self.assertIs(C.get(seen["cfg"], "termux.notify_on_finish"), True)


class NicetyEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.cwd = self.home / "cwd"
        self.bindir = self.home / "bin"
        self.cwd.mkdir()
        self.bindir.mkdir()
        self._old_home = os.environ.get("HYDRA_HOME")
        self._old_path = os.environ.get("PATH", "")
        os.environ["HYDRA_HOME"] = str(self.home / "hydra-home")
        os.environ["PATH"] = f"{self.bindir}{os.pathsep}{self._old_path}"

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home
        os.environ["PATH"] = self._old_path
        self.tmp.cleanup()

    def _shim(self, name: str, code: int) -> None:
        p = self.bindir / name
        p.write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
        p.chmod(0o755)

    def _runtime(self):
        from hydra.runtime import Runtime
        cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:9",
            "termux.wake_lock=true",
            "termux.notify_on_finish=true",
        ], project_dir=str(self.cwd))
        return Runtime(cfg, cwd=str(self.cwd), quiet=True)

    def _evidence(self):
        p = C.home() / "niceties.jsonl"
        return p, [json.loads(line) for line in
                   p.read_text(encoding="utf-8").splitlines() if line.strip()]

    def _assert_records(self, ok: bool, rc: int):
        rt = self._runtime()
        try:
            rt.wake_lock()                     # before journal -> buffered
            rt.start_run("nicety evidence")
            rt.notify("HYDRA done", "SECRET body must not be logged")
            events = J.read_events(rt.run_id)
        finally:
            rt.close()
        p, lines = self._evidence()
        self.assertEqual([r["kind"] for r in lines], ["wake_lock", "notify"])
        self.assertTrue(all(r["ok"] is ok for r in lines))
        self.assertTrue(all(r["returncode"] == rc for r in lines))
        self.assertEqual(lines[1]["title"], "HYDRA done")
        self.assertNotIn("SECRET", p.read_text(encoding="utf-8"))

        by_kind = {e["type"]: e for e in events
                   if e["type"] in ("wake_lock", "notify")}
        self.assertEqual(set(by_kind), {"wake_lock", "notify"})
        for ev in by_kind.values():
            self.assertIs(ev["ok"], ok)
            self.assertEqual(ev["returncode"], rc)
        self.assertEqual(by_kind["notify"]["title"], "HYDRA done")

    def test_wake_lock_and_notify_success_evidence(self):
        self._shim("termux-wake-lock", 0)
        self._shim("termux-notification", 0)
        self._assert_records(True, 0)

    def test_wake_lock_and_notify_failure_evidence(self):
        self._shim("termux-wake-lock", 3)
        self._shim("termux-notification", 3)
        self._assert_records(False, 3)


if __name__ == "__main__":
    unittest.main()
