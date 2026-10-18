"""Phase 5 acceptance: multi-model modes + verifier (§7.5, §5.3).

Acceptance (§17):
* scripted disagreement between fake models resolves correctly per mode
  (vote majority + judge tie-break, race first-valid, moa aggregate, critic
  revise loop, cascade escalation)
* all modes degrade sensibly when max_inflight=1
* verifier `fail` re-enters the loop once and then passes
"""
from __future__ import annotations

import json
import time
import unittest

from tests.test_phase1 import Base
from tests.test_phase2 import finish_node


def finish_files(summary="done", files=("x.py",)):
    return {"tool_calls": [{"function": {"name": "finish", "arguments": {
        "status": "done", "summary": summary, "files_changed": list(files)}}}]}


class MultiModelTests(Base):
    def _rt(self, *extra):
        from hydra.runtime import Runtime
        rt = Runtime(self.cfg(*extra), cwd=self.cwd.name, quiet=True)
        self.addCleanup(rt.close)
        return rt

    def test_vote_majority(self):
        rt = self._rt()
        self.srv.add("gpt-oss:120b", {"content": '{"choice": "A"}'})
        self.srv.add("nemotron-3-ultra", {"content": '{"choice": "B"}'})
        self.srv.add("gemma4:31b", {"content": '{"choice": "A"}'})
        out = rt.router.multi(prompt="pick one", mode="vote",
                              models=["gpt-oss:120b", "nemotron-3-ultra",
                                      "gemma4:31b"])
        self.assertEqual(out["mode"], "vote")
        self.assertEqual(out["votes"]["gpt-oss:120b"], json.dumps("A"))
        self.assertIn("A", out["text"])
        self.assertIn(out["model"], ("gpt-oss:120b", "gemma4:31b"))

    def test_vote_tie_goes_to_judge(self):
        rt = self._rt()
        self.srv.add("gpt-oss:120b", {"content": '{"choice": "X"}'})
        self.srv.add("nemotron-3-ultra", {"content": '{"choice": "Y"}'})
        self.srv.add("gemma4:31b", {"content": '{"choice": 2}'})  # the judge
        out = rt.router.multi(prompt="pick one", mode="vote",
                              models=["gpt-oss:120b", "nemotron-3-ultra"])
        self.assertEqual(out["mode"], "vote+judge")
        self.assertEqual(out["judge"], "gemma4:31b")
        self.assertEqual(out["model"], "nemotron-3-ultra")
        self.assertIn("Y", out["text"])

    def test_race_degrades_to_sequential_on_one_slot(self):
        rt = self._rt()
        self.srv.add("gpt-oss:120b", {"content": "FIRST"})
        self.srv.add("nemotron-3-ultra", {"content": "SECOND"})
        out = rt.router.multi(prompt="go", mode="race",
                              models=["gpt-oss:120b", "nemotron-3-ultra"])
        self.assertEqual(out["mode"], "race-seq")
        self.assertEqual(out["model"], "gpt-oss:120b")
        self.assertEqual(out["text"], "FIRST")
        self.assertEqual(self.srv.seen_models, ["gpt-oss:120b"])

    def test_race_parallel_when_slots_allow(self):
        rt = self._rt("api.max_inflight=3")
        self.srv.add("gpt-oss:120b",
                     {"content": "SLOW" * 6, "chunks": 6, "delay_chunks": 0.1})
        self.srv.add("nemotron-3-ultra",
                     {"content": "FAST" * 3, "chunks": 3, "delay_chunks": 0.05})
        out = rt.router.multi(prompt="go", mode="race",
                              models=["gpt-oss:120b", "nemotron-3-ultra"])
        self.assertEqual(out["mode"], "race")
        self.assertEqual(out["model"], "nemotron-3-ultra")
        self.assertTrue(out["text"].startswith("FAST"), out["text"])
        self.assertGreaterEqual(self.srv.max_inflight, 2)
        time.sleep(0.8)  # let the losing stream finish cleanly

    def test_moa_aggregates_drafts(self):
        rt = self._rt()
        self.srv.add("gpt-oss:120b", {"content": "DRAFT-ONE"})
        self.srv.add("gemma4:31b", {"content": "DRAFT-TWO"})
        self.srv.add("nemotron-3-ultra", {"content": "FINAL-SYNTH"})
        out = rt.router.multi(prompt="go", mode="moa",
                              models=["gpt-oss:120b", "gemma4:31b"])
        self.assertEqual(out["mode"], "moa")
        self.assertEqual(out["aggregator"], "nemotron-3-ultra")
        self.assertEqual(out["text"], "FINAL-SYNTH")

    def test_critic_revise_loop(self):
        rt = self._rt()
        self.srv.add("gpt-oss:120b", {"content": "DRAFT-V1"},
                     {"content": "DRAFT-V2"})
        self.srv.add("nemotron-3-ultra",
                     {"content": '{"issues": [{"what": "missing X", "fix": "add X"}]}'},
                     {"content": '{"issues": []}'})
        out = rt.router.multi(prompt="go", mode="critic",
                              models=["gpt-oss:120b"])
        self.assertEqual(out["mode"], "critic")
        self.assertEqual(out["rounds"], 2)
        self.assertEqual(out["text"], "DRAFT-V2")
        self.assertEqual(self.srv.seen_models,
                         ["gpt-oss:120b", "nemotron-3-ultra",
                          "gpt-oss:120b", "nemotron-3-ultra"])

    def test_cascade_escalates_on_bad_json(self):
        rt = self._rt()
        self.srv.add("gpt-oss:120b", {"content": "not json at all"})
        self.srv.add("nemotron-3-ultra", {"content": '{"ok": true}'})
        out = rt.router.multi(prompt="go", mode="cascade", accept="json",
                              models=["gpt-oss:120b", "nemotron-3-ultra"])
        self.assertEqual(out["mode"], "cascade")
        self.assertTrue(out["accepted"])
        self.assertEqual(out["model"], "nemotron-3-ultra")
        self.assertIn("ok", out["text"])

    def test_ask_models_tool_is_wired(self):
        from hydra.runtime import Runtime
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        lead = rt.start_run("ask_models tool")
        try:
            self.srv.add("gpt-oss:120b", {"content": "4"})
            calls = [{"function": {"name": "ask_models", "arguments": {
                "prompt": "2+2?", "mode": "race", "models": ["gpt-oss:120b"]}}}]
            results = rt.executor.run(calls, lead)
            body = results[0]["content"]
            self.assertIn("4", body)
            self.assertIn("mode=race-seq", body)
        finally:
            rt.close()


