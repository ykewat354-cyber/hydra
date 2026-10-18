"""Phase 2 acceptance: model registry + LLM gate + router error matrix.

Acceptance (spec §17 phase 2):
* 402 on model A -> failover to B, A marked `paid` in models.json
* 429 + Retry-After -> the wait is honored
* 3 concurrent agents with max_inflight=1 never overlap a model call
* all models 402 -> checkpoint + notification + exit 75
* `hydra models probe` fills models.json (ping/tools/json/think)
"""
from __future__ import annotations

import json
import threading
import time
import unittest
from pathlib import Path

from tests.fake_ollama import FakeOllama
from tests.test_phase1 import Base


def finish_node(summary="done"):
    return {"tool_calls": [{"function": {"name": "finish",
                                         "arguments": {"status": "done",
                                                       "summary": summary}}}]}


class GateTests(unittest.TestCase):
    def test_priority_deeper_first(self):
        from hydra.gate import LLMGate

        g = LLMGate(1)
        g.acquire(0)  # main thread holds the single slot
        order = []

        def worker(name, prio):
            g.acquire(prio)
            order.append(name)
            g.release()

        tA = threading.Thread(target=worker, args=("A", 0))
        tA.start()
        deadline = time.time() + 2
        while time.time() < deadline and g.waiting < 1:
            time.sleep(0.01)
        tB = threading.Thread(target=worker, args=("B", 5))
        tB.start()
        time.sleep(0.15)   # let B enqueue behind A
        g.release()        # slot frees: deeper agent (B) must go first
        tA.join(5)
        tB.join(5)
        self.assertEqual(order[0], "B")
        self.assertEqual(sorted(order), ["A", "B"])

    def test_aimd_shrink_then_recover(self):
        from hydra.gate import LLMGate

        g = LLMGate(2, shrink_secs=0.2)
        self.assertEqual(g.cap, 2)
        g.shrink()
        self.assertEqual(g.cap, 1)
        time.sleep(0.25)
        with g.slot(0):
            pass  # a check ramps the cap back up one step
        self.assertEqual(g.cap, 2)

    def test_timeout_cleans_up_waiter(self):
        from hydra.gate import LLMGate

        g = LLMGate(1)
        g.acquire(0)
        with self.assertRaises(TimeoutError):
            g.acquire(0, timeout=0.15)
        self.assertEqual(g.waiting, 0)
        g.release()
        self.assertEqual(g.inflight, 0)


class ThreeModelBase(Base):
    def setUp(self):
        super().setUp()
        self.srv.stop()
        self.srv = FakeOllama(tags=["gpt-oss:120b", "nemotron-3-ultra",
                                    "gemma4:31b"]).start()


