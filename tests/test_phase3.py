"""Phase 3 acceptance: context management.

Acceptance (§17):
* a scripted 200-turn run stays under `working_ctx` (compaction fires)
* spill files are readable via `read_file`
* stall injection fires on 3 identical calls (and escalates to the next model)
* `hydra usage` totals match the fake server's counts
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tests.test_phase1 import Base, REPO
from tests.test_phase2 import finish_node


class SpillTests(Base):
    def test_spill_readable_via_read_file(self):
        from hydra.runtime import Runtime

        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        lead = rt.start_run("spill test")
        try:
            calls = [{"function": {"name": "bash",
                                   "arguments": {"cmd": "seq 1 3000"}}}]
            results = rt.executor.run(calls, lead)
            text = results[0]["content"]
            self.assertIn("chars omitted", text)
            m = re.search(r"full output: ([^\]\n]+)\]", text)
            self.assertIsNotNone(m, text)
            spill = m.group(1).strip()
            self.assertTrue(Path(spill).is_file(), spill)

            calls = [{"function": {"name": "read_file",
                                   "arguments": {"path": spill, "start": 1, "end": 50}}}]
            results = rt.executor.run(calls, lead)
            body = results[0]["content"]
            self.assertIn("lines 1-50", body)
            self.assertIn("1: 1", body)
            self.assertIn("50: 50", body)
        finally:
            rt.close()


class StallTests(Base):
    def test_stall_fires_on_3_identical_calls(self):
        from hydra.runtime import Runtime
        from hydra import config as C
        from hydra import journal as J

        same = {"function": {"name": "bash", "arguments": {"cmd": "echo same"}}}
        for _ in range(3):
            self.srv.add("gpt-oss:120b", {"tool_calls": [same]})
        self.srv.add("gpt-oss:120b", finish_node("stayed"))
        self.srv.add("*", finish_node("escalated to another model"))

        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("stall test")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "done")
        stalls = [e for e in J.read_events(rid) if e.get("type") == "stall"]
        self.assertTrue(stalls, "stall must fire after 3 identical calls")
        self.assertEqual(stalls[0].get("repeated"), 3)
        self.assertEqual(stalls[0].get("escalate"), "gpt-oss:120b")
        trans = (C.run_dir(rid) / "agents" / "a0" / "transcript.jsonl").read_text()
        self.assertIn("Change approach", trans)
        self.assertEqual(self.srv.seen_models,
                         ["gpt-oss:120b"] * 3 + ["nemotron-3-ultra"])


class UsageTests(Base):
    def test_usage_totals_match_fake_and_cli(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        self.srv.add("gpt-oss:120b",
                     {"content": "one",
                      "tool_calls": [{"function": {"name": "bash",
                                                   "arguments": {"cmd": "echo hi"}}}],
                      "usage": {"in": 12000, "out": 3000}},
                     {"content": "", "tool_calls": [{"function": {
                         "name": "finish",
                         "arguments": {"status": "done", "summary": "usage done"}}}],
                      "usage": {"in": 20000, "out": 5000}})
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("usage test")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        recs = J.read_usage(rid)
        self.assertEqual(len(recs), 2)
        self.assertEqual(sum(int(r["in"]) for r in recs), 32000)
        self.assertEqual(sum(int(r["out"]) for r in recs), 8000)

        env = dict(os.environ)
        env["HYDRA_HOME"] = self.home.name
        env["PYTHONPATH"] = str(REPO)
        out = subprocess.run([sys.executable, "-m", "hydra", "usage", rid],
                             capture_output=True, text=True, env=env,
                             cwd=self.cwd.name, timeout=60)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn("32k in", out.stdout)
        self.assertIn("8k out", out.stdout)


class CompactTests(Base):
    def test_200_turn_run_stays_under_working_ctx(self):
        from hydra.runtime import Runtime
        from hydra import config as C
        from hydra import journal as J

        small = 5000
        turns = 200
        nodes = [{"tool_calls": [{"function": {"name": "glob",
                 "arguments": {"pattern": f"uniq-{i}-*.none"}}}]}
                 for i in range(turns)]
        nodes.append(finish_node("200 turns done"))
        self.srv.add("gpt-oss:120b", *nodes)
        for _ in range(20):  # summarizer replies (wildcard queue)
            self.srv.add("*", {"content":
                               "GOAL: keep doing unique work\nSTATUS: mid\n"
                               "DECISIONS: none\nFILES: none\n"
                               "COMMANDS: glob probes\nFINDINGS: none\n"
                               "BLOCKERS: none\nNEXT: continue"})
        rt = Runtime(self.cfg(f"loop.working_ctx={small}"), cwd=self.cwd.name,
                     quiet=True)
        sizes = []
        orig = rt.router.chat

        def wrapped(messages, **kw):
            sizes.append(int(len(json.dumps(messages, ensure_ascii=False)) / 3.5))
            return orig(messages, **kw)

        rt.router.chat = wrapped
        try:
            code, report = rt.run("do 200 unique turns")
            rid = rt.run_id
            final_est = rt.agents["a0"].estimate_tokens()
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "done")
        compacts = [e for e in J.read_events(rid) if e.get("type") == "compact"]
        self.assertTrue(compacts, "compaction must fire during a 200-turn run")
        self.assertLessEqual(max(sizes), small,
                             f"biggest prompt {max(sizes)} tokens > working_ctx")
        self.assertLessEqual(final_est, small)
        # the digest survived and the kept tail is intact
        digest = [m for m in rt.agents["a0"].messages
                  if str(m.get("content", "")).startswith("# State digest")]
        self.assertTrue(digest)
        self.assertEqual(C.run_dir(rid).name, rid)


if __name__ == "__main__":
    unittest.main()
