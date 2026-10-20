"""WO3.2: named sessions, safer resume selection, TUI pickers and auto-resume.

The acceptance case is deliberately black-box: one old interrupted run and a
newer finished run live in the same cwd; plain `hydra resume` must continue the
interrupted one, not replay the newest completed session.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
from tests.fake_ollama import FakeOllama
from tests.test_phase2 import finish_node


def _dead_pid() -> int:
    p = subprocess.Popen([sys.executable, "-c", "pass"])
    p.wait(timeout=5)
    return int(p.pid)


def _make_run(home: Path, work: Path, rid: str, task: str, *,
              status: str = "running", started: float = None,
              pid: int = None, name: str = "", transcript: bool = True):
    d = Path(home) / "runs" / rid
    (d / "agents" / "a0").mkdir(parents=True)
    started = float(started if started is not None else time.time())
    meta = {"run": rid, "name": name, "task": task, "cwd": str(work),
            "status": status, "started": started,
            "pid": int(pid if pid is not None else _dead_pid()),
            "finished": None, "exit_code": None}
    (d / "run.json").write_text(json.dumps(meta), encoding="utf-8")
    (d / "events.jsonl").write_text(
        json.dumps({"ts": started, "run": rid, "seq": 1, "type": "run_start",
                    "agent": None, "task": task, "cwd": str(work),
                    "name": name}) + "\n", encoding="utf-8")
    if transcript:
        (d / "agents" / "a0" / "transcript.jsonl").write_text(
            json.dumps({"role": "user", "content": task}) + "\n",
            encoding="utf-8")
    return d


def _cfg(srv_url: str, work: Path):
    from hydra import config as C
    key = Path(tempfile.mkdtemp(prefix="hydra-wo32-key-")) / "key"
    key.write_text("fake-key\n", encoding="utf-8")
    return C.load_config(overrides=[
        f"api.host={srv_url}", "api.key_env=", f"api.key_file={key}",
        "loop.verify=never", "termux.wake_lock=false",
        "termux.notify_on_finish=false", "termux.notify_on_park=false",
        "termux.notify_on_resume=false",
    ], project_dir=str(work))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".hydra"
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()
        self._old = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = str(self.home)
        from hydra import sessions as S
        self.S = S

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old
        self.tmp.cleanup()

    def test_bare_resume_picks_interrupted_not_newest_done(self):
        now = time.time()
        _make_run(self.home, self.work, "r_old", "Build the OTT site",
                  started=now - 300)
        _make_run(self.home, self.work, "r_new", "A finished experiment",
                  status="done", started=now - 5)
        self.assertEqual(self.S.pick_resume(cwd=str(self.work)), "r_old")
        self.assertEqual(self.S.pick_resume(), "r_old")

    def test_finished_run_is_available_when_named(self):
        now = time.time()
        _make_run(self.home, self.work, "r_done", "A finished experiment",
                  status="done", started=now - 5, name="finished-one")
        self.assertEqual(self.S.resolve_run("finished-one"), "r_done")
        self.assertEqual(self.S.resolve_run("r_done"), "r_done")

    def test_placeholder_paste_runs_are_hidden_from_pickers(self):
        now = time.time()
        _make_run(self.home, self.work, "r_junk", "task with the paste",
                  started=now - 50)
        _make_run(self.home, self.work, "r_real", "Real interrupted work",
                  started=now - 300)
        self.assertEqual([r["run"] for r in self.S.session_rows()], ["r_real"])

    def test_auto_resume_ignores_deliberate_stop_and_old_runs(self):
        now = time.time()
        d = _make_run(self.home, self.work, "r_stopped", "Stopped by user",
                      started=now - 60)
        meta = json.loads((d / "run.json").read_text())
        meta["stop_reason"] = "user"
        (d / "run.json").write_text(json.dumps(meta), encoding="utf-8")
        _make_run(self.home, self.work, "r_old", "Old interrupted work",
                  started=now - 30 * 3600)
        self.assertIsNone(self.S.pick_auto_resume(max_age_hours=24))
        _make_run(self.home, self.work, "r_live", "Fresh interrupted work",
                  started=now - 60)
        self.assertEqual(self.S.pick_auto_resume(max_age_hours=24), "r_live")

    def test_auto_resume_skips_a_run_already_over_the_spend_cap(self):
        now = time.time()
        d = _make_run(self.home, self.work, "r_pricey", "Expensive work",
                      started=now - 60)
        (d / "usage.jsonl").write_text(
            json.dumps({"in": 100, "out": 10, "est_usd": 0.25}) + "\n",
            encoding="utf-8")
        self.assertIsNone(self.S.pick_auto_resume(max_age_hours=24,
                                                  max_run_usd=0.08))
        row = self.S.record("r_pricey")
        self.assertAlmostEqual(row["cost"], 0.25)


class ResumeBlackBoxTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOllama(tags=["gpt-oss:120b"]).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".hydra"
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()
        self._old = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = str(self.home)

    def tearDown(self):
        self.srv.stop()
        if self._old is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old
        self.tmp.cleanup()

    def test_cli_resume_chooses_the_interrupted_session(self):
        now = time.time()
        _make_run(self.home, self.work, "r_old", "Continue the old task",
                  started=now - 300)
        _make_run(self.home, self.work, "r_done", "Newer but finished",
                  status="done", started=now - 5)
        self.srv.add("gpt-oss:120b", finish_node("continued"))
        env = dict(os.environ, HYDRA_HOME=str(self.home),
                   PYTHONPATH=str(REPO))
        base = [sys.executable, "-m", "hydra",
                "--set", f"api.host={self.srv.url}",
                "--set", "api.retries=1", "--set", "loop.verify=never",
                "--set", "termux.wake_lock=false",
                "--set", "termux.notify_on_finish=false",
                "--set", "termux.notify_on_park=false"]
        p = subprocess.run(base + ["resume"], cwd=self.work, env=env,
                           capture_output=True, text=True, timeout=90)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertIn("resuming r_old", p.stdout)
        meta = json.loads((self.home / "runs" / "r_old" / "run.json").read_text())
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["resume_count"], 1)
        types = [json.loads(line)["type"] for line in
                 (self.home / "runs" / "r_old" / "events.jsonl").read_text().splitlines()]
        self.assertIn("run_resume", types)
        done_meta = json.loads((self.home / "runs" / "r_done" / "run.json").read_text())
        self.assertEqual(done_meta["status"], "done")

    def test_auto_resume_once_continues_a_killed_run(self):
        from hydra import cli
        now = time.time()
        d = _make_run(self.home, self.work, "r_killed", "Auto resume this",
                      started=now - 60)
        self.srv.add("gpt-oss:120b", finish_node("auto done"))
        with mock.patch("builtins.print"):
            ran = cli._auto_resume_once(_cfg(self.srv.url, self.work))
        self.assertTrue(ran)
        meta = json.loads((d / "run.json").read_text())
        self.assertEqual(meta["status"], "done")
        self.assertEqual(meta["auto_resume_attempts"], 1)
        with mock.patch("builtins.print"):
            self.assertFalse(cli._auto_resume_once(_cfg(self.srv.url, self.work)))

    def test_daemon_waits_for_network_instead_of_failing_the_run(self):
        from hydra import cli
        now = time.time()
        d = _make_run(self.home, self.work, "r_offline", "Wait for network",
                      started=now - 60)
        with mock.patch("hydra.cli._auto_resume_network", return_value=False):
            self.assertFalse(cli._auto_resume_once(
                _cfg(self.srv.url, self.work)))
        meta = json.loads((d / "run.json").read_text())
        self.assertEqual(meta["status"], "running")
        self.assertNotIn("auto_resume_attempts", meta)

    def test_run_lock_is_released_when_execution_finishes(self):
        from hydra.runtime import Runtime
        now = time.time()
        _make_run(self.home, self.work, "r_release", "Release the lock",
                  started=now - 60)
        self.srv.add("gpt-oss:120b", finish_node("lock released"))
        cfg = _cfg(self.srv.url, self.work)
        rt = Runtime(cfg, cwd=self.work, quiet=True)
        try:
            code, _report = rt.resume_run("r_release")
            self.assertEqual(code, 0)
            # The Runtime is deliberately still open (as in the TUI); the
            # per-run lock must already be free.
            import fcntl
            with open(self.home / "runs" / "r_release" / "run.lock",
                      "a+", encoding="utf-8") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            rt.close()

    def test_daemon_process_resumes_and_releases_its_lock(self):
        now = time.time()
        d = _make_run(self.home, self.work, "r_daemon", "Daemon auto resume",
                      started=now - 60)
        self.srv.add("gpt-oss:120b", finish_node("daemon done"))
        env = dict(os.environ, HYDRA_HOME=str(self.home),
                   PYTHONPATH=str(REPO))
        base = [sys.executable, "-m", "hydra",
                "--set", f"api.host={self.srv.url}",
                "--set", "api.retries=1", "--set", "loop.verify=never",
                "--set", "termux.wake_lock=false",
                "--set", "termux.notify_on_finish=false",
                "--set", "termux.notify_on_park=false",
                "--set", "termux.notify_on_resume=false"]
        proc = subprocess.Popen(base + ["daemon", "--resume"], cwd=self.work,
                                env=env, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 20
            meta = {}
            while time.time() < deadline:
                try:
                    meta = json.loads((d / "run.json").read_text())
                except Exception:
                    meta = {}
                if meta.get("status") in ("done", "failed", "interrupted"):
                    break
                time.sleep(0.1)
            self.assertEqual(meta.get("status"), "done")
            self.assertEqual(meta.get("auto_resume_attempts"), 1)
        finally:
            proc.terminate()
            try:
                out, _ = proc.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()
                out, _ = proc.communicate()
        self.assertIn("auto-resume r_daemon", out)
        self.assertFalse((self.home / "boot" / "daemon.pid").exists())


class RuntimeLockTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".hydra"
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()
        self._old = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = str(self.home)
        _make_run(self.home, self.work, "r_lock", "Locked task")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old
        self.tmp.cleanup()

    def test_resume_claims_pid_and_refuses_a_second_owner(self):
        from hydra import config as C
        from hydra.runtime import Runtime
        cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:1", "termux.wake_lock=false",
            "termux.notify_on_finish=false", "termux.notify_on_park=false",
        ], project_dir=str(self.work))
        a = Runtime(cfg, cwd=self.work, quiet=True)
        try:
            a.resume("r_lock")
            self.assertEqual(a.run_meta["pid"], os.getpid())
            self.assertEqual(a.run_meta["resume_count"], 1)
            b = Runtime(cfg, cwd=self.work, quiet=True)
            try:
                with self.assertRaises(SystemExit):
                    b.resume("r_lock")
            finally:
                b.close()
        finally:
            a.close()
        meta = json.loads((self.home / "runs" / "r_lock" / "run.json").read_text())
        self.assertEqual(meta["pid"], os.getpid())


class BootInstallTests(unittest.TestCase):
    def test_install_is_idempotent_and_uninstall_is_clean(self):
        from hydra import boot as B
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            out = B.install(home)
            self.assertTrue(out["boot"])
            script = home / ".termux" / "boot" / B.BOOT_NAME
            self.assertTrue(script.is_file())
            self.assertIn("daemon --resume", script.read_text())
            parsed = subprocess.run(["sh", "-n", str(script)],
                                    capture_output=True, text=True)
            self.assertEqual(parsed.returncode, 0, parsed.stderr)
            self.assertTrue(B._has_hook(home / ".bashrc"))
            plain = home / "plain-rc"
            plain.write_text("export KEEP=1\n", encoding="utf-8")
            self.assertFalse(B._has_hook(plain))
            B.install(home)
            for name in (".bashrc", ".zshrc"):
                text = (home / name).read_text()
                self.assertEqual(text.count(B.MARK_BEGIN), 1)
            B.uninstall(home, stop_daemon=False)
            self.assertFalse(script.exists())
            for name in (".bashrc", ".zshrc"):
                self.assertNotIn(B.MARK_BEGIN, (home / name).read_text())

    def test_daemon_lock_is_single_owner(self):
        from hydra import boot as B
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("HYDRA_HOME")
            os.environ["HYDRA_HOME"] = str(Path(tmp) / ".hydra")
            try:
                a = B.DaemonLock()
                b = B.DaemonLock()
                self.assertTrue(a.acquire())
                self.assertFalse(b.acquire())
                a.release()
                self.assertTrue(b.acquire())
                b.release()
            finally:
                if old is None:
                    os.environ.pop("HYDRA_HOME", None)
                else:
                    os.environ["HYDRA_HOME"] = old


class TuiPickerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / ".hydra"
        self.work = Path(self.tmp.name) / "work"
        self.work.mkdir()
        self._old = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = str(self.home)
        self.now = time.time()
        _make_run(self.home, self.work, "r_old", "Build the OTT dashboard",
                  started=self.now - 100, name="OTT dashboard")
        _make_run(self.home, self.work, "r_done", "A finished experiment",
                  status="done", started=self.now - 5)

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old
        self.tmp.cleanup()

    def _app(self):
        from hydra.tui_app import App
        cfg = _cfg("http://127.0.0.1:1", self.work)
        return App(cfg, cwd=str(self.work))

    def test_resume_picker_shows_names_status_and_activity(self):
        app = self._app()
        app.handle_command("/resume")
        self.assertIsNotNone(app.picker)
        self.assertEqual(app.picker["kind"], "resume")
        item = next(i for i in app.picker["items"] if i["value"] == "r_old")
        self.assertEqual(item["label"], "OTT dashboard")
        self.assertIn("interrupted", item["desc"])
        text = "\n".join(app.render(80, 31))
        self.assertIn("Resume a session", text)
        self.assertIn("OTT dashboard", text)
        self.assertIn("interrupted", text)

    def test_resume_picker_stays_readable_on_a_phone_width(self):
        event = {"ts": self.now, "type": "tool_call", "agent": "a0",
                 "tool": "read_file", "args": {"path": "lib/app.js"}}
        with open(self.home / "runs" / "r_old" / "events.jsonl", "a",
                  encoding="utf-8") as fh:
            fh.write(json.dumps(event) + "\n")
        app = self._app()
        app.handle_command("/resume")
        idx = next(i for i, item in enumerate(app.picker["items"])
                   if item["value"] == "r_old")
        app.picker["index"] = idx
        text = "\n".join(app.render(36, 31))
        self.assertIn("OTT dashboard", text)
        self.assertIn("interrupted", text)
        self.assertIn("1m", text)
        self.assertIn("read_file: lib/app.js", text)

    def test_picker_enter_opens_the_selected_run(self):
        app = self._app()
        app.handle_command("/resume")
        idx = next(i for i, item in enumerate(app.picker["items"])
                   if item["value"] == "r_old")
        app.picker["index"] = idx
        with mock.patch.object(app, "resume_run") as resume:
            app.handle_action("ENTER")
            resume.assert_called_once_with("r_old")
        self.assertIsNone(app.picker)

    def test_commands_picker_is_grouped_and_context_aware(self):
        app = self._app()
        app.handle_command("/commands")
        labels = [i.get("label") for i in app.picker["items"]]
        self.assertIn("Start & resume", labels)
        self.assertIn("/resume", labels)
        idx = next(i for i, item in enumerate(app.picker["items"])
                   if item.get("value") == "/resume")
        app.picker["index"] = idx
        app.handle_action("ENTER")
        self.assertEqual(app.input, "/resume ")
        self.assertIsNone(app.picker)
        app.handle_command("/commands")
        idx = next(i for i, item in enumerate(app.picker["items"])
                   if item.get("value") == "/new")
        app.picker["index"] = idx
        app.handle_action("ENTER")
        self.assertEqual(app.input, "/new ")


if __name__ == "__main__":
    unittest.main()
