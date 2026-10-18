"""Phase 8 acceptance: project layer (§19.1–19.3, §19.8).

Acceptance (§17):
* HYDRA.md text appears in the env block exactly once and never in the
  system prompt; @path includes expand one level; project config wins
* a skill is listed by description only; its body loads after skill()
* a post_edit hook's output shows up in the tool result (auto-verify too)
* `hydra --continue` resumes the last run in this cwd
* SIGINT cancels the in-flight call but the run stays alive; SIGTERM
  checkpoints and exits
* `hydra schedule add` + the daemon fire jobs
"""
from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import time
import unittest
from pathlib import Path

from tests.test_phase1 import Base
from tests.test_phase2 import finish_node
from tests.test_phase6 import _base_args, _env, _wait_run


def _wait_events(home, wanted, timeout=30):
    """Wait until any run in HYDRA_HOME records one of the wanted event types."""
    runs = Path(home) / "runs"
    deadline = time.time() + timeout
    while time.time() < deadline:
        if runs.exists():
            for d in runs.iterdir():
                f = d / "events.jsonl"
                if not f.exists():
                    continue
                try:
                    lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
                except Exception:
                    continue
                for line in lines:
                    if not line.strip():
                        continue
                    try:
                        ev = json.loads(line)
                    except Exception:
                        continue
                    if ev.get("type") in wanted:
                        return ev
        time.sleep(0.2)
    return None


class ProjectTests(Base):
    def test_hydra_md_in_env_block_once_not_system(self):
        from hydra.runtime import Runtime

        marker = "PROJECT-RULE-MARKER-42"
        (Path(self.cwd.name) / "HYDRA.md").write_text(
            "# Project\n" + marker + "\n", encoding="utf-8")
        self.srv.add("gpt-oss:120b", finish_node("env checked"))
        rt = Runtime(self.cfg("loop.verify=never"), cwd=self.cwd.name, quiet=True)
        try:
            code, _ = rt.run("check the env")
        finally:
            rt.close()
        self.assertEqual(code, 0)
        reqs = self.srv.chat_requests()
        self.assertTrue(reqs)
        msgs = reqs[0]["body"]["messages"]
        self.assertEqual(msgs[0]["role"], "system")
        self.assertNotIn(marker, msgs[0]["content"])
        blob = "\n".join(str(m.get("content") or "") for m in msgs)
        self.assertEqual(blob.count(marker), 1)

    def test_at_include_expands_one_level(self):
        from hydra.project import project_text

        (Path(self.cwd.name) / "extra.md").write_text(
            "EXTRA-RULE-XYZ\n", encoding="utf-8")
        (Path(self.cwd.name) / "HYDRA.md").write_text(
            "# P\n@extra.md\n", encoding="utf-8")
        text = project_text(self.cwd.name, 8000)
        self.assertIn("EXTRA-RULE-XYZ", text)

    def test_project_config_overrides_global_and_flags(self):
        from hydra import config as C

        C.home().mkdir(parents=True, exist_ok=True)
        (C.home() / "config.toml").write_text("[loop]\nmax_turns = 5\n",
                                              encoding="utf-8")
        cfg = C.load_config(project_dir=self.cwd.name)
        self.assertEqual(C.get(cfg, "loop.max_turns"), 5)
        pdir = Path(self.cwd.name) / ".hydra"
        pdir.mkdir(parents=True, exist_ok=True)
        (pdir / "config.toml").write_text("[loop]\nmax_turns = 7\n",
                                          encoding="utf-8")
        cfg2 = C.load_config(project_dir=self.cwd.name)
        self.assertEqual(C.get(cfg2, "loop.max_turns"), 7)
        cfg3 = C.load_config(overrides=["loop.max_turns=9"],
                             project_dir=self.cwd.name)
        self.assertEqual(C.get(cfg3, "loop.max_turns"), 9)