class RouterModelTests(ThreeModelBase):
    def test_402_failover_marks_paid(self):
        from hydra.runtime import Runtime

        self.srv.add("gpt-oss:120b", {"status": 402,
                                      "body": {"error": "requires subscription"}})
        self.srv.add("nemotron-3-ultra", {"content": "hi", **finish_node("from B")})
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            reply = rt.router.chat([{"role": "user", "content": "x"}], role="lead")
            self.assertEqual(reply.tool_calls[0]["function"]["name"], "finish")
            self.assertEqual(self.srv.seen_models,
                             ["gpt-oss:120b", "nemotron-3-ultra"])
            self.assertEqual(rt.registry.status_of("gpt-oss:120b"), "paid")

            # next call: A is pruned from the chain, B is tried directly
            self.srv.reset()
            self.srv.add("*", {"content": "again", **finish_node("second")})
            rt.router.chat([{"role": "user", "content": "y"}], role="lead")
            self.assertEqual(self.srv.seen_models, ["nemotron-3-ultra"])
        finally:
            rt.close()

    def test_429_retry_after_honored(self):
        from hydra.runtime import Runtime

        self.srv.add("gpt-oss:120b",
                     {"status": 429, "retry_after": 0.4, "body": {"error": "slow down"}},
                     {"content": "recovered", **finish_node("after wait")})
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        delays = []
        rt.router.sleep = lambda d: delays.append(d)
        try:
            reply = rt.router.chat([{"role": "user", "content": "x"}], role="lead")
            self.assertEqual(reply.content, "recovered")
            self.assertTrue(delays, "router must wait when RateLimited")
            self.assertAlmostEqual(delays[0], 0.4, places=2)
        finally:
            rt.close()

    def test_all_paid_parks_with_exit_75(self):
        from hydra.runtime import Runtime

        for m in ("gpt-oss:120b", "nemotron-3-ultra", "gemma4:31b"):
            self.srv.add(m, {"status": 402, "body": {"error": "paid only"}})
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("do something")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 75, report)
        meta = json.loads((Path(self.home.name) / "runs" / rid / "run.json").read_text())
        self.assertEqual(meta.get("status"), "parked")
        data = json.loads((Path(self.home.name) / "models.json").read_text())
        for m in ("gpt-oss:120b", "nemotron-3-ultra", "gemma4:31b"):
            self.assertEqual(data["models"][m]["status"], "paid", m)

    def test_gate_serializes_three_agents(self):
        from hydra.runtime import Runtime

        for _ in range(3):
            self.srv.add("gpt-oss:120b",
                         {"content": "ok " * 30, "chunks": 4, "delay_chunks": 0.08,
                          **finish_node("x")})
        rt = Runtime(self.cfg("api.max_inflight=1"), cwd=self.cwd.name, quiet=True)
        errs = []

        def call():
            try:
                rt.router.chat([{"role": "user", "content": "hi"}], role="lead")
            except Exception as exc:  # pragma: no cover - failure path
                errs.append(exc)

        threads = [threading.Thread(target=call) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(60)
        try:
            self.assertEqual(errs, [])
            self.assertEqual(self.srv.max_inflight, 1)
            self.assertEqual(rt.gate.max_observed, 1)
        finally:
            rt.close()

    def test_think_bad_request_is_repaired(self):
        from hydra.runtime import Runtime

        self.srv.add("gpt-oss:120b",
                     {"status": 400, "body": {"error": "invalid think value: low"}},
                     {"content": "fine", **finish_node("no think")})
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            rt.router.chat([{"role": "user", "content": "x"}], role="lead")
            self.assertEqual(rt.registry.think_style("gpt-oss:120b"), "none")
            reqs = self.srv.chat_requests()
            self.assertEqual(len(reqs), 2)
            self.assertIn("think", reqs[0]["body"])
            self.assertNotIn("think", reqs[1]["body"])
        finally:
            rt.close()


class ProbeTests(ThreeModelBase):
    def test_probe_fills_registry(self):
        from hydra import models as M
        from hydra.client import OllamaCloud
        from hydra.gate import LLMGate

        add_call = {"content": "",
                    "tool_calls": [{"function": {"name": "add",
                                                 "arguments": {"a": 2, "b": 3}}}]}
        for name, think_nodes in (
                ("gpt-oss:120b", [{"content": "OK"}]),                    # levels accepted
                ("nemotron-3-ultra", [{"status": 400,
                                       "body": {"error": "invalid think value"}},
                                      {"content": "OK"}]),               # bool fallback
        ):
            self.srv.add(name,
                         {"content": "OK"},            # ping
                         add_call,                     # tools
                         {"content": '{"ok": true}'},  # json
                         *think_nodes)
        self.srv.add("gemma4:31b", {"status": 402, "body": {"error": "paid"}})

        cfg = self.cfg()
        client = OllamaCloud(self.srv.url, "test-key", 5, 5)
        gate = LLMGate(1)
        reg = M.ModelRegistry(cfg)
        results = M.probe_all(cfg, client, gate, reg)

        a = results["gpt-oss:120b"]
        self.assertEqual(a["status"], "ok")
        self.assertTrue(a["tools"])
        self.assertTrue(a["json"])
        self.assertEqual(a["think_style"], "levels")
        b = results["nemotron-3-ultra"]
        self.assertEqual(b["status"], "ok")
        self.assertEqual(b["think_style"], "bool")
        self.assertEqual(results["gemma4:31b"]["status"], "paid")

        self.assertEqual(reg.status_of("gemma4:31b"), "paid")
        self.assertEqual(reg.think_style("nemotron-3-ultra"), "bool")
        self.assertTrue((Path(self.home.name) / "models.json").is_file())
        self.assertTrue(reg.needs_reprobe() is False)

    def test_all_paid_flag_and_pin(self):
        from hydra import models as M

        cfg = self.cfg()
        reg = M.ModelRegistry(cfg)
        for m in ("gpt-oss:120b", "nemotron-3-ultra", "gemma4:31b"):
            reg.mark(m, "paid")
        from hydra.router import Router

        rt_router = Router(cfg, client=None, registry=reg)
        self.assertTrue(rt_router._all_paid("lead"))
        self.assertEqual(rt_router.chain_for("lead"), [])
        try:
            rt_router.chat([{"role": "user", "content": "x"}], role="lead")
        except Exception as exc:
            self.assertTrue(getattr(exc, "all_paid", False))
        else:  # pragma: no cover
            self.fail("expected AllModelsFailed")

        # pin writes a config layer that load_config picks up
        self.assertEqual(M.cli(cfg, ["pin", "nemotron-3-ultra", "--role", "lead"]), 0)
        cfg2 = self.cfg()
        chain = cfg2["roles"]["lead"]["chain"]
        self.assertEqual(chain[0], "nemotron-3-ultra")


if __name__ == "__main__":
    unittest.main()
