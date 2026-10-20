"""WO3 A1–A5: honest artifacts, claim-aware verification, one port, merge."""
from __future__ import annotations

import copy
import contextlib
import io
import json
import os
import socket
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hydra import artifacts as A
from hydra import config as C
from hydra import models as M
from hydra import verifier as V
from hydra.agent import Agent
from hydra.client import ModelMissing, PaidModel, Reply
from hydra.router import AllModelsFailed
from hydra.models import ModelRegistry
from hydra.router import Router
from hydra.swarm import Swarm
from hydra.tools import shell as S


def cfg(**over):
    c = copy.deepcopy(C.DEFAULTS)
    for k, v in over.items():
        c.setdefault(k, {}).update(v)
    return c


def agent_for(d, *, cfg_=None):
    a = Agent("a0", "lead", "task", cfg_ or cfg(), workdir=d)
    a.started_at = time.time()
    return a


def marker(d, since):
    p = Path(d) / "marker"
    p.write_text("")
    os.utime(p, (since - 1, since - 1))
    return p


class FilesChangedTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.proj = self.tmp / "project"
        self.proj.mkdir()

    def _arm(self, a, since):
        a.data["files_marker"] = str(marker(self.tmp, since))

    def test_scan_finds_shell_writes_at_scale(self):
        # 2k unrelated files next to the project: the scan must stay on roots.
        junk = self.tmp / "junk"
        junk.mkdir()
        for i in range(2000):
            (junk / f"x{i}.txt").write_text("j")
        since = time.time()
        a = agent_for(self.proj)
        self._arm(a, since)
        (self.proj / "css").mkdir()
        (self.proj / "css" / "app.css").write_text("body{}")
        (self.proj / "app.js").write_text("1")
        rep = A.collect(a, [])
        self.assertTrue(rep["complete"])
        self.assertIn("app.js", rep["files"])
        self.assertIn("css/app.css", rep["files"])
        self.assertLess(rep["scan"]["ms"], 1000)

    def test_sibling_project_from_home_is_found_without_home_scan(self):
        fake_home = self.tmp / "home"
        (fake_home / "junk").mkdir(parents=True)
        for i in range(300):
            (fake_home / "junk" / f"j{i}.txt").write_text("j")
        proj = fake_home / "proj"
        proj.mkdir()
        since = time.time()
        a = agent_for(fake_home)
        a.data["files_marker"] = str(marker(fake_home, since))
        a.messages = [{"role": "assistant", "tool_calls": [{"function": {
            "name": "bash", "arguments": {
                "cmd": "cd ~/proj && printf 'x' > js.js"}}}]}]
        (proj / "js.js").write_text("x")
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}), \
                mock.patch("pathlib.Path.home", return_value=fake_home):
            rep = A.collect(a, [])
        self.assertTrue(rep["complete"], rep)
        self.assertEqual(rep["files"], ["proj/js.js"])
        self.assertNotIn(str(fake_home), rep["scan"]["roots"])

    def test_tee_path_finds_project_from_home(self):
        fake_home = self.tmp / "home2"
        proj = fake_home / "proj"
        proj.mkdir(parents=True)
        since = time.time()
        a = agent_for(fake_home)
        a.data["files_marker"] = str(marker(fake_home, since))
        a.messages = [{"role": "assistant", "tool_calls": [{"function": {
            "name": "bash", "arguments": {
                "cmd": "printf x | tee proj/app.js >/dev/null"}}}]}]
        (proj / "app.js").write_text("x")
        with mock.patch.dict(os.environ, {"HOME": str(fake_home)}), \
                mock.patch("pathlib.Path.home", return_value=fake_home):
            rep = A.collect(a, [])
        self.assertTrue(rep["complete"], rep)
        self.assertEqual(rep["files"], ["proj/app.js"], rep)
        self.assertNotIn(str(fake_home), rep["scan"]["roots"])

    def test_truncated_scan_is_null_not_empty(self):
        since = time.time()
        a = agent_for(self.proj, cfg_=cfg(report={"scan_max_entries": 1}))
        self._arm(a, since)
        for i in range(3):
            (self.proj / f"f{i}.py").write_text("x")
        rep = A.collect(a, [])
        self.assertIsNone(rep["files"])
        self.assertFalse(rep["complete"])
        self.assertEqual(rep["method"], "scan")

    def test_explicit_tool_files_are_kept(self):
        since = time.time()
        a = agent_for(self.proj)
        self._arm(a, since)
        (self.proj / "made.py").write_text("x")
        rep = A.collect(a, ["made.py"])
        self.assertIn("made.py", rep["files"])
        self.assertEqual(rep["method"], "mixed")

    def test_finish_keeps_null_and_ui_reads_it(self):
        since = time.time()
        a = agent_for(self.proj, cfg_=cfg(report={"scan_max_entries": 1}))
        self._arm(a, since)
        for i in range(3):
            (self.proj / f"f{i}.py").write_text("x")
        a.set_report("done", "did it", files_changed=["f0.py"])
        self.assertIsNone(a.report["files_changed"])
        self.child = None

    def test_empty_run_is_empty_list(self):
        a = agent_for(self.proj)
        self._arm(a, time.time())
        rep = A.collect(a, [])
        self.assertEqual(rep["files"], [])
        self.assertTrue(rep["complete"])


class ClaimTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        (self.d / "js").mkdir()
        self.rep = {"status": "done", "summary": "", "files_changed": ["js/script.js"],
                    "scan_complete": True, "findings": [], "artifacts": []}

    def audit(self, summary, files=None, messages=None):
        self.rep["summary"] = summary
        if files is not None:
            self.rep["files_changed"] = files
        a = Agent("a0", "lead", "task", cfg(), workdir=self.d)
        a.report = dict(self.rep)
        a.messages = messages or []
        return V.audit_claims(a)

    def test_removed_still_present_is_rejected(self):
        (self.d / "js/script.js").write_text("const FIR = 1;\n")
        out = self.audit("Removed FIR from the script.")
        self.assertTrue(out["issues"])
        self.assertIn("js/script.js:1", out["issues"][0]["what"])

    def test_removed_absent_passes(self):
        (self.d / "js/script.js").write_text("const x = 1;\n")
        out = self.audit("Removed the word FIR from the script.")
        self.assertFalse(out["issues"], out)

    def test_added_missing_is_rejected(self):
        (self.d / "js/script.js").write_text("const x = 1;\n")
        out = self.audit("Added 'FIRE' to the script.")
        self.assertTrue(out["issues"])
        self.assertIn("not found", out["issues"][0]["what"])

    def test_added_present_passes(self):
        (self.d / "js/script.js").write_text("const FIRE = 1;\n")
        out = self.audit("Added 'FIRE' to the script.")
        self.assertFalse(out["issues"], out)

    def test_claim_without_changed_file_is_rejected(self):
        out = self.audit("Removed FIR from the script.", files=[])
        self.assertTrue(out["issues"])
        self.assertIn("no changed text file", out["issues"][0]["what"])

    def test_untracked_files_reject_file_claims(self):
        (self.d / "js/script.js").write_text("const x = 1;\n")
        self.rep["files_changed"] = None
        out = self.audit("Removed FIR from the script.")
        self.assertTrue(out["issues"])
        self.assertIn("not tracked", out["issues"][0]["what"])

    def test_created_missing_is_rejected(self):
        out = self.audit("Created js/missing.js.")
        self.assertTrue(out["issues"])
        self.assertIn("missing or empty", out["issues"][0]["what"])

    def test_unrelated_extra_file_does_not_reject(self):
        (self.d / "js/script.js").write_text("const x = 1;\n")
        (self.d / "js/other.js").write_text("FIR\n")
        out = self.audit("Removed FIR from js/script.js.", files=["js/script.js", "js/other.js"])
        self.assertFalse(out["issues"], out)


class EvidenceTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.rep = {"status": "done", "summary": "", "files_changed": [],
                    "scan_complete": True, "findings": [], "artifacts": []}

    def audit(self, summary, rows):
        self.rep["summary"] = summary
        a = Agent("a0", "lead", "task", cfg(), workdir=self.d)
        a.report = dict(self.rep)
        a.messages = rows
        return V.audit_claims(a)

    def row(self, text, ev, changed=False, name="bash", turn=1):
        return {"role": "tool", "tool_name": name, "content": text,
                "ev": ev, "turn": turn, "changed": changed}

    def test_latest_success_after_change_passes_and_records_event(self):
        rows = [self.row("wrote js/app.js", 10, changed=True, name="write_file"),
                self.row("HTTP/1.1 200 OK", 11, turn=2)]
        out = self.audit("HTTP 200 after the edit.", rows)
        self.assertFalse(out["issues"], out)
        self.assertEqual(out["evidence"], [11])

    def test_success_then_failure_is_rejected(self):
        rows = [self.row("HTTP/1.1 200 OK", 10),
                self.row("curl: (52) Empty reply from server", 11, turn=2)]
        out = self.audit("HTTP 200.", rows)
        self.assertTrue(out["issues"])
        self.assertIn("latest evidence is a failure", out["issues"][0]["what"])

    def test_late_change_invalidates_old_success(self):
        rows = [self.row("HTTP/1.1 200 OK", 10),
                self.row("wrote js/app.js", 11, changed=True, name="write_file")]
        out = self.audit("HTTP 200.", rows)
        self.assertTrue(out["issues"])
        self.assertIn("no evidence after the last change", out["issues"][0]["what"])

    def test_no_evidence_is_rejected(self):
        out = self.audit("All tests passed.", [])
        self.assertTrue(out["issues"])
        self.assertIn("no evidence", out["issues"][0]["what"])

    def test_tests_pass_after_change(self):
        rows = [self.row("wrote test.py", 20, changed=True, name="write_file"),
                self.row("12 passed in 1.2s", 21, turn=2)]
        out = self.audit("All tests passed.", rows)
        self.assertFalse(out["issues"], out)
        self.assertEqual(out["evidence"], [21])

    def test_model_text_is_not_evidence(self):
        out = self.audit("I ran it and got HTTP 200 and all tests passed.", [])
        self.assertTrue(out["issues"])

    def test_unparseable_output_counts_as_no_evidence(self):
        out = self.audit("HTTP 200.", [self.row("done", 7)])
        self.assertTrue(out["issues"])


class PortTests(unittest.TestCase):
    def setUp(self):
        self.cwd = Path(tempfile.mkdtemp())
        self.rt = _RT()
        self.ctx = _Ctx(self.rt, self.cwd)

    def tearDown(self):
        for job in self.rt.jobs.values():
            try:
                job["proc"].kill()
                job["proc"].wait(timeout=2)
            except Exception:
                pass

    def test_port_parser(self):
        self.assertEqual(S._server_port("python -m http.server 8080")[:1], (8080,))
        self.assertEqual(S._server_port("npm run dev -- --port 5173")[:1], (5173,))
        self.assertIsNone(S._server_port("ls -la"))
        self.assertIsNone(S._server_port("curl localhost:8080"))

    def test_duplicate_server_nudges_once_and_reuses(self):
        first = S._background(self.ctx, self.cwd, "python -m http.server 18091")
        self.assertIn("started job", first)
        time.sleep(0.4)
        second = S._background(self.ctx, self.cwd, "python -m http.server 18091")
        third = S._background(self.ctx, self.cwd, "python -m http.server 18091")
        self.assertIn("NUDGE", second)
        self.assertNotIn("NUDGE", third)
        self.assertEqual(len(self.rt.jobs), 1)

    def test_foreign_port_gets_a_free_port_and_a_stated_url(self):
        srv = socket.socket()
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 18092))
        srv.listen(1)
        try:
            out = S._background(self.ctx, self.cwd, "python -m http.server 18092")
        finally:
            srv.close()
        self.assertIn("NOTE: port 18092 was already in use", out)
        self.assertIn("started on port", out)
        job = list(self.rt.jobs.values())[0]
        self.assertNotEqual(job["port"], 18092)
        self.assertTrue(job["url"].startswith("http://127.0.0.1:"))
        self.assertIn(str(job["port"]), out)

    def test_job_status_shows_port(self):
        S._background(self.ctx, self.cwd, "python -m http.server 18093")
        time.sleep(0.3)
        out = S._tool_job_status({}, self.ctx)
        self.assertIn("port=18093", out)
        self.assertIn("url=http://127.0.0.1:18093/", out)