class SkillTests(Base):
    def test_skill_listed_by_description_body_on_demand(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        skdir = Path(self.home.name) / "skills" / "deploy"
        skdir.mkdir(parents=True)
        (skdir / "SKILL.md").write_text(
            "---\nname: deploy\ndescription: MAGIC-DESCRIPTION-7\n---\n\n"
            "MAGIC-BODY-123 deploy steps here.\n", encoding="utf-8")
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {"name": "skill",
                                                   "arguments": {"name": "deploy"}}}]},
                     finish_node("skill loaded"))
        rt = Runtime(self.cfg("loop.verify=never"), cwd=self.cwd.name, quiet=True)
        try:
            code, _ = rt.run("use the deploy skill")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0)
        reqs = self.srv.chat_requests()
        blob = "\n".join(str(m.get("content") or "")
                         for m in reqs[0]["body"]["messages"])
        self.assertIn("MAGIC-DESCRIPTION-7", blob)
        self.assertNotIn("MAGIC-BODY-123", blob)
        results = [e for e in J.read_events(rid)
                   if e.get("type") == "tool_result" and e.get("tool") == "skill"]
        self.assertTrue(results)
        self.assertIn("MAGIC-BODY-123", results[0]["preview"])


class HookTests(Base):
    def test_post_edit_hook_and_autoverify_reach_tool_result(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {
                         "name": "write_file",
                         "arguments": {"path": "notes.txt",
                                       "content": "hello hooks\n"}}}]},
                     {"tool_calls": [{"function": {
                         "name": "write_file",
                         "arguments": {"path": "broken.py",
                                       "content": "def broken(:\n"}}}]},
                     finish_node("hooks checked"))
        rt = Runtime(self.cfg("loop.verify=never",
                              "hooks.post_edit=echo HOOK-RAN-123"),
                     cwd=self.cwd.name, quiet=True)
        try:
            code, _ = rt.run("write two files")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0)
        # the model-visible tool results carry the hook + autoverify notes
        reqs = self.srv.chat_requests()
        last = reqs[-1]["body"]["messages"]
        contents = [str(m.get("content") or "") for m in last
                    if m.get("role") == "tool"]
        notes = [c for c in contents if "notes.txt" in c]
        self.assertTrue(notes, contents)
        self.assertIn("[hook]", notes[0])
        self.assertIn("HOOK-RAN-123", notes[0])
        broken = [c for c in contents if "broken.py" in c]
        self.assertTrue(broken, contents)
        self.assertIn("autoverify", broken[0])
        self.assertIn("failed", broken[0])
        # and the hook run itself is journaled for observability
        hook_events = [e for e in J.read_events(rid)
                       if e.get("type") == "hook" and e.get("hook") == "post_edit"]
        self.assertTrue(hook_events)
        self.assertIn("HOOK-RAN-123", json.dumps(hook_events))


