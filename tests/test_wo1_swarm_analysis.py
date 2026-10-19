"""T12 regression: swarm_analysis parses a synthetic run journal."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.swarm_analysis import analyse, load_events

EVENTS = [
    {"ts": 0.0, "type": "run_start", "agent": "", "run": "r_x"},
    {"ts": 0.0, "type": "model_request", "agent": "a0"},
    {"ts": 0.5, "type": "model_request", "agent": "a1"},
    {"ts": 2.0, "type": "model_response", "agent": "a0", "role": "lead",
     "in_tokens": 100, "out_tokens": 10},
    {"ts": 3.0, "type": "model_response", "agent": "a1", "role": "worker",
     "in_tokens": 50, "out_tokens": 5},
    {"ts": 1.0, "type": "tool_call", "agent": "a1", "tool": "bash"},
    {"ts": 2.0, "type": "tool_call", "agent": "a1", "tool": "web_search"},
    {"ts": 3.0, "type": "tool_result", "agent": "a1", "tool": "web_search",
     "ms": 1000},
    {"ts": 4.0, "type": "tool_result", "agent": "a1", "tool": "bash",
     "ms": 3000},
]


class SwarmAnalysisTests(unittest.TestCase):
    def test_counts_overlap_and_tokens(self):
        out = analyse(EVENTS)
        self.assertEqual(out["max_concurrent_model_calls"], 2)
        self.assertEqual(out["tool_overlap_seconds"], 1.0)
        self.assertEqual(out["tool_ms_sum"], 4000)
        self.assertEqual(out["wall_seconds"], 4.0)
        self.assertEqual(out["lead_last_prompt_tokens"], 100)
        self.assertEqual(out["children_prompt_tokens_total"], 50)
        self.assertEqual(out["per_agent"]["a1"]["role"], "worker")

    def test_load_events_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "events.jsonl"
            path.write_text("\n".join(json.dumps(e) for e in EVENTS) + "\n",
                            encoding="utf-8")
            self.assertEqual(len(load_events(Path(tmp))), len(EVENTS))
            self.assertEqual(len(load_events(path)), len(EVENTS))

    def test_sequential_model_calls_peak_is_one(self):
        evs = [dict(e) for e in EVENTS]
        evs[2] = {"ts": 2.5, "type": "model_request", "agent": "a1"}
        evs[4] = {"ts": 3.0, "type": "model_response", "agent": "a1",
                  "role": "worker", "in_tokens": 50, "out_tokens": 5}
        self.assertEqual(analyse(evs)["max_concurrent_model_calls"], 1)


if __name__ == "__main__":
    unittest.main()
