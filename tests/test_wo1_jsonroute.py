"""F1 — JSON-role routing: fake models of mixed class and price.

Unit level: candidate selection (strict → cost → latency, json_class filter,
role_chain policy, unknown models), lowest-think translation, tolerant
normalization, the one-repair rule, candidate fallback, exclude/diversity, and
the journal event.  Integration level (fake server): the planner and the
verifier are routed to the cheapest conformant model, not the role-chain head.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.test_phase1 import Base
from hydra import config as C
from hydra import jsonroute
from hydra.agent import DIGEST_SCHEMA
from hydra.client import Reply
from hydra.models import ModelRegistry
from hydra.planner import DAG_SCHEMA
from hydra.router import Router
from hydra.verifier import VERDICT_SCHEMA


class FakeClient:
    def __init__(self, scripts=None):
        self.scripts = {m: list(v) for m, v in (scripts or {}).items()}
        self.calls = []

    def chat(self, model, messages, tools=None, think=None, options=None,
             stream=True, on_delta=None, format=None):
        self.calls.append({"model": model, "think": think, "format": format,
                           "messages": [dict(m) for m in messages]})
        q = self.scripts.get(model) or []
        item = q.pop(0) if q else {}
        if isinstance(item, Exception):
            raise item
        return Reply(model=model, content=item.get("content", ""),
                     thinking=item.get("thinking", ""),
                     prompt_eval_count=item.get("tin", 10),
                     eval_count=item.get("tout", 5), ms=item.get("ms", 50))


class FakeJournal:
    def __init__(self):
        self.events = []

    def event(self, type_, agent=None, **fields):
        self.events.append(dict(type=type_, agent=agent, **fields))


def _reply(content):
    return {"content": content}


PRICES = {
    "s-cheap": {"in": 1.0, "out": 1.0},
    "s-pricey": {"in": 10.0, "out": 10.0},
    "t-cheap": {"in": 0.1, "out": 0.1},
    "m1": {"in": 1.0, "out": 1.0},
    "m2": {"in": 2.0, "out": 2.0},
    "a-bool": {"in": 1.0, "out": 1.0},
    "b-levels": {"in": 1.0, "out": 1.0},
    "c-none": {"in": 1.0, "out": 1.0},
}


class JsonRouteUnitTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name

    def tearDown(self):
        self.home.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home

    # ------------------------------------------------------------ helpers
    def registry(self, entries):
        cfg = C.load_config()
        reg = ModelRegistry(cfg, path=Path(self.home.name) / "models.json")
        for name, meta in entries.items():
            reg.mark(name, "ok", tools=True, json=True)
            if meta.get("json_class"):
                reg.set_json_class(name, meta["json_class"],
                                   json_med_in=meta.get("med_in", 0),
                                   json_med_out=meta.get("med_out", 0),
                                   json_p50_ms=meta.get("p50", 0))
        return reg

    def router(self, reg, client, chain, prices=None):
        cfg = C.load_config(overrides=[f'roles.lead.chain={json.dumps(chain)}',
                                       f'roles.reviewer.chain={json.dumps(chain)}'])
        return Router(cfg, client, prices or PRICES, registry=reg)

    # ------------------------------------------------------------ candidates
    def test_strict_before_tolerant_then_cost_then_latency(self):
        reg = self.registry({
            "s-cheap": {"json_class": "strict", "med_in": 100, "med_out": 50, "p50": 500},
            "s-pricey": {"json_class": "strict", "med_in": 100, "med_out": 50, "p50": 100},
            "t-cheap": {"json_class": "tolerant", "med_in": 100, "med_out": 50, "p50": 50},
            "nope": {"json_class": "no", "med_in": 1, "med_out": 1, "p50": 1},
            "u-model": {},
        })
        r = self.router(reg, FakeClient(), ["t-cheap", "nope", "s-pricey"])
        self.assertEqual(jsonroute.candidates(r, "lead"),
                         ["s-cheap", "s-pricey", "t-cheap", "u-model"])
        # role_chain keeps the configured order, then the rest by cost
        self.assertEqual(jsonroute.candidates(r, "lead", policy="role_chain"),
                         ["t-cheap", "s-pricey", "s-cheap", "u-model"])

    def test_unknown_models_keep_chain_order(self):
        reg = self.registry({"b": {}, "a": {}})
        r = self.router(reg, FakeClient(), ["b", "a"])
        self.assertEqual(jsonroute.candidates(r, "lead"), ["b", "a"])
        self.assertEqual(jsonroute.candidates(r, "lead", policy="role_chain"),
                         ["b", "a"])

    def test_all_no_falls_back_to_the_chain(self):
        reg = self.registry({"b": {"json_class": "no"}, "a": {"json_class": "no"}})
        r = self.router(reg, FakeClient({"b": [_reply('{"verdict":"pass","issues":[]}')]}),
                        ["b", "a"])
        self.assertEqual(jsonroute.candidates(r, "lead"), ["b", "a"])
        res = jsonroute.structured_chat(r, [{"role": "user", "content": "x"}],
                                        role="lead", schema=VERDICT_SCHEMA,
                                        schema_name="verdict")
        self.assertTrue(res.ok)
        self.assertEqual(res.model, "b")

    def test_exclude_drops_the_producer(self):
        reg = self.registry({
            "m1": {"json_class": "strict", "med_in": 100, "med_out": 50, "p50": 100},
            "m2": {"json_class": "strict", "med_in": 100, "med_out": 50, "p50": 100},
        })
        r = self.router(reg, FakeClient(), ["m1", "m2"])
        self.assertEqual(jsonroute.candidates(r, "reviewer", exclude=("m1",)),
                         ["m2"])
        # excluding every candidate keeps them anyway (a call must be possible)
        self.assertIn("m1", jsonroute.candidates(r, "reviewer", exclude=("m1", "m2")))

    # ------------------------------------------------------------ thinking
    def test_lowest_think_dialects(self):
        reg = self.registry({"b-levels": {}, "a-bool": {}, "c-none": {}})
        reg.set_think_style("a-bool", "bool")
        reg.set_think_style("b-levels", "levels")
        reg.set_think_style("c-none", "none")
        self.assertEqual(jsonroute.lowest_think(reg, "b-levels"), "low")
        self.assertIs(jsonroute.lowest_think(reg, "a-bool"), False)
        self.assertIsNone(jsonroute.lowest_think(reg, "c-none"))

    def test_structured_call_sends_lowest_think(self):
        reg = self.registry({
            "a-bool": {"json_class": "strict", "med_in": 10, "med_out": 5, "p50": 10},
            "b-levels": {"json_class": "strict", "med_in": 10, "med_out": 5, "p50": 10},
            "c-none": {"json_class": "strict", "med_in": 10, "med_out": 5, "p50": 10},
        })
        reg.set_think_style("a-bool", "bool")
        reg.set_think_style("b-levels", "levels")
        reg.set_think_style("c-none", "none")
        prose = _reply("this is not json at all")
        client = FakeClient({m: [prose, prose]
                             for m in ("a-bool", "b-levels", "c-none")})
        r = self.router(reg, client, ["c-none", "b-levels", "a-bool"],
                        prices={m: {"in": 1.0, "out": 1.0}
                                for m in ("a-bool", "b-levels", "c-none")})
        res = jsonroute.structured_chat(r, [{"role": "user", "content": "x"}],
                                        role="lead", schema=VERDICT_SCHEMA,
                                        schema_name="verdict")
        self.assertFalse(res.ok)
        self.assertEqual([c["think"] for c in client.calls],
                         [False, False, "low", "low", None, None])
        self.assertEqual(client.calls[0]["format"], VERDICT_SCHEMA)

    # ------------------------------------------------------------ normalization
    def test_bare_array_plan_is_wrapped_without_repair(self):
        reg = self.registry({"m": {"json_class": "strict", "med_in": 10,
                                   "med_out": 5, "p50": 10}})
        nodes = [{"id": 1, "role": "coder", "title": "write sum.py", "deps": []},
                 {"id": 2, "role": "tester", "title": "test it", "deps": [1]}]
        client = FakeClient({"m": [_reply(json.dumps(nodes))]})
        r = self.router(reg, client, ["m"])
        res = jsonroute.structured_chat(r, [{"role": "user", "content": "plan"}],
                                        role="lead", schema=DAG_SCHEMA,
                                        schema_name="plan", task="plan a 2-step task")
        self.assertTrue(res.ok)
        self.assertTrue(res.normalized)
        self.assertFalse(res.repaired)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(len(res.obj["steps"]), 2)

    def test_fenced_verdict_is_normalized(self):
        reg = self.registry({"m": {"json_class": "strict", "med_in": 10,
                                   "med_out": 5, "p50": 10}})
        fenced = "```json\n" + json.dumps({"verdict": "fail", "issues": []}) + "\n```"
        client = FakeClient({"m": [_reply(fenced)]})
        r = self.router(reg, client, ["m"])
        res = jsonroute.structured_chat(r, [{"role": "user", "content": "x"}],
                                        role="lead", schema=VERDICT_SCHEMA,
                                        schema_name="verdict")
        self.assertTrue(res.ok)
        self.assertTrue(res.normalized)
        self.assertEqual(res.obj["verdict"], "fail")

    def test_prose_is_not_normalized(self):
        obj, errors, normalized = jsonroute.accept(
            "it probably passes", VERDICT_SCHEMA, "verdict")
        self.assertIsNone(obj)
        self.assertFalse(normalized)
        self.assertTrue(errors)

    # ------------------------------------------------------------ repair rule
    def test_one_repair_with_validator_error_then_next_candidate(self):
        reg = self.registry({
            "m1": {"json_class": "strict", "med_in": 10, "med_out": 5, "p50": 10},
            "m2": {"json_class": "strict", "med_in": 10, "med_out": 5, "p50": 20},
        })
        good = {"verdict": "pass", "issues": []}
        client = FakeClient({"m1": [_reply("I think it passes."),
                                    _reply("Still prose, sorry.")],
                             "m2": [_reply(json.dumps(good))]})
        r = self.router(reg, client, ["m1", "m2"])
        res = jsonroute.structured_chat(r, [{"role": "user", "content": "judge"}],
                                        role="lead", schema=VERDICT_SCHEMA,
                                        schema_name="verdict")
        self.assertTrue(res.ok)
        self.assertEqual(res.model, "m2")
        self.assertFalse(res.repaired)
        self.assertEqual([c["model"] for c in client.calls], ["m1", "m1", "m2"])
        repair_msg = client.calls[1]["messages"][-1]["content"]
        self.assertIn("Validator errors", repair_msg)
        self.assertIn("did not satisfy", repair_msg)
        self.assertEqual(client.calls[1]["format"], VERDICT_SCHEMA)

    def test_repair_result_is_accepted(self):
        reg = self.registry({"m1": {"json_class": "strict", "med_in": 10,
                                    "med_out": 5, "p50": 10}})
        good = {"verdict": "pass", "issues": []}
        client = FakeClient({"m1": [_reply("nope"), _reply("```json\n"
                                                           + json.dumps(good)
                                                           + "\n```")]})
        r = self.router(reg, client, ["m1"])
        res = jsonroute.structured_chat(r, [{"role": "user", "content": "judge"}],
                                        role="lead", schema=VERDICT_SCHEMA,
                                        schema_name="verdict")
        self.assertTrue(res.ok)
        self.assertTrue(res.repaired)
        self.assertEqual(res.model, "m1")
        self.assertEqual(len(client.calls), 2)

    def test_journal_records_the_route(self):
        reg = self.registry({"m1": {"json_class": "strict", "med_in": 10,
                                    "med_out": 5, "p50": 10}})
        client = FakeClient({"m1": [_reply('{"verdict":"pass","issues":[]}')]})
        r = self.router(reg, client, ["m1"])
        r.journal = FakeJournal()
        res = jsonroute.structured_chat(r, [{"role": "user", "content": "judge"}],
                                        role="lead", schema=VERDICT_SCHEMA,
                                        schema_name="verdict")
        self.assertTrue(res.ok)
        routes = [e for e in r.journal.events if e["type"] == "json_route"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0]["model"], "m1")
        self.assertEqual(routes[0]["schema"], "verdict")
        self.assertTrue(routes[0]["ok"])

    # ------------------------------------------------------------ router knob
    def test_chain_override_pins_one_model(self):
        reg = self.registry({"x": {"json_class": "strict", "med_in": 10,
                                   "med_out": 5, "p50": 10}})
        client = FakeClient({"x": [_reply('{"verdict":"pass","issues":[]}')]})
        r = self.router(reg, client, ["x", "other"])
        reply = r.chat([{"role": "user", "content": "x"}], role="lead",
                       format=VERDICT_SCHEMA, chain_override=["x"])
        self.assertEqual(reply.model, "x")
        self.assertEqual([c["model"] for c in client.calls], ["x"])


class JsonRouteIntegrationTests(Base):
    def test_planner_routes_to_cheapest_strict_model(self):
        from hydra.runtime import Runtime
        from hydra import planner

        rt = Runtime(self.cfg('roles.planner.chain=["gpt-oss:120b","cheap-strict"]'),
                     cwd=self.cwd.name, quiet=True)
        self.addCleanup(rt.close)
        rt.registry.mark("gpt-oss:120b", "ok", tools=True, json=True)
        rt.registry.set_json_class("gpt-oss:120b", "tolerant",
                                   json_med_in=500, json_med_out=200, json_p50_ms=1500)
        rt.registry.mark("cheap-strict", "ok", tools=True, json=True)
        rt.registry.set_json_class("cheap-strict", "strict",
                                   json_med_in=100, json_med_out=50, json_p50_ms=400)
        rt.registry.set_think_style("cheap-strict", "levels")
        self.srv.add("cheap-strict", {"content": json.dumps({
            "goal": "write sum.py and test it",
            "steps": [{"id": 1, "title": "write sum.py", "deps": [], "role": "coder"},
                      {"id": 2, "title": "test it", "deps": [1], "role": "tester"}]})})
        dag, err = planner.make_dag(rt, "write sum.py then test it")
        self.assertEqual(err, "")
        self.assertEqual(self.srv.seen_models, ["cheap-strict"])
        body = self.srv.chat_requests()[0]["body"]
        self.assertEqual(body["format"]["required"], ["goal", "steps"])
        self.assertEqual(body["think"], "low")

    def test_verifier_routes_away_from_the_producer(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        # producer is strict and cheapest; the reviewer must pick the other
        # conformant model because of the diversity exclude.
        rt = Runtime(self.cfg("loop.verify=auto",
                              'roles.reviewer.chain=["gpt-oss:120b","v-tolerant"]'),
                     cwd=self.cwd.name, quiet=True)
        runtime = rt
        rt.registry.mark("gpt-oss:120b", "ok", tools=True, json=True)
        rt.registry.set_json_class("gpt-oss:120b", "strict",
                                   json_med_in=10, json_med_out=5, json_p50_ms=10)
        rt.registry.mark("v-tolerant", "ok", tools=True, json=True)
        rt.registry.set_json_class("v-tolerant", "tolerant",
                                   json_med_in=100, json_med_out=50, json_p50_ms=10)
        self.srv.add("gpt-oss:120b", {"tool_calls": [{"function": {
            "name": "finish", "arguments": {
                "status": "done", "summary": "wrote x.py", "files_changed": ["x.py"]}}}]})
        self.srv.add("v-tolerant", {"content": json.dumps(
            {"verdict": "pass", "issues": []})})
        try:
            code, report = rt.run("verify me")
            rid = runtime.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        verifies = [e for e in J.read_events(rid) if e.get("type") == "verify"]
        self.assertEqual(len(verifies), 1)
        self.assertEqual(verifies[0].get("model"), "v-tolerant")


if __name__ == "__main__":
    unittest.main()
