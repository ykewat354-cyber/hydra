"""Phase 4 acceptance: sub-agents (§9).

Acceptance (§17):
* lead spawns 3 researchers; tool executions overlap in wall-clock while model
  calls stay serialized (max_inflight == 1 asserted)
* reports <= report_cap chars
* resume mid-swarm re-attaches children (and never re-runs finished calls)
* blackboard note/recall (+ durable memory)
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

from tests.test_phase1 import Base, REPO
from tests.test_phase2 import finish_node


def lead_msgs(rid, home):
    from hydra import config as C
    p = C.run_dir(rid) / "agents" / "a0" / "transcript.jsonl"
    out = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return out


class SwarmTests(Base):
    def test_three_researchers_parallel_tools_serial_models(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        tasks = [{"task": f"run the timestamp overlap probe {i}",
                  "role": "researcher", "tools": "all"} for i in (1, 2, 3)]
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {
                         "name": "spawn_swarm",
                         "arguments": {"tasks": tasks, "join": "all"}}}]},
                     finish_node("swarm complete"))
        # one callable serves every child request: bash probe first, then finish.
        # (A flat queue would let a child's 2nd call eat another child's bash node.)
        probe_state = {"n": 0}

        def child_node(body):
            msgs = body.get("messages") or []
            if any(m.get("role") == "tool" for m in msgs):
                return {"tool_calls": [{"function": {
                    "name": "finish", "arguments": {
                        "status": "done", "summary": "researcher report " * 100}}}]}
            probe_state["n"] += 1
            i = probe_state["n"]
            return {"tool_calls": [{"function": {
                "name": "bash", "arguments": {
                    "cmd": f"date +%s.%N > ovl_{i}.start && sleep 1 && "
                           f"date +%s.%N > ovl_{i}.end",
                    "timeout": 30}}}]}

        for _ in range(10):
            self.srv.add("*", child_node)

        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("spawn three researchers in parallel")
            rid = rt.run_id
            cap_text = rt.swarm.format_report("a1")
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "done")

        # model calls stayed serialized on free-plan capacity 1
        self.assertEqual(self.srv.max_inflight, 1)

        events = J.read_events(rid)
        spawns = [e for e in events if e.get("type") == "agent_spawn"]
        self.assertEqual(len(spawns), 3)
        child_reports = [e for e in events if e.get("type") == "report"
                         and e.get("agent") in ("a1", "a2", "a3")]
        self.assertEqual(len(child_reports), 3)
        for e in child_reports:
            self.assertEqual(e["report"]["status"], "done")

        # wall-clock overlap: every probe started before any probe ended
        starts, ends = [], []
        for i in (1, 2, 3):
            starts.append(float((Path(self.cwd.name) / f"ovl_{i}.start").read_text()))
            ends.append(float((Path(self.cwd.name) / f"ovl_{i}.end").read_text()))
        self.assertLess(max(starts), min(ends),
                        f"probes did not overlap: starts={starts} ends={ends}")

        # reports are capped when handed back to the parent
        self.assertLessEqual(len(cap_text), 1600 + 300)
        self.assertIn("truncated", cap_text)
        tool_results = [m for m in lead_msgs(rid, self.home.name)
                        if m.get("role") == "tool"
                        and m.get("tool_name") == "spawn_swarm"]
        self.assertTrue(tool_results)
        joined = tool_results[0]["content"]
        self.assertEqual(joined.count("report truncated"), 3)
        self.assertLessEqual(len(joined), 3 * (1600 + 300))

    def test_kill9_mid_swarm_resume_reattaches_children(self):
        env = dict(os.environ)
        env["HYDRA_HOME"] = self.home.name
        env["PYTHONPATH"] = str(REPO)
        base = [sys.executable, "-m", "hydra",
                "--set", f"api.host={self.srv.url}",
                "--set", "api.retries=2",
                "--set", "journal.fsync=event",
                "--set", "termux.wake_lock=false",
                "--set", "termux.notify_on_finish=false",
                "--set", "termux.notify_on_park=false"]

        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {
                         "name": "spawn_agent",
                         "arguments": {"task": "sleep a while, then report",
                                       "role": "worker", "wait": False}}}]},
                     {"tool_calls": [{"function": {
                         "name": "agent_wait",
                         "arguments": {"ids": ["a1"], "timeout": 300}}}]},
                     {"tool_calls": [{"function": {
                         "name": "agent_wait",
                         "arguments": {"ids": ["a1"], "timeout": 300}}}]},
                     finish_node("lead got the child report"))
        self.srv.add("*",
                     {"tool_calls": [{"function": {"name": "bash",
                         "arguments": {"cmd":
                             "echo child-running > child.txt && sleep 45",
                             "timeout": 60}}}]},
                     finish_node("child finished after resume"))

        proc = subprocess.Popen(base + ["run the swarm kill test"],
                                cwd=self.cwd.name, env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        from hydra import config as C

        run_id, deadline = None, time.time() + 60
        while time.time() < deadline:
            if run_id is None:
                runs = [p.name for p in (Path(self.home.name) / "runs").glob("*")]
                for rid in runs:
                    ev = C.run_dir(rid) / "events.jsonl"
                    if ev.is_file() and "agent_spawn" in ev.read_text(encoding="utf-8"):
                        run_id = rid
                        break
            if (Path(self.cwd.name) / "child.txt").is_file():
                break
            time.sleep(0.1)
        self.assertIsNotNone(run_id, "run never spawned a child")
        self.assertTrue((Path(self.cwd.name) / "child.txt").is_file(), "child never started")
        time.sleep(0.3)  # let the child settle into the sleep
        proc.kill()
        proc.wait(timeout=10)
        try:
            proc.stdout.close()
        except Exception:
            pass
        self.assertEqual(proc.returncode, -9)

        resume = subprocess.run(base + ["resume"], cwd=self.cwd.name, env=env,
                                capture_output=True, text=True, timeout=180)
        self.assertEqual(resume.returncode, 0, resume.stdout + resume.stderr)
        subprocess.run(["pkill", "-f", "sleep 45"], capture_output=True)

        from hydra import journal as J
        events = J.read_events(run_id)
        self.assertTrue(any(e.get("type") == "agent_reattach" for e in events))
        child_report = [e for e in events if e.get("type") == "report"
                        and e.get("agent") == "a1"]
        self.assertTrue(child_report)
        self.assertEqual(child_report[-1]["report"]["status"], "done")
        trans = (C.run_dir(run_id) / "agents" / "a1" / "transcript.jsonl").read_text()
        self.assertIn("INTERRUPTED", trans)
        # finished calls are never re-run: exactly one bash happened in total
        bashes = [e for e in events if e.get("type") == "tool_call"
                  and e.get("tool") == "bash"]
        self.assertEqual(len(bashes), 1)


class BlackboardTests(Base):
    def test_note_recall_and_memory(self):
        from hydra.runtime import Runtime

        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        lead = rt.start_run("blackboard test")
        try:
            calls = [{"function": {"name": "note",
                                   "arguments": {"key": "api.base",
                                                 "value": "https://ollama.com",
                                                 "persist": True}}}]
            results = rt.executor.run(calls, lead)
            self.assertIn("noted", results[0]["content"])
            calls = [{"function": {"name": "recall",
                                   "arguments": {"key": "api.base"}}}]
            results = rt.executor.run(calls, lead)
            self.assertIn("api.base = https://ollama.com", results[0]["content"])
            mem = Path(self.home.name) / "memory" / "api.base.md"
            self.assertTrue(mem.is_file())
            self.assertIn("api.base", mem.read_text(encoding="utf-8"))
        finally:
            rt.close()


if __name__ == "__main__":
    unittest.main()
