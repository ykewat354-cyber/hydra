"""Root-fix regression tests for the October 2026 gym/3D website failures.

Covered here:
* workspace + recent-runs context injected into the env block
* built-in website-3d skill discovery (project skills still override it)
* `finish` auto-filling files_changed from recorded file writes
* verifier: duplicate live asset trees + unfinished todos
* model probe counting text-emitted tool calls (the gpt-oss:120b pruning bug)
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hydra import verifier
from hydra import project as project_mod

from tests.fake_ollama import FakeOllama
from tests.test_phase1 import Base


class _HomeCase(unittest.TestCase):
    """Temp HYDRA_HOME + temp cwd, no fake Ollama server needed."""

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name

    def tearDown(self):
        self.home.cleanup()
        self.cwd.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home


# ---------------------------------------------------------------- workspace

class WorkspaceContextTests(_HomeCase):
    def test_existing_project_is_described(self):
        g = Path(self.cwd.name) / "gym_website"
        (g / "assets" / "css").mkdir(parents=True)
        (g / "css").mkdir()
        (g / "assets" / "css" / "style.css").write_text("body{}", encoding="utf-8")
        (g / "css" / "style.css").write_text("body{}", encoding="utf-8")
        (g / "index.html").write_text("<html></html>", encoding="utf-8")

        text = project_mod.workspace_summary(self.cwd.name)
        self.assertIn("gym_website/", text)
        self.assertIn("index.html", text)
        self.assertIn("assets/", text)
        self.assertIn("css/", text)

    def test_heavy_and_sensitive_names_are_not_listed(self):
        (Path(self.cwd.name) / "app").mkdir()
        (Path(self.cwd.name) / "app" / "index.html").write_text(
            "<html></html>", encoding="utf-8")
        (Path(self.cwd.name) / "node_modules").mkdir()
        (Path(self.cwd.name) / "node_modules" / "big.js").write_text(
            "x", encoding="utf-8")
        for name in ("auth_login.log", "api_key.txt", ".env"):
            (Path(self.cwd.name) / name).write_text("secret", encoding="utf-8")

        text = project_mod.workspace_summary(self.cwd.name)
        self.assertIn("app/", text)
        for bad in ("node_modules", "auth_login", "api_key", ".env"):
            self.assertNotIn(bad, text)

    def test_workspace_can_be_disabled(self):
        (Path(self.cwd.name) / "app").mkdir()
        text = project_mod.workspace_summary(
            self.cwd.name, {"project": {"workspace": False}})
        self.assertEqual(text, "")

    def test_recent_runs_filter_by_cwd_and_show_files(self):
        cwd = str(Path(self.cwd.name).resolve())
        runs = Path(self.home.name) / "runs"
        r1 = runs / "r_test1"
        r1.mkdir(parents=True)
        (r1 / "run.json").write_text(json.dumps({
            "cwd": cwd, "task": "Gym ki website banao",
            "status": "done", "started": 100.0}), encoding="utf-8")
        (r1 / "events.jsonl").write_text(json.dumps({
            "type": "report",
            "report": {"files_changed": ["gym_website/index.html"],
                       "artifacts": []}}) + "\n", encoding="utf-8")
        r2 = runs / "r_other"
        r2.mkdir(parents=True)
        (r2 / "run.json").write_text(json.dumps({
            "cwd": "/somewhere/else", "task": "unrelated",
            "status": "done", "started": 999.0}), encoding="utf-8")

        text = project_mod.recent_runs_summary(self.cwd.name)
        self.assertIn("r_test1", text)
        self.assertIn("Gym ki website banao", text)
        self.assertIn("gym_website/index.html", text)
        self.assertNotIn("r_other", text)

    def test_env_block_includes_workspace(self):
        from hydra.agent import build_env_block
        from hydra import config as C

        (Path(self.cwd.name) / "gym_website").mkdir()
        (Path(self.cwd.name) / "gym_website" / "index.html").write_text(
            "<html></html>", encoding="utf-8")
        cfg = C.load_config(project_dir=self.cwd.name)
        text = build_env_block(self.cwd.name, cfg, role="lead")
        self.assertIn("# Workspace", text)
        self.assertIn("gym_website/", text)


# ---------------------------------------------------------------- skills

class BuiltinSkillTests(_HomeCase):
    def test_builtin_website_skill_is_listed(self):
        skills = project_mod.find_skills(self.cwd.name)
        self.assertIn("website-3d", skills)
        self.assertIn("Three.js", skills["website-3d"]["body"])
        listing = project_mod.skills_listing(self.cwd.name, role="lead")
        self.assertIn("website-3d", listing)

    def test_project_skill_overrides_builtin(self):
        d = Path(self.cwd.name) / ".hydra" / "skills" / "website-3d"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(
            "---\nname: website-3d\ndescription: local override\n---\nLOCAL-BODY\n",
            encoding="utf-8")
        skills = project_mod.find_skills(self.cwd.name)
        self.assertEqual(skills["website-3d"]["description"], "local override")
        self.assertIn("LOCAL-BODY", skills["website-3d"]["body"])


# ---------------------------------------------------------------- finish

class FinishAutoFillTests(Base):
    def test_finish_autofills_files_changed_from_writes(self):
        from hydra.runtime import Runtime

        self.srv.add("gpt-oss:120b", {"tool_calls": [
            {"function": {"name": "write_file",
                          "arguments": {"path": "hello.txt", "content": "hi"}}}]})
        self.srv.add("gpt-oss:120b", {"tool_calls": [
            {"function": {"name": "finish",
                          "arguments": {"status": "done",
                                        "summary": "wrote hello.txt"}}}]})
        rt = Runtime(self.cfg("loop.verify=never"), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("write hello.txt then finish")
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        self.assertTrue((Path(self.cwd.name) / "hello.txt").is_file())
        self.assertIn("hello.txt", report.get("files_changed") or [])


# ---------------------------------------------------------------- verifier

class VerifierRootFixTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _agent(self, artifacts=("index.html",), todos=None):
        data = {}
        if todos is not None:
            data["todos"] = list(todos)
        return SimpleNamespace(
            workdir=self.workdir,
            report={"status": "done", "summary": "built the site",
                    "files_changed": list(artifacts),
                    "artifacts": list(artifacts)},
            data=data,
            task="build a 3D website",
        )

    def _html(self, body: str = "<h1>Hi</h1>") -> None:
        (self.workdir / "index.html").write_text(
            "<!doctype html><html><head></head><body>" + body + "</body></html>",
            encoding="utf-8")

    def test_duplicate_asset_trees_are_flagged(self):
        self._html('<link rel="stylesheet" href="css/style.css">')
        for d in ("css", "assets/css"):
            p = self.workdir / d
            p.mkdir(parents=True)
            (p / "style.css").write_text("body { color: #111; }", encoding="utf-8")
        issues = verifier.deterministic_issues(self._agent())
        self.assertTrue(any("two live asset trees" in str(i) for i in issues),
                        issues)

    def test_single_asset_tree_is_not_flagged(self):
        self._html()
        p = self.workdir / "css"
        p.mkdir()
        (p / "style.css").write_text("body { color: #111; }", encoding="utf-8")
        self.assertEqual(verifier.deterministic_issues(self._agent()), [])

    def test_pending_todo_blocks_done_finish(self):
        self._html()
        agent = self._agent(todos=["x setup", "build 3D scene", "host it"])
        issues = verifier.deterministic_issues(agent)
        self.assertTrue(any("still open" in str(i) for i in issues), issues)

    def test_completed_todos_are_fine(self):
        self._html()
        agent = self._agent(todos=["x setup", "x build 3D scene", "x host it"])
        issues = verifier.deterministic_issues(agent)
        self.assertFalse(any("still open" in str(i) for i in issues), issues)


# ---------------------------------------------------------------- probe

class ProbeTextToolTests(Base):
    def setUp(self):
        super().setUp()
        self.srv.stop()
        self.srv = FakeOllama(tags=["gpt-oss:120b"]).start()

    def test_text_tool_call_counts_as_tool_capable(self):
        from hydra import models as M
        from hydra.client import OllamaCloud
        from hydra.gate import LLMGate

        tool_text = ('<tool_call>{"name": "add", "arguments": '
                     '{"a": 2, "b": 3}}</tool_call>')
        self.srv.add("gpt-oss:120b",
                     {"content": "OK"},      # ping
                     {"content": tool_text},  # tools, text-emitted
                     {"content": '{"ok": true}'},  # json
                     {"content": "OK"},      # think probe
                     {"content": "OK"})      # spare
        cfg = self.cfg()
        client = OllamaCloud(self.srv.url, "test-key", 5, 5)
        res = M.probe_one(client, LLMGate(1), "gpt-oss:120b")
        self.assertEqual(res["status"], "ok")
        self.assertTrue(res["tools"], res)


if __name__ == "__main__":
    unittest.main()
