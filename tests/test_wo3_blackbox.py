"""WO3 acceptance: drive the public CLI in a clean HOME on the fake server."""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from tests.fake_ollama import FakeOllama

REPO = Path(__file__).resolve().parents[1]
ROLES = ("lead", "worker", "researcher", "coder", "reviewer", "summarizer",
         "critic", "planner", "vision", "debugger", "tester", "scribe")


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *_a):
        pass


class Blackbox(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOllama(tags=["fake"]).start()
        self.tmp = Path(tempfile.mkdtemp())
        self.home = self.tmp / "home"
        self.hydra_home = self.home / ".hydra"
        self.hydra_home.mkdir(parents=True)
        (self.hydra_home / "key").write_text("fake-key", encoding="utf-8")
        self.work = self.tmp / "work"
        self.work.mkdir()

    def tearDown(self):
        self.srv.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self):
        env = os.environ.copy()
        env.update({"HOME": str(self.home), "HYDRA_HOME": str(self.hydra_home),
                    "PYTHONPATH": str(REPO), "NO_COLOR": "1",
                    "PYTHONUNBUFFERED": "1"})
        env.pop("OLLAMA_API_KEY", None)
        return env

    def run_cli(self, task, nodes, *, cwd=None, verify="auto", rounds=1,
                timeout=120):
        self.srv.script["fake"] = list(nodes)
        args = [sys.executable, "-m", "hydra", "--plain", "--no-color",
                "--set", f"api.host={self.srv.url}",
                "--set", "api.retries=1",
                "--set", "termux.wake_lock=false",
                "--set", "termux.notify_on_finish=false",
                "--set", "termux.notify_on_park=false",
                "--set", "loop.max_turns=12",
                "--set", f"loop.verify={verify}",
                "--set", f"loop.verify_rounds={rounds}",
                "--set", "routing.json_policy=role_chain",
                "--set", "agents.max_agents=4"]
        for role in ROLES:
            args += ["--set", f'roles.{role}.chain=["fake"]']
        args.append(task)
        p = subprocess.run(args, cwd=str(cwd or self.work), env=self._env(),
                           capture_output=True, text=True, timeout=timeout)
        runs = sorted((self.hydra_home / "runs").glob("r_*"),
                      key=lambda d: d.stat().st_mtime) if \
            (self.hydra_home / "runs").is_dir() else []
        reports, events = [], []
        if runs:
            for line in (runs[-1] / "events.jsonl").read_text(
                    encoding="utf-8", errors="replace").splitlines():
                try:
                    ev = json.loads(line)
                except Exception:
                    continue
                events.append(ev)
                if ev.get("type") == "report" and isinstance(ev.get("report"), dict):
                    reports.append(ev["report"])
        self.assertTrue(reports, "no report event\nSTDOUT:\n" + p.stdout[-3000:] +
                        "\nSTDERR:\n" + p.stderr[-3000:])
        return p, reports[-1], events

    # ---------------------------------------------------------------- A4
    def test_a4_duplicate_project_server_is_reused_not_restarted(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        cmd = f"timeout 8 python -m http.server {port}"
        script = [{
            "content": "starting the server",
            "tool_calls": [{"function": {"name": "bash", "arguments": {
                "cmd": cmd, "background": True}}}],
        }, {
            "content": "starting it again by mistake",
            "tool_calls": [{"function": {"name": "bash", "arguments": {
                "cmd": cmd, "background": True}}}],
        }, {
            "content": "done",
            "tool_calls": [{"function": {"name": "finish", "arguments": {
                "status": "done",
                "summary": f"Started the server at http://127.0.0.1:{port}.",
                "commands_run": [cmd]}}}],
        }, {"content": '{"verdict":"pass","issues":[]}'}]
        p, rep, events = self.run_cli(
            "Start the project server once and leave it running", script)
        starts = [e for e in events if e.get("type") == "job_start"]
        self.assertEqual(len(starts), 1, p.stderr[-1000:])
        previews = "\n".join(str(e.get("preview") or "") for e in events
                             if e.get("type") == "tool_result")
        self.assertTrue("already serving" in previews or
                        "already running" in previews or
                        "reuse" in previews.lower(), previews[-1500:])
        jobs = rep.get("jobs") or []
        self.assertEqual(len(jobs), 1, rep)
        self.assertIn(str(port), str(jobs[0].get("url") or ""))
        self.assertTrue(str(rep.get("served_url") or "").startswith(
            f"http://127.0.0.1:{port}"), rep)
        self.assertTrue([e for e in events if e.get("type") == "served_probe"
                         and int(e.get("status") or 0) == 200], events[-20:])

    def test_a4_served_url_survives_stopping_the_job(self):
        # P1 regression: the agent may stop the server before finish.  HYDRA's
        # own bounded GET at job_start must still put served_url in the report.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        cmd = f"timeout 30 python -m http.server {port}"
        script = [{
            "content": "starting the server",
            "tool_calls": [{"function": {"name": "bash", "arguments": {
                "cmd": cmd, "background": True}}}],
        }, {
            "content": "stopping it before finish",
            "tool_calls": [{"function": {"name": "job_kill", "arguments": {
                "id": "j1"}}}],
        }, {
            "content": "done",
            "tool_calls": [{"function": {"name": "finish", "arguments": {
                "status": "done",
                "summary": f"Served the site on http://127.0.0.1:{port} — HTTP 200."}}}],
        }, {
            "content": "still stopped",
            "tool_calls": [{"function": {"name": "finish", "arguments": {
                "status": "done",
                "summary": f"Served the site on http://127.0.0.1:{port} — HTTP 200."}}}],
        }]
        _p, rep, events = self.run_cli(
            "Serve the site on one port, check it returns 200, then finish",
            script)
        self.assertTrue(str(rep.get("served_url") or "").startswith(
            f"http://127.0.0.1:{port}"), rep)
        self.assertFalse(rep.get("served_alive"), rep)
        self.assertEqual("start", rep.get("served_probe_at"), rep)
        self.assertEqual("partial", rep.get("status"), rep)
        self.assertIn("server not running at finish",
                      "\n".join(rep.get("open_issues") or []))
        probes = [e for e in events if e.get("type") == "served_probe"
                  and int(e.get("status") or 0) == 200]
        self.assertTrue(probes, events[-20:])
        jobs = rep.get("jobs") or []
        self.assertEqual(len(jobs), 1, rep)
        self.assertEqual("exited", jobs[0].get("state"), rep)

    def test_a4_shell_ampersand_server_is_tracked(self):
        # P1/A4: models often write ``python -m http.server 8080 &`` without
        # background=true; the harness must still create a tracked job.
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        cmd = f"timeout 30 python3 -m http.server {port} > /dev/null 2>&1 &"
        script = [{
            "content": "starting the server",
            "tool_calls": [{"function": {"name": "bash", "arguments": {
                "cmd": cmd}}}],
        }, {
            "content": "checking it",
            "tool_calls": [{"function": {"name": "bash", "arguments": {
                "cmd": f"sleep 1; curl -s -o /dev/null -w '%{{http_code}}' "
                       f"http://127.0.0.1:{port}/"}}}],
        }, {
            "content": "done",
            "tool_calls": [{"function": {"name": "finish", "arguments": {
                "status": "done",
                "summary": f"Served on http://127.0.0.1:{port} — HTTP 200."}}}],
        }, {"content": '{"verdict":"pass","issues":[]}'}]
        _p, rep, events = self.run_cli(
            "Serve the site on one port, check it returns 200, then finish",
            script)
        starts = [e for e in events if e.get("type") == "job_start"]
        self.assertEqual(len(starts), 1, events[-10:])
        self.assertEqual(int(starts[0].get("port") or 0), port, starts)
        jobs = rep.get("jobs") or []
        self.assertEqual(len(jobs), 1, rep)
        self.assertTrue(str(rep.get("served_url") or "").startswith(
            f"http://127.0.0.1:{port}"), rep)

    # ---------------------------------------------------------------- A1
    def test_a1_files_changed_with_20k_home_files(self):
        junk = self.home / "junk"
        junk.mkdir()
        for i in range(20000):
            (junk / f"j{i}.txt").write_text("x", encoding="utf-8")
        script = [{
            "content": "writing the site with shell tools",
            "tool_calls": [{"function": {"name": "bash", "arguments": {"cmd":
                "mkdir -p proj && cat > proj/index.html <<'EOF'\n"
                "<!doctype html><title>hi</title>\nEOF\n"
                "cp proj/index.html proj/copy.html && "
                "printf 'console.log(1)\\n' | tee proj/app.js >/dev/null"}}}],
        }, {
            "content": "done",
            "tool_calls": [{"function": {"name": "finish", "arguments": {
                "status": "done",
                "summary": "Created proj/index.html, proj/copy.html and proj/app.js.",
                "commands_run": ["shell heredoc/cp/tee"]}}}],
        }, {"content": '{"verdict":"pass","issues":[]}'}]
        t0 = time.time()
        p, rep, _evs = self.run_cli(
            "Create a tiny site in proj/ using shell commands", script,
            cwd=self.home)
        elapsed = time.time() - t0
        self.assertIn(rep["status"], ("done", "partial"), p.stdout[-1000:])
        files = rep.get("files_changed")
        self.assertIsInstance(files, list, rep)
        for want in ("proj/index.html", "proj/copy.html", "proj/app.js"):
            self.assertIn(want, files, rep)
        self.assertTrue(rep.get("scan_complete"))
        self.assertIn(rep.get("files_changed_method"), ("scan", "mixed"))
        self.assertFalse([f for f in files if ".hydra" in f or "junk/" in f])
        self.assertLess(elapsed, 60)

    # ---------------------------------------------------------------- P3
    def _outside_write_run(self):
        cmd = ("python -c \"import os; "
               "open(os.path.expanduser('~/other/x.txt'),'w').write('x')\"")
        script = [
            {"content": "writing outside the cwd",
             "tool_calls": [{"function": {"name": "bash",
                                          "arguments": {"cmd": cmd}}}]},
            {"content": "done",
             "tool_calls": [{"function": {"name": "finish", "arguments": {
                 "status": "done", "summary": "wrote ~/other/x.txt"}}}]},
        ]
        return self.run_cli("write ~/other/x.txt with python", script,
                            cwd=self.work, verify="never")

    def test_p3_outside_root_write_is_reported_or_honest_null(self):
        # ~/other does not exist: the root walk reaches $HOME (never scanned),
        # so the honest answer is null + a note, not [] + complete=true.
        _p, rep, _evs = self._outside_write_run()
        files = rep.get("files_changed")
        self.assertIsNone(files, rep)
        self.assertFalse(rep.get("scan_complete"))
        self.assertIn("outside", str(rep.get("scan_note") or ""))

    def test_p3_outside_root_write_is_reported_when_parent_exists(self):
        (self.home / "other").mkdir()
        _p, rep, _evs = self._outside_write_run()
        files = rep.get("files_changed")
        self.assertIsInstance(files, list, rep)
        self.assertTrue(rep.get("scan_complete"), rep)
        self.assertTrue(any("other/x.txt" in str(f) for f in files), rep)
        self.assertTrue(any(str(self.home / "other") in str(r)
                            for r in (rep.get("scan_roots") or [])), rep)

    # ---------------------------------------------------------------- A2
    def test_a2_removed_claim_on_js_only_change_passes(self):
        (self.work / "js").mkdir()
        (self.work / "js" / "script.js").write_text("const FIR = 1;\n")
        script = [
            {"content": "removing FIR",
             "tool_calls": [{"function": {"name": "write_file", "arguments": {
                 "path": "js/script.js", "content": "const x = 1;\n"}}}]},
            {"content": "done", "tool_calls": [{"function": {"name": "finish",
             "arguments": {"status": "done",
                           "summary": "Removed FIR from js/script.js."}}}]},
            {"content": '{"verdict":"pass","issues":[]}'},
        ]
        p, rep, evs = self.run_cli("remove FIR from the JavaScript", script)
        self.assertEqual(rep["status"], "done", p.stdout[-2000:])
        self.assertIn("js/script.js", rep.get("files_changed") or [])
        self.assertFalse([e for e in evs if e.get("type") == "verify" and
                          e.get("verdict") == "fail"])

    def test_a2_removed_claim_still_present_is_rejected(self):
        (self.work / "js").mkdir()
        (self.work / "js" / "script.js").write_text("const FIR = 1;\n")
        script = [
            {"content": "trying a fix",
             "tool_calls": [{"function": {"name": "write_file", "arguments": {
                 "path": "js/script.js", "content": "const FIR = 2;\n"}}}]},
            {"content": "done", "tool_calls": [{"function": {"name": "finish",
             "arguments": {"status": "done",
                           "summary": "Removed FIR from js/script.js."}}}]},
        ]
        p, rep, evs = self.run_cli("remove FIR from the JavaScript", script)
        self.assertEqual(rep["status"], "partial", p.stdout[-2000:])
        joined = "\n".join(rep.get("open_issues") or [])
        self.assertIn("still found", joined)
        self.assertIn("js/script.js", joined)

    # ---------------------------------------------------------------- A3
    def test_a3_later_success_is_evidence(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        try:
            script = [
                {"content": "first try",
                 "tool_calls": [{"function": {"name": "bash", "arguments": {
                     "cmd": "curl -sS -o /dev/null -w '%{http_code}' "
                            "http://127.0.0.1:1/ 2>&1 || true"}}}]},
                {"content": "retry",
                 "tool_calls": [{"function": {"name": "bash", "arguments": {
                     "cmd": f"curl -sS -o /dev/null -w '%{{http_code}}' {url}"}}}]},
                {"content": "done", "tool_calls": [{"function": {"name": "finish",
                 "arguments": {"status": "done",
                               "summary": f"Serving {url} — curl returned HTTP 200."}}}]},
                {"content": '{"verdict":"pass","issues":[]}'},
            ]
            p, rep, _evs = self.run_cli("prove the local server works", script)
            self.assertEqual(rep["status"], "done", p.stdout[-2000:])
            self.assertEqual(rep.get("served_url"), url)
            self.assertTrue(rep.get("evidence"), rep)
        finally:
            httpd.shutdown()

    def test_a3_success_then_failure_is_rejected(self):
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{httpd.server_address[1]}/"
        try:
            script = [
                {"content": "check",
                 "tool_calls": [{"function": {"name": "bash", "arguments": {
                     "cmd": f"curl -sS -o /dev/null -w '%{{http_code}}' {url}"}}}]},
                {"content": "recheck",
                 "tool_calls": [{"function": {"name": "bash", "arguments": {
                     "cmd": "curl -sS -o /dev/null -w '%{http_code}' "
                            "http://127.0.0.1:1/ 2>&1 || true"}}}]},
                {"content": "done", "tool_calls": [{"function": {"name": "finish",
                 "arguments": {"status": "done",
                               "summary": f"Serving {url} — HTTP 200."}}}]},
            ]
            p, rep, _evs = self.run_cli("prove the local server works", script)
            self.assertEqual(rep["status"], "partial", p.stdout[-2000:])
            self.assertIn("latest evidence is a failure",
                          "\n".join(rep.get("open_issues") or []))
        finally:
            httpd.shutdown()

    # ---------------------------------------------------------------- Q2
    def test_q2_nudge_restarts_a_stopped_server(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        cmd = f"timeout 20 python -m http.server {port}"
        script = [
            {"content": "start", "tool_calls": [{"function": {
                "name": "bash", "arguments": {"cmd": cmd, "background": True}}}]},
            {"content": "stop too early", "tool_calls": [{"function": {
                "name": "job_kill", "arguments": {"id": "j1"}}}]},
            {"content": "finish", "tool_calls": [{"function": {
                "name": "finish", "arguments": {
                    "status": "done", "summary": "served the site"}}}]},
            {"content": "restart after the nudge", "tool_calls": [{"function": {
                "name": "bash", "arguments": {"cmd": cmd, "background": True}}}]},
            {"content": "finish alive", "tool_calls": [{"function": {
                "name": "finish", "arguments": {
                    "status": "done", "summary": "served the site"}}}]},
            {"content": '{"verdict":"pass","issues":[]}'},
        ]
        p, rep, events = self.run_cli(
            "Serve the site on one port, check it returns 200, then finish",
            script)
        self.assertEqual("done", rep.get("status"), p.stdout[-2000:])
        self.assertTrue(rep.get("served_alive"), rep)
        self.assertEqual("finish", rep.get("served_probe_at"), rep)
        self.assertTrue(str(rep.get("served_url") or "").startswith(
            f"http://127.0.0.1:{port}"), rep)
        self.assertEqual(1, len([e for e in events
                                 if e.get("type") == "server_nudge"]), events[-30:])
        self.assertFalse([e for e in events if e.get("type") == "server_finish"])

    def test_q2_ignored_nudge_finishes_partial(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        cmd = f"timeout 20 python -m http.server {port}"
        script = [
            {"content": "start", "tool_calls": [{"function": {
                "name": "bash", "arguments": {"cmd": cmd, "background": True}}}]},
            {"content": "stop", "tool_calls": [{"function": {
                "name": "job_kill", "arguments": {"id": "j1"}}}]},
            {"content": "finish", "tool_calls": [{"function": {
                "name": "finish", "arguments": {
                    "status": "done", "summary": "served the site"}}}]},
            {"content": "finish anyway", "tool_calls": [{"function": {
                "name": "finish", "arguments": {
                    "status": "done", "summary": "served the site"}}}]},
        ]
        p, rep, events = self.run_cli(
            "Serve the site on one port, check it returns 200, then finish",
            script)
        self.assertEqual("partial", rep.get("status"), p.stdout[-2000:])
        self.assertFalse(rep.get("served_alive"), rep)
        self.assertEqual("start", rep.get("served_probe_at"), rep)
        self.assertIn("server not running at finish",
                      "\n".join(rep.get("open_issues") or []))
        self.assertEqual(1, len([e for e in events
                                 if e.get("type") == "server_nudge"]), events[-30:])
        self.assertTrue([e for e in events if e.get("type") == "server_finish"])

    def test_q2_prose_after_nudge_is_not_silent_done(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        cmd = f"timeout 20 python -m http.server {port}"
        script = [
            {"content": "start", "tool_calls": [{"function": {
                "name": "bash", "arguments": {"cmd": cmd, "background": True}}}]},
            {"content": "stop", "tool_calls": [{"function": {
                "name": "job_kill", "arguments": {"id": "j1"}}}]},
            {"content": "finish", "tool_calls": [{"function": {
                "name": "finish", "arguments": {
                    "status": "done", "summary": "served the site"}}}]},
            {"content": "I cannot keep it running."},
        ]
        p, rep, _events = self.run_cli(
            "Serve the site on one port, check it returns 200, then finish",
            script)
        self.assertEqual("partial", rep.get("status"), p.stdout[-2000:])
        self.assertIn("server not running at finish",
                      "\n".join(rep.get("open_issues") or []))
        self.assertTrue(str(rep.get("served_url") or "").startswith(
            f"http://127.0.0.1:{port}"), rep)

    def test_q2_live_server_needs_no_nudge(self):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()
        cmd = f"timeout 20 python -m http.server {port}"
        script = [
            {"content": "start", "tool_calls": [{"function": {
                "name": "bash", "arguments": {"cmd": cmd, "background": True}}}]},
            {"content": "finish", "tool_calls": [{"function": {
                "name": "finish", "arguments": {
                    "status": "done", "summary": "served the site"}}}]},
            {"content": '{"verdict":"pass","issues":[]}'},
        ]
        p, rep, events = self.run_cli(
            "Serve the site on one port and leave it running", script)
        self.assertEqual("done", rep.get("status"), p.stdout[-2000:])
        self.assertTrue(rep.get("served_alive"), rep)
        self.assertEqual("finish", rep.get("served_probe_at"), rep)
        self.assertFalse([e for e in events if e.get("type") == "server_nudge"])
        self.assertEqual([], rep.get("open_issues") or [])

    def test_q2_no_server_task_has_no_nudge(self):
        script = [
            {"content": "finish", "tool_calls": [{"function": {
                "name": "finish", "arguments": {
                    "status": "done", "summary": "repository summarised"}}}]},
            {"content": '{"verdict":"pass","issues":[]}'},
        ]
        p, rep, events = self.run_cli("Summarise the repository in one line.",
                                      script)
        self.assertEqual("done", rep.get("status"), p.stdout[-2000:])
        self.assertFalse(rep.get("served_requested"), rep)
        self.assertEqual("", rep.get("served_url"), rep)
        self.assertFalse([e for e in events if e.get("type") == "server_nudge"])

    # ---------------------------------------------------------------- A5
    def test_a5_parent_report_merges_child_files(self):
        script = [
            {"content": "delegating",
             "tool_calls": [{"function": {"name": "spawn_agent", "arguments": {
                 "task": "write kid.js with the number 42", "wait": True}}}]},
            {"content": "child writing",
             "tool_calls": [{"function": {"name": "write_file", "arguments": {
                 "path": "kid.js", "content": "console.log(42)\n"}}}]},
            {"content": "child done",
             "tool_calls": [{"function": {"name": "finish", "arguments": {
                 "status": "done", "summary": "kid.js written",
                 "files_changed": ["kid.js"]}}}]},
            {"content": "parent done",
             "tool_calls": [{"function": {"name": "finish", "arguments": {
                 "status": "done", "summary": "worker finished"}}}]},
        ]
        p, rep, _evs = self.run_cli(
            "Spawn a worker to write kid.js, then finish", script,
            verify="never")
        self.assertIn(rep["status"], ("done", "partial"), p.stdout[-2000:])
        self.assertIn("kid.js", rep.get("files_changed") or [], rep)
        self.assertTrue((self.work / "kid.js").is_file())


if __name__ == "__main__":
    unittest.main()
