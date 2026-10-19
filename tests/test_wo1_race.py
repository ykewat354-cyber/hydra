"""WO1 T17: ask_models race(2) must degrade to sequential on one slot.

The free-plan gate has a single LLM slot.  A two-model race cannot fire in
parallel there, so it must fall back to first-valid sequential execution and
say so in both its tool result and the journal's ``tool_result`` event.
"""
from __future__ import annotations

import unittest

from tests.test_phase1 import Base


class AskModelsRaceOneSlotTests(Base):
    def test_race_two_models_degrades_to_race_seq(self):
        from hydra import journal as J
        from hydra.runtime import Runtime

        rt = Runtime(self.cfg("api.max_inflight=1"), cwd=self.cwd.name, quiet=True)
        self.addCleanup(rt.close)
        lead = rt.start_run("race degrade")
        run_id = rt.run_id

        self.srv.add("gpt-oss:120b", {"content": "FIRST"})
        self.srv.add("nemotron-3-ultra", {"content": "SECOND"})
        calls = [{"function": {"name": "ask_models", "arguments": {
            "prompt": "go", "mode": "race", "n": 2}}}]
        results = rt.executor.run(calls, lead)

        # The tool result names the degraded strategy.
        self.assertIn("mode=race-seq", results[0]["content"])
        # The journal event carries the same marker, so a run's log is auditable.
        events = [e for e in J.read_events(run_id)
                  if e.get("type") == "tool_result"
                  and e.get("tool") == "ask_models"]
        self.assertTrue(events)
        self.assertIn("mode=race-seq", events[-1]["preview"])
        # Sequential first-valid: only the first model was contacted, one slot used.
        self.assertEqual(self.srv.seen_models, ["gpt-oss:120b"])
        self.assertEqual(self.srv.max_inflight, 1)
        self.assertEqual(rt.gate.plan_cap, 1)


if __name__ == "__main__":
    unittest.main()