class MergeTests(unittest.TestCase):
    def test_child_files_union_and_null_is_sticky(self):
        sw = Swarm(_RT())
        parent = Agent("a0", "lead", "task", cfg(), workdir=tempfile.mkdtemp())
        sw._reports["a1"] = {"files_changed": ["a.py", "b.py"]}
        sw._reports["a2"] = {"files_changed": ["b.py", "c.py"]}
        sw.merge_child(parent, "a1")
        sw.merge_child(parent, "a2")
        self.assertEqual(parent.data["child_files"], ["a.py", "b.py", "c.py"])
        self.assertFalse(parent.data.get("child_files_null"))
        sw._reports["a3"] = {"files_changed": None}
        sw.merge_child(parent, "a3")
        self.assertTrue(parent.data["child_files_null"])

    def test_parent_finish_includes_child_files(self):
        parent = Agent("a0", "lead", "task", cfg(), workdir=tempfile.mkdtemp())
        parent.started_at = time.time()
        parent.data["files_marker"] = str(marker(parent.workdir, parent.started_at))
        parent.data["child_files"] = ["kid.js"]
        parent.set_report("done", "merged")
        self.assertEqual(parent.report["files_changed"], ["kid.js"])


class _Gate:
    def slot(self, priority=0):
        return contextlib.nullcontext()


class _ProbeClient:
    key = "test-key"

    def __init__(self, mode="native"):
        self.mode = mode
        self.tool_calls = 0

    def chat(self, model, messages, **kw):
        text = str((messages or [{}])[-1].get("content") or "")
        if self.mode == "always":
            return Reply(model=model, content="", tool_calls=[{
                "function": {"name": "add", "arguments": {"a": 2, "b": 3}}}])
        if text == M.TOOL_MSG or "add tool" in text:
            self.tool_calls += 1
            if self.mode == "error":
                raise RuntimeError("tool probe down")
            if self.mode == "native":
                return Reply(model=model, content="", tool_calls=[{
                    "function": {"name": "add", "arguments": {"a": 2, "b": 3}}}])
            if self.mode == "text":
                return Reply(model=model, content=(
                    '<tool_call>{"name": "add", "arguments": '
                    '{"a": 2, "b": 3}}</tool_call>'))
            return Reply(model=model, content="I cannot use tools.")
        if "JSON" in text:
            return Reply(model=model, content='{"ok": true}')
        return Reply(model=model, content="OK")


