"""Work order #1 — F4 (--debug-http) and F3 (hydra report) acceptance tests.

Run from the repo root:  python -m unittest tests.test_wo1_http_report -v
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from tests.fake_ollama import FakeOllama

# A fake fixture secret (never a real credential) used to prove redaction.
NEEDLE = "hydra-test-needle-abcdef0123456789"


def finish_node(summary="done"):
    return {"tool_calls": [{"function": {"name": "finish",
                                         "arguments": {"status": "done",
                                                       "summary": summary}}}]}


def _ev(ts, type_, agent=None, **fields):
    ev = {"ts": ts, "run": "fixture", "agent": agent, "seq": 0, "type": type_}
    ev.update(fields)
    return ev


class F4HttpDebugTests(unittest.TestCase):
    """--debug-http / HYDRA_DEBUG_HTTP=1 -> runs/<id>/http.jsonl."""

    def setUp(self):
        self.srv = FakeOllama().start()
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        self._old_key = os.environ.pop("OLLAMA_API_KEY", None)
        self._old_dbg = os.environ.pop("HYDRA_DEBUG_HTTP", None)
        os.environ["HYDRA_HOME"] = self.home.name
        os.environ["HYDRA_TEST_SECRET"] = NEEDLE

    def tearDown(self):
        self.srv.stop()
        self.home.cleanup()
        self.cwd.cleanup()
        os.environ.pop("HYDRA_TEST_SECRET", None)
        for name, val in (("HYDRA_HOME", self._old_home),
                          ("OLLAMA_API_KEY", self._old_key),
                          ("HYDRA_DEBUG_HTTP", self._old_dbg)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val

    def cfg(self, *extra):
        from hydra import config as C
        overrides = [
            f"api.host={self.srv.url}",
            "api.retries=2",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ] + list(extra)
        return C.load_config(overrides=overrides, project_dir=self.cwd.name)

    def _script_429_then_finish(self):
        self.srv.add("gpt-oss:120b",
                     {"status": 429,
                      "body": {"error": f"quota exhausted, credential={NEEDLE}"},
                      "retry_after": 0.05},
                     finish_node("ok"))

    def test_http_jsonl_fields_redaction_and_prefix_hash(self):
        from hydra import config as C
        from hydra.runtime import Runtime

        self._script_429_then_finish()
        rt = Runtime(self.cfg('hygiene.redact_env=["HYDRA_TEST_SECRET"]',
                              "api.debug_http=true", "loop.verify=never"),
                     cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("do the thing")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0, report)

        p = C.run_dir(rid) / "http.jsonl"
        self.assertTrue(p.is_file(), "http.jsonl was not written")
        raw = p.read_text(encoding="utf-8")
        self.assertNotIn("Authorization", raw)
        self.assertNotIn("Bearer", raw)
        self.assertNotIn(NEEDLE, raw)

        records = [json.loads(ln) for ln in raw.splitlines() if ln.strip()]
        self.assertGreaterEqual(len(records), 2)
        fields = {"ts", "method", "path", "model", "status", "latency_ms",
                  "ttfb_ms", "request_bytes", "response_bytes", "headers",
                  "think", "format", "prefix_hash"}
        for rec in records:
            self.assertTrue(fields <= set(rec), f"missing: {fields - set(rec)}")

        err = [r for r in records if r["status"] == 429][0]
        ok = [r for r in records if r["status"] == 200][0]
        self.assertEqual(ok["method"], "POST")
        self.assertEqual(ok["path"], "/api/chat")
        self.assertEqual(ok["model"], "gpt-oss:120b")
        self.assertGreater(ok["latency_ms"], 0)
        self.assertIsNotNone(ok["think"])              # the wire think value
        self.assertIsNone(ok["format"])                # tools call: no format
        self.assertIn("content-type", ok["headers"])
        self.assertIn("retry-after", err["headers"])
        self.assertIn("error_body", err)
        self.assertIn("***", err["error_body"])
        self.assertLessEqual(len(err["error_body"]), 500)
        self.assertNotIn("error_body", ok)             # 2xx keeps no body

        # request_bytes/response_bytes agree with the wire traffic
        body = self.srv.chat_requests()[0]["body"]
        self.assertEqual(ok["think"], body.get("think"))
        self.assertEqual(ok["request_bytes"], len(json.dumps(body).encode()))
        self.assertGreater(ok["response_bytes"], 0)

        # prefix hash = sha256(system prompt + tools JSON), recomputed here
        system = body["messages"][0]["content"]
        tools_json = json.dumps(body.get("tools") or [], sort_keys=True,
                                ensure_ascii=False)
        expect = hashlib.sha256((system + "\n" + tools_json).encode()).hexdigest()
        self.assertEqual(ok["prefix_hash"], expect)
        self.assertEqual(err["prefix_hash"], expect)

    def test_env_var_enables_logging(self):
        from hydra import httpdebug
        cfg = self.cfg()
        self.assertFalse(httpdebug.enabled(cfg))
        os.environ["HYDRA_DEBUG_HTTP"] = "1"
        try:
            self.assertTrue(httpdebug.enabled(cfg))
        finally:
            os.environ.pop("HYDRA_DEBUG_HTTP", None)
        self.assertTrue(httpdebug.enabled(self.cfg("api.debug_http=true")))

    def test_api_error_message_never_carries_the_secret(self):
        from hydra import hygiene
        from hydra.client import ApiError, OllamaCloud

        self.srv.add("gpt-oss:120b",
                     {"status": 429,
                      "body": {"error": f"credential {NEEDLE} rejected"},
                      "retry_after": 1})
        cfg = self.cfg('hygiene.redact_env=["HYDRA_TEST_SECRET"]')
        cli = OllamaCloud(self.srv.url, "", 5, 5)
        cli.redact = hygiene.make_redactor(cfg)
        with self.assertRaises(ApiError) as ctx:
            cli.chat("gpt-oss:120b", [{"role": "user", "content": "hi"}])
        self.assertNotIn(NEEDLE, str(ctx.exception))
        self.assertIn("***", str(ctx.exception))

    def test_pick_headers_and_prefix_hash_helpers(self):
        from hydra import httpdebug

        class _Resp:
            def __init__(self, items):
                self.headers = dict(items)

        resp = _Resp([("Content-Type", "application/json"),
                      ("Retry-After", "7"),
                      ("X-RateLimit-Remaining", "3"),
                      ("X-Quota-Reset", "soon"),
                      ("X-Unrelated", "nope")])
        picked = httpdebug.pick_headers(resp)
        self.assertEqual(picked["content-type"], "application/json")
        self.assertEqual(picked["retry-after"], "7")
        self.assertEqual(picked["x-ratelimit-remaining"], "3")
        self.assertEqual(picked["x-quota-reset"], "soon")
        self.assertNotIn("x-unrelated", picked)

        msgs = [{"role": "system", "content": "SYS"},
                {"role": "user", "content": "hi"}]
        tools = [{"type": "function", "function": {"name": "x"}}]
        manual = hashlib.sha256(
            ("SYS" + "\n" + json.dumps(tools, sort_keys=True,
                                       ensure_ascii=False)).encode()).hexdigest()
        self.assertEqual(httpdebug.prefix_hash(msgs, tools), manual)

    def test_cli_flag_parsed_and_end_to_end(self):
        from hydra.cli import _parse

        o, rest = _parse(["--debug-http", "say hi"])
        self.assertTrue(o.debug_http)
        self.assertEqual(rest, ["say hi"])
        o2, _ = _parse(["say hi"])
        self.assertFalse(o2.debug_http)

        self.srv.add("gpt-oss:120b", finish_node("hi"))
        env = dict(os.environ)
        env["HYDRA_HOME"] = self.home.name
        env["PYTHONPATH"] = str(REPO)
        env.pop("OLLAMA_API_KEY", None)
        proc = subprocess.run(
            [sys.executable, "-m", "hydra", "--plain", "--debug-http",
             "--set", f"api.host={self.srv.url}", "--set", "api.retries=2",
             "--set", "loop.verify=never", "--set", "termux.wake_lock=false",
             "--set", "termux.notify_on_finish=false",
             "--set", "termux.notify_on_park=false", "say hi"],
            cwd=self.cwd.name, env=env, capture_output=True, text=True,
            timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        homes = sorted((Path(self.home.name) / "runs").iterdir())
        p = homes[-1] / "http.jsonl"
        self.assertTrue(p.is_file(), proc.stdout + proc.stderr)
        recs = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines()
                if ln.strip()]
        self.assertTrue(recs)
        self.assertEqual(recs[-1]["status"], 200)


class F3ReportTests(unittest.TestCase):
    """hydra report on synthetic run-dir fixtures."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        self._old_key = os.environ.pop("OLLAMA_API_KEY", None)
        os.environ["HYDRA_HOME"] = self.home.name
        os.environ["HYDRA_TEST_SECRET"] = NEEDLE

    def tearDown(self):
        self.home.cleanup()
        self.cwd.cleanup()
        os.environ.pop("HYDRA_TEST_SECRET", None)
        for name, val in (("HYDRA_HOME", self._old_home),
                          ("OLLAMA_API_KEY", self._old_key)):
            if val is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = val

    def cfg(self, *extra):
        from hydra import config as C
        overrides = ['hygiene.redact_env=["HYDRA_TEST_SECRET"]'] + list(extra)
        return C.load_config(overrides=overrides, project_dir=self.cwd.name)

    def _make_run(self, rid, *, started, status="done", task="do the thing",
                  cwd=None, finished=None, events=(), usage=(), http=()):
        d = Path(self.home.name) / "runs" / rid
        d.mkdir(parents=True, exist_ok=True)
        meta = {"run": rid, "name": "", "task": task,
                "cwd": cwd or str(self.cwd.name), "status": status,
                "started": started,
                "finished": started + 30 if finished is None else finished,
                "pid": os.getpid(), "exit_code": 0}
        (d / "run.json").write_text(json.dumps(meta), encoding="utf-8")
        lines = [json.dumps(e) for e in events]
        (d / "events.jsonl").write_text(
            "\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        ulines = [json.dumps(u) for u in usage]
        (d / "usage.jsonl").write_text(
            "\n".join(ulines) + ("\n" if ulines else ""), encoding="utf-8")
        if http:
            (d / "http.jsonl").write_text(
                "\n".join(json.dumps(h) for h in http) + "\n", encoding="utf-8")
        return d

    def _rich_run(self, rid="r_0119_1000", started=None, task=None):
        started = time.time() - 120 if started is None else started
        task = task if task is not None else f"do the thing via {NEEDLE} now"
        now = started
        events = [
            _ev(now, "run_start", task=task, cwd=str(self.cwd.name)),
            _ev(now + 1, "agent_start", "a0", role="lead", depth=0, parent=None),
            _ev(now + 2, "model_request", "a0", role="lead",
                model="gpt-oss:120b", attempt=1),
            _ev(now + 2.5, "model_response", "a0", model="gpt-oss:120b",
                role="lead", in_tokens=1000, out_tokens=100, ms=5),
            _ev(now + 3, "tool_call", "a0", tool="bash", args={"cmd": "ls"}),
            _ev(now + 3.1, "tool_result", "a0", tool="bash", chars=10, ms=3,
                lines=1, preview="ok"),
            _ev(now + 4, "compact", "a0", retired=6, kept=7, digest_chars=100,
                est_before=4000, est_after=1500),
            _ev(now + 5, "agent_start", "a1", role="worker", depth=1, parent="a0"),
            _ev(now + 5.5, "model_request", "a1", role="worker",
                model="gpt-oss:20b", attempt=1),
            _ev(now + 6, "model_response", "a1", model="gpt-oss:20b",
                role="worker", in_tokens=500, out_tokens=50, ms=4),
            _ev(now + 7, "model_request", "a0", role="lead",
                model="nemotron-3-ultra", attempt=1),      # failover
            _ev(now + 7.1, "model_request", "a0", role="lead",
                model="nemotron-3-ultra", attempt=2),      # retry
            _ev(now + 8, "model_response", "a0", model="nemotron-3-ultra",
                role="lead", in_tokens=1200, out_tokens=120, ms=6),
            _ev(now + 9, "report", "a0",
                report={"status": "done", "summary": "all good"}),
        ]
        usage = [
            {"ts": now + 2.5, "model": "gpt-oss:120b", "in": 1000, "out": 100,
             "est_usd": 0.0005},
            {"ts": now + 6, "model": "gpt-oss:20b", "in": 500, "out": 50,
             "est_usd": 0.0001},
            {"ts": now + 8, "model": "nemotron-3-ultra", "in": 1200, "out": 120,
             "est_usd": 0.0009},
        ]
        http = [{"status": 200}, {"status": 200}, {"status": 429}]
        self._make_run(rid, started=started, task=task, events=events,
                       usage=usage, http=http)
        return rid

    def _run_cli(self, cfg, args):
        from hydra import report as R
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = R.cli(cfg, args)
        return rc, buf.getvalue()

    # ------------------------------------------------------------ markdown

    def test_markdown_report_fields_and_redaction(self):
        rid = self._rich_run()
        rc, out = self._run_cli(self.cfg(), [rid])
        self.assertEqual(rc, 0)
        self.assertIn(rid, out)
        self.assertIn("task:", out)
        self.assertIn("do the thing via", out)
        self.assertIn("## agents", out)
        self.assertIn("a0", out)
        self.assertIn("lead", out)
        self.assertIn("gpt-oss:120b", out)
        self.assertIn("nemotron-3-ultra", out)
        self.assertIn("a1", out)
        self.assertIn("worker", out)
        self.assertIn("turns", out)
        self.assertIn("tools: bash ×1", out)
        self.assertIn("retries: 1", out)
        self.assertIn("failovers: 1", out)
        self.assertIn("compactions: 1", out)
        self.assertIn("200×2", out)
        self.assertIn("429×1", out)
        self.assertIn("0.0015", out)
        self.assertIn("## files changed", out)
        self.assertIn("## last 8 events", out)
        self.assertLessEqual(len(out.splitlines()), 120)
        self.assertNotIn("Authorization", out)
        self.assertNotIn(NEEDLE, out)      # the redaction path ran
        self.assertIn("***", out)

    def test_json_report_parses(self):
        rid = self._rich_run()
        rc, out = self._run_cli(self.cfg(), ["--json", rid])
        self.assertEqual(rc, 0)
        data = json.loads(out)
        for key in ("run", "task", "status", "duration_sec", "agents", "tools",
                    "retries", "failovers", "http_status", "compactions",
                    "cost", "files_changed", "last_events"):
            self.assertIn(key, data)
        self.assertEqual(data["run"], rid)
        self.assertEqual(data["status"], "done")
        self.assertEqual(data["tools"], {"bash": 1})
        self.assertEqual(data["retries"], 1)
        self.assertEqual(data["failovers"], 1)
        self.assertEqual(data["compactions"], 1)
        self.assertEqual(data["http_status"], {"200": 2, "429": 1})
        self.assertAlmostEqual(data["cost"]["est_usd"], 0.0015, places=6)
        self.assertEqual(len(data["last_events"]), 8)
        by_id = {a["id"]: a for a in data["agents"]}
        self.assertEqual(by_id["a0"]["role"], "lead")
        self.assertEqual(by_id["a0"]["turns"], 2)
        self.assertEqual(by_id["a0"]["in"], 2200)
        self.assertEqual(by_id["a0"]["out"], 220)
        self.assertIn("nemotron-3-ultra", by_id["a0"]["models"])
        self.assertEqual(by_id["a1"]["depth"], 1)
        self.assertEqual(by_id["a1"]["turns"], 1)
        self.assertIsNone(data["files_changed"])   # temp cwd is not a repo
        self.assertNotIn(NEEDLE, out)
        self.assertIn("***", out)

    def test_timeline_and_full_cap(self):
        rid = "r_0119_0900"
        started = time.time() - 300
        events = [_ev(started + i, "model_response", "a0", model="m",
                      role="lead", in_tokens=10, out_tokens=1, ms=1)
                  for i in range(150)]
        self._make_run(rid, started=started, events=events,
                       usage=[{"model": "m", "in": 1500, "out": 150,
                               "est_usd": 0.001}])
        _, out = self._run_cli(self.cfg(), [rid, "--timeline"])
        self.assertIn("## timeline", out)
        self.assertLessEqual(len(out.splitlines()), 120)
        _, full = self._run_cli(self.cfg(), [rid, "--timeline", "--full"])
        self.assertGreater(len(full.splitlines()), 120)

    # ------------------------------------------------------------ since

    def test_since_aggregates_two_runs(self):
        now = time.time()
        self._rich_run("r_0119_0800", started=now - 600, task="older run")
        self._rich_run("r_0119_0801", started=now - 60, task="newer run")
        self._make_run("r_0119_0802", started=now - 6 * 3600, task="too old",
                       usage=[{"model": "m", "in": 1, "out": 1,
                               "est_usd": 9.0}])
        rc, out = self._run_cli(self.cfg(), ["--since", "2h"])
        self.assertEqual(rc, 0)
        self.assertIn("r_0119_0800", out)
        self.assertIn("r_0119_0801", out)
        self.assertNotIn("r_0119_0802", out)
        self.assertIn("2 runs", out)
        self.assertIn("totals:", out)
        self.assertNotIn(NEEDLE, out)

        rc, out = self._run_cli(self.cfg(), ["--since=2h", "--json"])
        self.assertEqual(rc, 0)
        agg = json.loads(out)
        self.assertEqual(agg["totals"]["runs"], 2)
        self.assertEqual(len(agg["runs"]), 2)
        self.assertAlmostEqual(agg["totals"]["est_usd"], 0.003, places=6)

    def test_bad_since_is_a_friendly_error(self):
        from hydra import report as R
        rc, out = self._run_cli(self.cfg(), ["--since", "fortnight"])
        self.assertEqual(rc, 2)
        self.assertIn("--since", out)
        with self.assertRaises(ValueError):
            R.parse_window("2 fortnights")

    def test_parse_window_units(self):
        from hydra import report as R
        self.assertEqual(R.parse_window("30m"), 1800)
        self.assertEqual(R.parse_window("2h"), 7200)
        self.assertEqual(R.parse_window("3d"), 259200)
        self.assertEqual(R.parse_window("45s"), 45)

    # ------------------------------------------------------------ cli + git

    def test_cli_dispatch_via_main(self):
        from hydra import cli
        rid = self._rich_run()
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = cli.main(["report", "--json", rid])
        self.assertEqual(rc, 0)
        data = json.loads(buf.getvalue())
        self.assertEqual(data["run"], rid)

    @unittest.skipUnless(shutil.which("git"), "git is not installed")
    def test_files_changed_from_git_diff_stat(self):
        repo = Path(self.cwd.name) / "repo"
        repo.mkdir()
        (repo / "a.py").write_text("x = 1\n", encoding="utf-8")
        env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                   GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "."], cwd=repo, check=True, env=env)
        subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True,
                       env=env)
        (repo / "a.py").write_text("x = 2\n", encoding="utf-8")
        now = time.time() - 30
        self._make_run("r_0119_0700", started=now, cwd=str(repo),
                       events=[_ev(now, "agent_start", "a0", role="lead",
                                   depth=0)])
        rc, out = self._run_cli(self.cfg(), ["--json", "r_0119_0700"])
        self.assertEqual(rc, 0)
        files = json.loads(out)["files_changed"]
        self.assertTrue(files, files)
        self.assertTrue(any("a.py" in f for f in files), files)

    def test_git_stat_skips_non_repos_cleanly(self):
        from hydra import report as R
        self.assertIsNone(R.git_stat(None))
        self.assertIsNone(R.git_stat(""))


if __name__ == "__main__":
    unittest.main()
