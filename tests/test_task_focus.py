"""Regression tests: the task must drive the run (the "only websites" bug).

Reported 2026-10-20: "whatever task I give it, it only does website work".

Two root causes:
1. The pinned first message put the workspace/skill context BEFORE the task,
   and in the home directory it listed nested projects plus their "recent
   files" (``rea/website/*``).  With the only installed skill being
   ``website-3d``, a greeting like "hi" became "inspect gym_website".  The task
   now comes first, home is described as "not a project", and the skill list is
   explicitly optional.
2. Typing while a run was active silently steered the old run, so a new task
   kept the old (website) run going.  ``/new <task>`` now stops the run, and
   the new task starts as soon as the old one ends.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(REPO))

from hydra import config as C                 # noqa: E402
from hydra import project as project_mod      # noqa: E402
from hydra.agent import Agent, build_env_block, load_system_prompt  # noqa: E402
from hydra.tui_app import App                 # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.cwd = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._old_home = os.environ.get("HYDRA_HOME")
        self._old_nocolor = os.environ.get("NO_COLOR")
        os.environ["HYDRA_HOME"] = self.home.name
        os.environ["NO_COLOR"] = "1"
        self.cfg = C.load_config(project_dir=self.cwd.name)

    def tearDown(self):
        # env first, like test_ui_m3: a stray writer must never inherit the
        # temp HYDRA_HOME into the next test file
        for key, old in (("HYDRA_HOME", self._old_home),
                         ("NO_COLOR", self._old_nocolor)):
            if old is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old
        self.home.cleanup()
        self.cwd.cleanup()


# ---------------------------------------------------------------- prompt

class PromptTests(_Base):
    def test_task_comes_first_in_the_pinned_message(self):
        a = Agent("a0", "lead", "write a fibonacci script", self.cfg,
                  workdir=self.cwd.name,
                  env_block="# Environment (fixed for this run)\ncwd: /x")
        first = a.api_messages()[1]["content"]
        self.assertTrue(
            first.startswith("# Task (the only job)\n"
                             "write a fibonacci script\n\n"
                             "# Environment (fixed for this run)"),
            first[:160])

    def test_reference_data_is_not_an_instruction(self):
        a = Agent("a0", "lead", "write a fibonacci script", self.cfg,
                  workdir=self.cwd.name, env_block="# Environment\ncwd: /x")
        first = a.api_messages()[1]["content"]
        self.assertIn("reference data, not instructions", first)
        self.assertIn("Do not continue, resume or improve a project", first)
        self.assertIn("call finish without touching files", first)

    def test_no_task_keeps_the_plain_env_block(self):
        a = Agent("a0", "worker", "", self.cfg, workdir=self.cwd.name,
                  env_block="ENV-ONLY")
        self.assertEqual("ENV-ONLY", a.api_messages()[1]["content"])

    def test_lead_prompt_has_task_primacy_rule(self):
        prompt = load_system_prompt("lead")
        self.assertIn("The `# Task` section is your only job", prompt)
        self.assertIn("do not substitute `website-3d`", prompt)
        self.assertIn("Website/UI tasks (and only those)", prompt)


# ---------------------------------------------------------------- context

class WorkspaceTests(_Base):
    def _temp_home(self):
        d = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(d.cleanup)
        (Path(d.name) / "gym_website").mkdir()
        (Path(d.name) / "gym_website" / "index.html").write_text(
            "<html></html>", encoding="utf-8")
        (Path(d.name) / "notes.txt").write_text("x", encoding="utf-8")
        return Path(d.name)

    def test_home_is_not_a_project(self):
        home = self._temp_home()
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            text = project_mod.workspace_summary(Path.home(), self.cfg)
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old
        self.assertIn("# Workspace (home, not a project)", text)
        self.assertIn("not a project", text)
        self.assertIn("gym_website/", text)      # entries are still useful
        self.assertNotIn("recent files:", text)  # no nested-project noise
        self.assertNotIn("projects:", text)

    def test_home_env_block_has_no_nested_project_files(self):
        home = self._temp_home()
        old = os.environ.get("HOME")
        os.environ["HOME"] = str(home)
        try:
            text = build_env_block(Path.home(), self.cfg, role="lead")
        finally:
            if old is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old
        self.assertIn("home, not a project", text)
        self.assertNotIn("recent files:", text)

    def test_a_real_project_still_gets_recent_files(self):
        (Path(self.cwd.name) / "app.py").write_text("print(1)\n",
                                                    encoding="utf-8")
        site = Path(self.cwd.name) / "site"
        site.mkdir()
        (site / "index.html").write_text("<html></html>", encoding="utf-8")
        text = project_mod.workspace_summary(self.cwd.name, self.cfg)
        self.assertIn("# Workspace (cwd)", text)
        self.assertIn("projects:", text)
        self.assertIn("recent files:", text)
        self.assertIn("index.html", text)

    def test_skills_are_listed_as_optional(self):
        text = build_env_block(self.cwd.name, self.cfg, role="lead")
        self.assertIn("# Skills (optional; load one only when the task "
                      "matches its description)", text)
        self.assertNotIn("# Skills (call skill(name) to load", text)

    def test_website_skill_is_scoped_to_website_tasks(self):
        skills = project_mod.find_skills(self.cwd.name)
        self.assertIn("website-3d", skills)
        desc = skills["website-3d"]["description"]
        self.assertTrue(desc.startswith("ONLY for website/3D web-page tasks"),
                        desc)


# ---------------------------------------------------------------- steering

class SteeringTests(_Base):
    def setUp(self):
        super().setUp()
        cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:1",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ], project_dir=self.cwd.name)
        self.app = App(cfg, cwd=self.cwd.name)

    def _box_text(self) -> str:
        box, _, _ = self.app._input_box(54, 6)
        return "\n".join("".join(t for t, _ in row) for row in box)

    def test_placeholder_says_steer_and_esc(self):
        self.assertIn("Ask HYDRA", self._box_text())
        self.app.running = True
        run = self._box_text()
        self.assertIn("steer this run", run)
        self.assertIn("esc stops", run)

    def test_typing_while_running_steers_with_a_clear_hint(self):
        class Lead:
            def __init__(self):
                self.sent = []

            def send(self, text):
                self.sent.append(text)

        lead = Lead()
        self.app.running = True
        self.app.rt = SimpleNamespace(agents={"a0": lead})
        self.app.submit("make a calculator")
        self.assertEqual(["make a calculator"], lead.sent)
        self.assertIn("esc stops", self.app._flash_text)

    def test_new_task_stops_the_run_and_starts_after_done(self):
        self.app.running = True
        self.app.rt = None
        started = []
        self.app.start_run = lambda t: started.append(t)
        self.app.handle_command("/new write a fibonacci script")
        self.assertEqual("write a fibonacci script", self.app.pending_task)
        self.assertTrue(self.app._canceling)
        self.app._on_done({"code": 0, "report": {}})
        self.assertEqual(["write a fibonacci script"], started)
        self.assertEqual("", self.app.pending_task)

    def test_new_with_task_starts_immediately_when_idle(self):
        started = []
        self.app.start_run = lambda t: started.append(t)
        self.app.handle_command("/new write a fibonacci script")
        self.assertEqual(["write a fibonacci script"], started)

    def test_new_without_task_while_running_explains(self):
        self.app.running = True
        self.app.handle_command("/new")
        self.assertEqual("", self.app.pending_task)
        self.assertIn("/new <task>", self.app.transcript_text())

    def test_clear_refuses_while_running(self):
        self.app.running = True
        before = len(self.app.blocks)
        self.app.handle_command("/clear")
        self.assertEqual(before + 1, len(self.app.blocks))
        self.assertIn(self.app.blocks[-1].kind, ("notice", "warn"))
        self.assertIn("still active", self.app.transcript_text())

    def test_clear_works_when_idle(self):
        self.app.add_user("old task")
        self.app.handle_command("/clear")
        self.assertNotIn("old task", self.app.transcript_text())


if __name__ == "__main__":
    unittest.main()