class CapabilityProbeTests(unittest.TestCase):
    def test_native_text_nothing_and_failed_probe_are_classified(self):
        native = M.probe_one(_ProbeClient("native"), _Gate(), "m")
        text = M.probe_one(_ProbeClient("text"), _Gate(), "m")
        nothing = M.probe_one(_ProbeClient("nothing"), _Gate(), "m")
        failed = M.probe_one(_ProbeClient("error"), _Gate(), "m")
        self.assertIs(native["tools"], True)
        self.assertIs(text["tools"], True)
        self.assertIs(nothing["tools"], False)
        self.assertIsNone(failed["tools"])   # unknown, not a false claim
        self.assertEqual(native["status"], "ok")

    def test_observed_native_call_learns_tools_yes(self):
        cfg_ = cfg()
        cfg_["roles"]["lead"]["chain"] = ["fake"]
        reg = ModelRegistry(cfg_, path=Path(tempfile.mkdtemp()) / "models.json")
        client = _ProbeClient("always")
        router = Router(cfg_, client, registry=reg, gate=_Gate())
        reply = router.chat([{"role": "user", "content": "hi"}], role="lead")
        self.assertTrue(reply.tool_calls)
        e = reg.entry("fake")
        self.assertIs(e.get("tools"), True)
        self.assertEqual(e.get("tools_source"), "observed")

    def test_roles_view_shows_effective_chain_and_drop_reasons(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"HYDRA_HOME": str(tmp)}):
            cfg_ = cfg()
            cfg_["roles"]["lead"]["chain"] = ["paid-one", "no-tools", "good"]
            reg = ModelRegistry(cfg_)
            reg.mark("paid-one", "paid")
            reg.mark("no-tools", "ok", tools=False)
            reg.mark("good", "ok", tools=True)
            out = io.StringIO()
            with redirect_stdout(out):
                M._cmd_ls(cfg_, ["--roles"])
        text = out.getvalue()
        self.assertIn("effective: good", text)
        self.assertIn("dropped: paid", text)
        self.assertIn("dropped: tools=no", text)
        self.assertIn("default lead: good", text)


class ReprobeTests(unittest.TestCase):
    def test_weekly_reprobe_runs_once_then_is_cached(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"HYDRA_HOME": str(tmp)}):
            cfg_ = cfg()
            cfg_["roles"]["lead"]["chain"] = ["stale-one"]
            reg = ModelRegistry(cfg_)
            reg.mark("stale-one", "ok", tools=True)
            reg.data["models"]["stale-one"]["checked_at"] = \
                time.time() - 10 * 86400
            reg.save()
            events = []

            class J:
                def event(self, *a, **k):
                    events.append((a, k))

            with mock.patch("hydra.models.probe_one", return_value={
                    "status": "ok", "tools": True, "json": True,
                    "think_style": "levels", "ms": 1}) as probe:
                done = M.maybe_reprobe(cfg_, _ProbeClient(), _Gate(), reg,
                                       journal=J(), needed=["stale-one"])
                again = M.maybe_reprobe(cfg_, _ProbeClient(), _Gate(), reg,
                                        needed=["stale-one"])
        self.assertEqual(done, ["stale-one"])
        self.assertEqual(again, [])
        self.assertEqual(probe.call_count, 1)
        self.assertTrue(any(a and a[0] == "reprobe" for a, _ in events))

    def test_paid_model_is_reprobed_after_cooldown_once(self):
        cfg_ = cfg()
        cfg_["roles"]["lead"]["chain"] = ["paid-one"]
        reg = ModelRegistry(cfg_, path=Path(tempfile.mkdtemp()) / "models.json")
        reg.mark("paid-one", "paid")
        reg.data["models"]["paid-one"]["recheck_at"] = time.time() - 1
        reg.save()
        router = Router(cfg_, _ProbeClient(), registry=reg, gate=_Gate())
        with mock.patch("hydra.models.probe_one", return_value={
                "status": "ok", "tools": True, "json": True,
                "think_style": "levels", "ms": 1}) as probe:
            router._reprobe_due(["paid-one"])
            router._reprobe_due(["paid-one"])
        self.assertEqual(probe.call_count, 1)
        self.assertEqual(reg.status_of("paid-one"), "ok")