class VerifierTests(Base):
    def test_verifier_fail_reenters_loop_once(self):
        from hydra.runtime import Runtime
        from hydra import journal as J
        from hydra import config as C

        self.srv.add("gpt-oss:120b",
                     finish_files("wrote x.py", ["x.py"]),
                     finish_files("fixed x.py", ["x.py"]))
        self.srv.add("nemotron-3-ultra",
                     {"content": '{"verdict": "fail", "issues": '
                                 '[{"what": "x.py does not exist", "fix": "create it"}]}'},
                     {"content": '{"verdict": "pass", "issues": []}'})
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("verify me")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "done")
        verifies = [e for e in J.read_events(rid) if e.get("type") == "verify"]
        self.assertEqual([v.get("verdict") for v in verifies], ["fail", "pass"])
        self.assertEqual(verifies[0].get("model"), "nemotron-3-ultra")
        trans = (C.run_dir(rid) / "agents" / "a0" / "transcript.jsonl").read_text()
        self.assertIn("REJECTED your finish", trans)
        self.assertIn("x.py does not exist", trans)

    def test_verify_never_skips_reviewer(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        self.srv.add("gpt-oss:120b", finish_files("wrote y.py", ["y.py"]))
        rt = Runtime(self.cfg("loop.verify=never"), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("no verify")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        self.assertEqual([e for e in J.read_events(rid)
                          if e.get("type") == "verify"], [])
        self.assertEqual(self.srv.seen_models, ["gpt-oss:120b"])

    def test_verify_rounds_exhaust_then_accept(self):
        """A stubborn verifier cannot loop forever: after verify_rounds, accept."""
        from hydra.runtime import Runtime
        from hydra import journal as J

        self.srv.add("gpt-oss:120b",
                     *[finish_files(f"attempt {i}", ["z.py"]) for i in range(4)])
        for _ in range(8):
            self.srv.add("nemotron-3-ultra",
                         {"content": '{"verdict": "fail", "issues": ["nope"]}'})
        rt = Runtime(self.cfg("loop.verify_rounds=2"), cwd=self.cwd.name,
                     quiet=True)
        try:
            code, report = rt.run("stubborn verify")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        verifies = [e for e in J.read_events(rid) if e.get("type") == "verify"]
        self.assertEqual(len(verifies), 2)
        self.assertEqual(report["status"], "done")


if __name__ == "__main__":
    unittest.main()