class ContinueTests(Base):
    def test_continue_resumes_last_run_in_cwd(self):
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {
                         "name": "bash",
                         "arguments": {"cmd": "echo step1 > step1.txt && sleep 45",
                                       "timeout": 60}}}]},
                     {"tool_calls": [{"function": {
                         "name": "bash",
                         "arguments": {"cmd": "echo step2 > step2.txt"}}}]},
                     finish_node("both steps done"))
        env = _env(self.home.name)
        proc = subprocess.Popen(_base_args(self.srv) + ["two-step task"],
                                cwd=self.cwd.name, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        step1 = Path(self.cwd.name) / "step1.txt"
        deadline = time.time() + 20
        while time.time() < deadline and not step1.exists():
            time.sleep(0.2)
        self.assertTrue(step1.exists(), "step1 never appeared")
        proc.kill()
        proc.wait(timeout=10)
        proc.stdout.close()
        cont = subprocess.run(_base_args(self.srv) + ["--continue"],
                              cwd=self.cwd.name, env=env,
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(cont.returncode, 0, cont.stdout + cont.stderr)
        self.assertTrue((Path(self.cwd.name) / "step2.txt").exists(),
                        cont.stdout + cont.stderr)
        _rid_dir, meta = _wait_run(self.home.name, timeout=20)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("status"), "done")


class SigintTests(Base):
    def test_sigint_cancels_inflight_call_run_survives(self):
        self.srv.add("gpt-oss:120b",
                     {"content": "WORK" * 20, "chunks": 20, "delay_chunks": 0.6},
                     finish_node("recovered after ctrl-c"))
        env = _env(self.home.name)
        proc = subprocess.Popen(_base_args(self.srv) + ["long stream task"],
                                cwd=self.cwd.name, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        # wait until the long stream actually reached the fake server
        deadline = time.time() + 30
        while time.time() < deadline and not self.srv.seen_models:
            time.sleep(0.2)
        self.assertTrue(self.srv.seen_models, "the run never reached its model call")
        time.sleep(0.3)
        proc.send_signal(signal.SIGINT)
        try:
            out, _ = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 0, out.decode("utf-8", "replace"))
        rid_dir, meta = _wait_run(self.home.name, timeout=20)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("status"), "done")
        from hydra import journal as J
        events = J.read_events(rid_dir.name)
        types = [e.get("type") for e in events]
        self.assertIn("sigint", types)
        # proof the long stream was cancelled: only the finish reply completed
        responses = [e for e in events if e.get("type") == "model_response"]
        self.assertEqual(len(responses), 1,
                         "the cancelled stream still completed: " +
                         json.dumps([e.get("tool_calls") for e in responses]))
        self.assertIn("finish", json.dumps(responses[0].get("tool_calls") or []))

    def test_sigterm_checkpoints_and_exits(self):
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {
                         "name": "bash",
                         "arguments": {"cmd": "sleep 45", "timeout": 60}}}]},
                     finish_node("never reached"))
        env = _env(self.home.name)
        proc = subprocess.Popen(_base_args(self.srv) + ["long sleep task"],
                                cwd=self.cwd.name, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        ev = _wait_events(self.home.name, ("tool_call",), timeout=30)
        self.assertIsNotNone(ev, "the run never started its bash call")
        t0 = time.time()
        proc.send_signal(signal.SIGTERM)
        try:
            out, _ = proc.communicate(timeout=120)
        finally:
            if proc.poll() is None:
                proc.kill()
        self.assertEqual(proc.returncode, 130, out.decode("utf-8", "replace"))
        self.assertLess(time.time() - t0, 40,
                        "process did not exit promptly (did the bash survive?)")
        _rid_dir, meta = _wait_run(self.home.name, timeout=10)
        self.assertIsNotNone(meta)
        self.assertEqual(meta.get("status"), "interrupted")


class ScheduleTests(Base):
    def test_schedule_add_ls_fire_and_rm(self):
        env = _env(self.home.name)
        add = subprocess.run(_base_args(self.srv)
                             + ["schedule", "add", "every 1s", "scheduled task"],
                             cwd=self.cwd.name, env=env,
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(add.returncode, 0, add.stdout + add.stderr)
        m = re.search(r"schedule (\S+):", add.stdout)
        self.assertIsNotNone(m, add.stdout)
        jid = m.group(1)
        ls = subprocess.run(_base_args(self.srv) + ["schedule", "ls"],
                            cwd=self.cwd.name, env=env,
                            capture_output=True, text=True, timeout=60)
        self.assertIn("every 1s", ls.stdout)
        self.srv.add("gpt-oss:120b", finish_node("sched done"))
        dae = subprocess.run(_base_args(self.srv) + ["daemon", "--once"],
                             cwd=self.cwd.name, env=env,
                             capture_output=True, text=True, timeout=180)
        self.assertEqual(dae.returncode, 0, dae.stdout + dae.stderr)
        runs = Path(self.home.name) / "runs"
        found = []
        for d in sorted(runs.iterdir(), key=lambda p: p.stat().st_mtime):
            try:
                data = json.loads((d / "run.json").read_text())
            except Exception:
                continue
            if str(data.get("name") or "").startswith("sched-"):
                found.append(data)
        self.assertTrue(found, dae.stdout)
        self.assertEqual(found[-1].get("status"), "done")
        rm = subprocess.run(_base_args(self.srv) + ["schedule", "rm", jid],
                            cwd=self.cwd.name, env=env,
                            capture_output=True, text=True, timeout=60)
        self.assertEqual(rm.returncode, 0, rm.stdout + rm.stderr)
        self.assertIn("removed", rm.stdout)


if __name__ == "__main__":
    unittest.main()