class CapabilityErrorReprobeTests(unittest.TestCase):
    def _paid_404(self, exc):
        cfg_ = cfg()
        cfg_["roles"]["lead"]["chain"] = ["m"]
        reg = ModelRegistry(cfg_, path=Path(tempfile.mkdtemp()) / "models.json")

        class C:
            def chat(self, *a, **k):
                raise exc

        router = Router(cfg_, C(), registry=reg, gate=_Gate())
        with self.assertRaises(AllModelsFailed):
            router.chat([{"role": "user", "content": "hi"}], role="lead")
        e = reg.entry("m")
        self.assertEqual(e.get("status"), "paid" if exc.status == 402 else "missing")
        self.assertLessEqual(float(e.get("recheck_at") or 0) - time.time(), 61)
        return cfg_, reg, router

    def test_402_and_404_mark_then_reprobe_once(self):
        for exc in (PaidModel(402, "paid", "m"),
                    ModelMissing(404, "missing", "m")):
            cfg_, reg, router = self._paid_404(exc)
            reg.data["models"]["m"]["recheck_at"] = time.time() - 1
            reg.save()
            with mock.patch("hydra.models.probe_one", return_value={
                    "status": "ok", "tools": True, "json": True,
                    "think_style": "levels", "ms": 1}) as probe:
                router._reprobe_due(["m"])
                router._reprobe_due(["m"])
            self.assertEqual(probe.call_count, 1)
            self.assertEqual(reg.status_of("m"), "ok")


class VerifierBudgetTests(unittest.TestCase):
    def test_three_rounds_are_tool_less_stable_and_costed(self):
        calls = []

        class Reply:
            model = "v"
            prompt_eval_count = 1000
            eval_count = 100

        class Res:
            reply = Reply()
            obj = {"verdict": "pass", "issues": []}
            error = ""

        def fake_chat(router, messages, **kw):
            calls.append((messages, kw))
            return Res()

        class Router:
            prices = {"v": {"in": 1.0, "out": 2.0}}

            def chain_for(self, *a, **k):
                return ["v"]

        class Journal:
            def __init__(self):
                self.events = []

            def event(self, *a, **k):
                self.events.append((a, k))

        rt = type("RT", (), {})()
        rt.cfg = cfg(verification={"max_tool_calls": 5, "budget_s": 60,
                                   "max_usd": 0.01})
        rt.router = Router()
        rt.journal = Journal()
        a = Agent("a0", "lead", "task", rt.cfg, workdir=tempfile.mkdtemp())
        a.report = {"status": "done", "summary": "", "files_changed": [],
                    "scan_complete": True, "findings": [], "artifacts": []}
        before = len(a.messages)
        with mock.patch("hydra.jsonroute.structured_chat", side_effect=fake_chat):
            results = [V.check(rt, a) for _ in range(3)]
        self.assertEqual([r["verdict"] for r in results], ["pass"] * 3)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(k.get("role") == "reviewer" for _, k in calls))
        self.assertTrue(all(k.get("usage_role") == "verifier"
                            for _, k in calls))
        self.assertTrue(all("tools" not in k for _, k in calls))
        self.assertEqual(len(a.messages), before)
        self.assertEqual(a.report["verify_tool_calls"], 0)
        self.assertEqual(a.report["verify_rounds"], 3)
        self.assertAlmostEqual(a.report["verify_cost_usd"], 0.0036, places=6)
        self.assertTrue(a.report["verify_budget_ok"])
        self.assertFalse([e for e in rt.journal.events
                          if e[0] and e[0][0] == "tool_result"])
        v_events = [e for e in rt.journal.events if e[0] and e[0][0] == "verify"]
        self.assertEqual(len(v_events), 3)
        self.assertTrue(all(e[1].get("calls") == 0 for e in v_events))


