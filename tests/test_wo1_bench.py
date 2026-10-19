"""F2 — model bench fixtures, scorecard merge, tune proposals, config layering.

Pure unit tests cover fixture generation and pass/fail checks, scorecard
merging, tune ranking/proposals and the roles.auto.toml config layer.  One
integration test drives a real bench cell through tests/fake_ollama.py.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.test_phase1 import Base
from hydra import bench
from hydra import config as C


class FixtureTests(unittest.TestCase):
    def test_b1_fixture_and_check(self):
        with tempfile.TemporaryDirectory() as d:
            meta = bench.make_fixture("B1", d)
            self.assertIn("sum of the integers 1 to 100", meta["task"])
            ok, _ = bench.check_fixture("B1", d, meta)
            self.assertFalse(ok)
            (Path(d) / "sum.py").write_text("print(sum(range(1, 101)))\n")
            ok, _ = bench.check_fixture("B1", d, meta)
            self.assertTrue(ok)

    def test_b2_fixture_and_check(self):
        with tempfile.TemporaryDirectory() as d:
            meta = bench.make_fixture("B2", d)
            self.assertEqual((Path(d) / "calc.py").read_text(), bench.B2_CALC)
            ok, _ = bench.check_fixture("B2", d, meta)
            self.assertFalse(ok)
            calc = Path(d) / "calc.py"
            calc.write_text(calc.read_text().replace("a - b", "a + b"))
            ok, detail = bench.check_fixture("B2", d, meta)
            self.assertTrue(ok, detail)
            # editing the tests must fail even when they pass
            test = Path(d) / "test_calc.py"
            test.write_text(test.read_text() + "\n# changed\n")
            ok, detail = bench.check_fixture("B2", d, meta)
            self.assertFalse(ok)
            self.assertIn("tests_unchanged=False", detail)

    def test_b3_fixture_expected_and_check(self):
        with tempfile.TemporaryDirectory() as d:
            meta = bench.make_fixture("B3", d)
            expected = bench.b3_expected()
            self.assertEqual(meta["expected"], expected)
            lines = expected.strip().splitlines()
            self.assertEqual(len(lines), 9)
            self.assertEqual(lines[0], "1. notes/a.txt: TODO: refactor the parser")
            for decoy in ("todo: lower-case", "TODOS", "TO DO:"):
                self.assertNotIn(decoy, expected)
            self.assertEqual(len(list((Path(d) / "notes").iterdir())), 6)
            (Path(d) / "todos.md").write_text(expected)
            ok, _ = bench.check_fixture("B3", d, meta)
            self.assertTrue(ok)
            (Path(d) / "todos.md").write_text(expected.replace("1.", "1)"))
            ok, _ = bench.check_fixture("B3", d, meta)
            self.assertFalse(ok)


class ScorecardTests(unittest.TestCase):
    def test_merge_appends_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "scorecard.json"
            bench.save_scorecard(bench.merge_scorecard(
                bench.load_scorecard(p), [{"model": "m1", "ok": True}]), p)
            data = bench.merge_scorecard(bench.load_scorecard(p),
                                         [{"model": "m2", "ok": False}])
            bench.save_scorecard(data, p)
            got = json.loads(p.read_text())
            self.assertEqual([c["model"] for c in got["cells"]], ["m1", "m2"])
            self.assertEqual(got["version"], 1)
            self.assertGreater(got["updated"], 0)

    def test_merge_caps_history(self):
        data = {"version": 1, "cells": [{"n": i} for i in range(5)]}
        bench.merge_scorecard(data, [{"n": 5}], cap=3)
        self.assertEqual([c["n"] for c in data["cells"]], [3, 4, 5])

    def test_broken_scorecard_loads_empty(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "scorecard.json"
            p.write_text("{not json")
            self.assertEqual(bench.load_scorecard(p)["cells"], [])


class TuneTests(unittest.TestCase):
    def cells(self):
        return [
            {"model": "alpha:1b", "ok": True, "est_usd": 0.10, "seconds": 5},
            {"model": "alpha:1b", "ok": True, "est_usd": 0.10, "seconds": 5},
            {"model": "beta:2b", "ok": True, "est_usd": 0.02, "seconds": 3},
            {"model": "beta:2b", "ok": False, "est_usd": 0.02, "seconds": 3},
            {"model": "gamma:3b", "ok": False, "est_usd": 0.01, "seconds": 2},
        ]

    def test_aggregate_and_rank(self):
        agg = bench.aggregate(self.cells())
        self.assertEqual(agg["alpha:1b"]["pass_rate"], 1.0)
        self.assertAlmostEqual(agg["beta:2b"]["avg_usd"], 0.02)
        self.assertEqual(bench.rank_models(agg),
                         ["alpha:1b", "beta:2b", "gamma:3b"])

    def test_proposal_reliability_then_cost_and_critic_family(self):
        proposals = bench.propose(self.cells())
        self.assertEqual(set(proposals), set(bench.ROLES))
        self.assertEqual(proposals["lead"][0], "alpha:1b")
        self.assertNotEqual(bench.family(proposals["critic"][0]),
                            bench.family(proposals["coder"][0]))
        self.assertIn("beta:2b", proposals["critic"])
        # tune --apply must fix the reviewer too (the slow 155s verifier)
        self.assertEqual(proposals["reviewer"], proposals["critic"])
        for role in ("planner", "debugger", "tester", "scribe"):
            self.assertEqual(proposals[role], proposals["lead"])

    def test_render_and_apply(self):
        proposals = bench.propose(self.cells())
        with tempfile.TemporaryDirectory() as d:
            p = bench.apply_proposals(proposals, Path(d) / "roles.auto.toml")
            text = p.read_text()
            self.assertIn("[roles.lead]", text)
            self.assertIn("alpha:1b", text)
            self.assertIn("[roles.critic]", text)
            self.assertIn("[roles.reviewer]", text)
            self.assertIn("[roles.planner]", text)


class LayeringTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.project = tempfile.TemporaryDirectory()
        self.old = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name

    def tearDown(self):
        self.home.cleanup()
        self.project.cleanup()
        if self.old is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self.old

    def write(self, path, chain):
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(f'[roles.lead]\nchain = {json.dumps([chain])}\n')

    def test_precedence(self):
        home = Path(self.home.name)
        auto = home / "roles.auto.toml"
        cfgp = home / "config.toml"
        proj = Path(self.project.name) / ".hydra" / "config.toml"
        self.write(auto, "auto")
        got = C.load_config(project_dir=self.project.name)
        self.assertEqual(C.get(got, "roles.lead.chain"), ["auto"])
        self.assertEqual(C.get(got, "roles.lead.think"), "medium")  # defaults kept

        self.write(cfgp, "cfg")
        got = C.load_config(project_dir=self.project.name)
        self.assertEqual(C.get(got, "roles.lead.chain"), ["cfg"])

        self.write(proj, "proj")
        got = C.load_config(project_dir=self.project.name)
        self.assertEqual(C.get(got, "roles.lead.chain"), ["proj"])

        got = C.load_config(overrides=['roles.lead.chain=["flag"]'],
                            project_dir=self.project.name)
        self.assertEqual(C.get(got, "roles.lead.chain"), ["flag"])


class BudgetTests(unittest.TestCase):
    def test_budget_aborts_remaining_cells(self):
        calls = []

        def fake_cell(model, task, cfg, **kw):
            calls.append((model, task))
            return {"model": model, "task": task, "ok": True,
                    "est_usd": 0.10, "seconds": 0.1}

        with tempfile.TemporaryDirectory() as d, \
                mock.patch.object(bench, "run_cell", side_effect=fake_cell), \
                mock.patch.object(bench, "save_scorecard",
                                  return_value=Path(d) / "scorecard.json"):
            res = bench.run({}, ["m"], ["B1", "B2", "B3"], max_usd=0.05)
        self.assertEqual(len(calls), 1)
        self.assertTrue(res["aborted"])
        self.assertEqual(res["skipped"], 2)


class FakeServerBenchTests(Base):
    def test_one_bench_cell_end_to_end(self):
        from hydra.runtime import Runtime  # noqa: F401  (cell constructs it)
        self.srv.add("gpt-oss:120b",
                     {"content": "Writing sum.py.",
                      "tool_calls": [{"function": {
                          "name": "write_file",
                          "arguments": {"path": "sum.py",
                                        "content": "print(sum(range(1, 101)))\n"}}}]},
                     {"content": "Running it.",
                      "tool_calls": [{"function": {
                          "name": "bash",
                          "arguments": {"cmd": "python sum.py"}}}]},
                     {"tool_calls": [{"function": {
                         "name": "finish",
                         "arguments": {"status": "done", "summary": "5050"}}}]})
        rec = bench.run_cell("gpt-oss:120b", "B1",
                             self.cfg("loop.verify=never"))
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["status"], "done")
        self.assertEqual(rec["turns"], 3)
        self.assertGreater(rec["in"], 0)
        self.assertGreater(rec["est_usd"], 0)
        self.assertEqual(rec["tool_errors"], 0)
        self.assertTrue(rec["run_id"])
        self.assertEqual(self.srv.pending("gpt-oss:120b"), 0)


if __name__ == "__main__":
    unittest.main()
