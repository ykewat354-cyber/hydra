"""T12 regression: a real (fake-backed) spawn_swarm run shows tool overlap
while the LLM gate still serializes model calls.

The fake server's callable nodes answer the two workers from one shared queue:
the first call from each worker gets a long ``bash``, the next gets ``finish``.
Both bash calls therefore run at the same time while the single free-plan model
slot keeps model calls strictly one at a time.
"""
from __future__ import annotations

import json
import unittest

from tests.test_phase1 import Base
from tests.fake_ollama import FakeOllama  # noqa: F401  (Base starts one)
from tools.swarm_analysis import analyse, load_events


def _worker_node(body):
    msgs = body.get("messages") or []
    blob = json.dumps(msgs)
    if any(m.get("role") == "tool" for m in msgs):
        return {"tool_calls": [{"function": {
            "name": "finish",
            "arguments": {"status": "done", "summary": "worker done"}}}]}
    name = "a.txt" if "a.txt" in blob else "b.txt"
    letter = name[0]
    return {"chunks": 3, "delay_chunks": 0.05,
            "content": "sleeping then writing",
            "tool_calls": [{"function": {
                "name": "bash",
                "arguments": {"cmd": f"sleep 1.2 && printf {letter} > {name}"}}}]}


class SwarmRunTests(Base):
    def test_parallel_workers_keep_model_calls_serial(self):
        from hydra import config as C
        from hydra.runtime import Runtime

        self.srv.add("gpt-oss:120b",
                     {"chunks": 3, "delay_chunks": 0.05,
                      "tool_calls": [{"function": {
                          "name": "spawn_swarm",
                          "arguments": {"tasks": [
                              "Run `sleep 1.2 && printf a > a.txt`, then finish.",
                              "Run `sleep 1.2 && printf b > b.txt`, then finish.",
                          ]}}}]},
                     {"tool_calls": [{"function": {
                         "name": "finish",
                         "arguments": {"status": "done",
                                       "summary": "two workers joined",
                                       "files_changed": ["a.txt", "b.txt"]}}}]})
        self.srv.add("gpt-oss:20b", *[_worker_node for _ in range(4)])
        rt = Runtime(self.cfg("loop.verify=never", "api.max_inflight=1"),
                     cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("spawn two workers in parallel and join them")
            rid = rt.run_id
        finally:
            rt.close()

        self.assertEqual(code, 0, report)
        from pathlib import Path
        self.assertEqual((Path(self.cwd.name) / "a.txt").read_text(), "a")
        self.assertEqual((Path(self.cwd.name) / "b.txt").read_text(), "b")
        out = analyse(load_events(C.run_dir(rid) / "events.jsonl"))
        self.assertEqual(out["agents"], 3)
        self.assertEqual(out["max_concurrent_model_calls"], 1)
        self.assertGreater(out["tool_overlap_seconds"], 0)


if __name__ == "__main__":
    unittest.main()