class PromptFootprintTests(unittest.TestCase):
    def test_realistic_pinned_prompt_is_under_5000_tokens(self):
        import json
        from hydra.runtime import Runtime
        from hydra import config as C

        tmp = Path(tempfile.mkdtemp())
        proj = tmp / "proj"
        proj.mkdir()
        (proj / ".git").mkdir()
        files = {
            "index.html": "<!doctype html><title>demo</title>" + "<p>x</p>" * 30,
            "app.js": "function main(){}\n" + "// filler\n" * 50,
            "style.css": "body{margin:0}\n" * 40,
            "README.md": "# demo\n" + "- item\n" * 40,
            "package.json": json.dumps({"name": "demo"}),
            "src/app.py": "def main():\n    pass\n" + "# filler\n" * 30,
            "src/util.py": "def util():\n    return 1\n",
            "tests/test_app.py": "def test_one():\n    assert True\n",
            "docs/notes.md": "# notes\n" + "text\n" * 40,
            "data/config.json": "{\"a\": 1}",
            "scripts/build.sh": "echo build\n",
            "assets/logo.svg": "<svg></svg>",
        }
        for rel, txt in files.items():
            p = proj / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(txt)
        (proj / "HYDRA.md").write_text("# Instructions\n\nUse stdlib only.\n")
        home = tmp / "home"
        (home / "runs").mkdir(parents=True)
        (home / "skills").mkdir()
        for i in range(8):
            d = home / "skills" / f"skill{i}"
            d.mkdir()
            (d / "SKILL.md").write_text(
                f"---\ndescription: Skill {i} handles task type {i}.\n---\n")
        for i in range(6):
            d = home / "runs" / f"r_old{i:02d}"
            d.mkdir()
            (d / "run.json").write_text(json.dumps({
                "cwd": str(proj), "task": f"old task {i}", "status": "done",
                "started": time.time() - i * 3600}))
        with mock.patch.dict(os.environ, {"HYDRA_HOME": str(home)}):
            cfg = C.load_config(overrides=[
                "api.host=http://127.0.0.1:1", "termux.wake_lock=false",
                "termux.notify_on_finish=false", "termux.notify_on_park=false",
            ], project_dir=proj)
            rt = Runtime(cfg, cwd=str(proj), quiet=True)
            self.addCleanup(rt.close)
            lead = rt.start_run("build a small feature in this demo project")
            tokens = lead.pinned_tokens(rt)
            tools = rt.tools.schemas_for(lead)
        self.assertLess(tokens, 5000, f"pinned prompt is {tokens} tokens")
        self.assertGreater(tokens, 1000)
        self.assertTrue(all(len(t["function"]["description"]) <= 57
                            for t in tools))
        names = {t["function"]["name"] for t in tools}
        self.assertIn("termux", names)
        self.assertNotIn("termux_battery", names)
        self.assertIn("termux_battery", rt.tools.tools)

    def test_termux_umbrella_dispatches_every_action(self):
        from hydra.tools import termux as T
        seen = []
        ctx = type("Ctx", (), {"cfg": {}})()
        orig = T._run
        T._run = lambda cmd, timeout=10: seen.append(cmd) or "ok"
        try:
            for action in ("battery", "toast", "clipboard_get",
                           "clipboard_set", "tts", "vibrate", "open_url",
                           "share", "sms_send", "call", "camera_photo", "wifi"):
                out = T._tool_termux({"action": action, "text": "x",
                                      "path": "/tmp/x", "url": "https://x",
                                      "number": "1"}, ctx)
                self.assertNotIn("unknown termux action", out)
            self.assertGreaterEqual(len(seen), 12)
            self.assertIn("unknown termux action",
                          T._tool_termux({"action": "nope"}, ctx))
        finally:
            T._run = orig


class _Agent:
    id = "a0"


class _RT:
    def __init__(self):
        self.jobs = {}
        self.journal = None
        self.server_nudged = set()
        self._n = 0

    def next_job_id(self):
        self._n += 1
        return f"j{self._n}"


class _Ctx:
    def __init__(self, rt, cwd):
        self.rt = rt
        self.agent = _Agent()
        self.cwd = cwd


if __name__ == "__main__":
    unittest.main()
