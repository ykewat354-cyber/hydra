"""Phase 1 acceptance tests: streaming client, loop, core tools, journal, resume.

Acceptance: a fake-backed run creates hello.py, runs it, finishes; kill -9 mid-run
then `hydra resume` completes without re-running finished calls; the journal holds
the full event trail; the tolerant call parser recovers text tool calls.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

from tests.fake_ollama import FakeOllama


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOllama(tags=["gpt-oss:120b", "gemma4:31b"]).start()
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name

    def tearDown(self):
        self.srv.stop()
        self.home.cleanup()
        self.cwd.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home

    def cfg(self, *extra):
        from hydra import config as C
        overrides = [
            f"api.host={self.srv.url}",
            "api.retries=2",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ] + list(extra)
        return C.load_config(overrides=overrides, project_dir=self.cwd.name)


class EndToEndTests(Base):
    def test_hello_py_end_to_end(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        self.srv.add("gpt-oss:120b",
                     {"content": "Writing hello.py.",
                      "tool_calls": [{"function": {"name": "write_file",
                                                   "arguments": {"path": "hello.py",
                                                                 "content": "print(42)\n"}}}]},
                     {"content": "Running it.",
                      "tool_calls": [{"function": {"name": "bash",
                                                   "arguments": {"cmd": "python hello.py"}}}]},
                     {"tool_calls": [{"function": {"name": "finish",
                                                   "arguments": {"status": "done",
                                                                 "summary": "hello.py created and executed",
                                                                 "files_changed": ["hello.py"],
                                                                 "commands_run": ["python hello.py -> 42"]}}}]})
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("create hello.py printing 42 and run it")
            run_id = rt.run_id
        finally:
            rt.close()

        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "done")
        self.assertEqual((Path(self.cwd.name) / "hello.py").read_text(), "print(42)\n")

        evs = J.read_events(run_id)
        types = [e["type"] for e in evs]
        for t in ("run_start", "agent_start", "model_request", "model_response",
                  "tool_call", "tool_result", "report"):
            self.assertIn(t, types)
        bash_results = [e for e in evs if e["type"] == "tool_result"
                        and e.get("tool") == "bash"]
        self.assertTrue(bash_results and "42" in bash_results[0]["preview"])
        usage = J.read_usage(run_id)
        self.assertGreaterEqual(len(usage), 3)
        self.assertGreater(sum(r["in"] for r in usage), 0)

    def test_kill9_then_resume(self):
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {"name": "bash",
                                                   "arguments": {"cmd": "echo step1 > step1.txt && sleep 45",
                                                                 "timeout": 60}}}]},
                     {"tool_calls": [{"function": {"name": "bash",
                                                   "arguments": {"cmd": "echo step2 > step2.txt"}}}]},
                     {"tool_calls": [{"function": {"name": "finish",
                                                   "arguments": {"status": "done",
                                                                 "summary": "both steps done"}}}]})
        env = dict(os.environ)
        env["HYDRA_HOME"] = self.home.name
        env["PYTHONPATH"] = str(REPO)
        base = [sys.executable, "-m", "hydra",
                "--set", f"api.host={self.srv.url}",
                "--set", "api.retries=2",
                "--set", "journal.fsync=event",
                "--set", "termux.wake_lock=false",
                "--set", "termux.notify_on_finish=false"]
        proc = subprocess.Popen(base + ["run the two-step task"],
                                cwd=self.cwd.name, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        step1 = Path(self.cwd.name) / "step1.txt"
        deadline = time.time() + 25
        while time.time() < deadline and not step1.exists():
            time.sleep(0.1)
        self.assertTrue(step1.exists(), "first step never ran")
        time.sleep(0.5)  # let the event land on disk
        proc.kill()
        proc.wait(timeout=10)
        try:
            proc.stdout.close()
        except Exception:
            pass

        resume = subprocess.run(base + ["resume"], cwd=self.cwd.name, env=env,
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(resume.returncode, 0, resume.stdout + resume.stderr)
        self.assertTrue((Path(self.cwd.name) / "step2.txt").exists())

        from hydra import config as C
        from hydra import journal as J
        rid = (J.run_ids() or [None])[0]
        self.assertIsNotNone(rid)
        trans = (C.run_dir(rid) / "agents" / "a0" / "transcript.jsonl").read_text()
        self.assertIn("INTERRUPTED", trans)
        subprocess.run(["pkill", "-f", "sleep 45"], capture_output=True)


class ParserTests(unittest.TestCase):
    def test_text_call_recovery(self):
        from hydra.agent import parse_text_calls
        cases = [
            'some text {"name": "bash", "arguments": {"cmd": "ls"}} more',
            'text\n```json\n{"name": "read_file", "arguments": {"path": "a.py"}}\n```',
            '<tool_call>{"name": "finish", "arguments": {"status": "done", "summary": "ok"}}</tool_call>',
            'Action: bash\nAction Input: {"cmd": "pwd"}',
        ]
        for c in cases:
            calls = parse_text_calls(c)
            self.assertEqual(len(calls), 1, c)
            self.assertIn(calls[0]["function"]["name"], {"bash", "read_file", "finish"})

    def test_string_arguments(self):
        from hydra.agent import parse_text_calls
        calls = parse_text_calls('{"name": "bash", "arguments": "{\\"cmd\\": \\"ls\\"}"}')
        self.assertEqual(calls[0]["function"]["arguments"]["cmd"], "ls")

    def test_no_false_positives_on_plain_text(self):
        from hydra.agent import parse_text_calls
        self.assertEqual(parse_text_calls("All done. The result is 5."), [])
        self.assertEqual(parse_text_calls(""), [])


class ToolTests(Base):
    def test_file_tools_and_cap(self):
        from hydra.runtime import Runtime
        from hydra.agent import Agent

        cfg = self.cfg()
        rt = Runtime(cfg, cwd=self.cwd.name, quiet=True)
        agent = Agent("a0", "worker", "x", cfg, workdir=self.cwd.name)
        try:
            calls = [
                {"function": {"name": "write_file",
                              "arguments": {"path": "x/y.txt", "content": "hello\n"}}},
            ]
            results = rt.executor.run(calls, agent)
            self.assertIn("wrote", results[0]["content"])
            calls = [
                {"function": {"name": "read_file",
                              "arguments": {"path": "x/y.txt"}}},
            ]
            results = rt.executor.run(calls, agent)
            self.assertIn("hello", results[0]["content"])

            calls = [{"function": {"name": "edit_file",
                                   "arguments": {"path": "x/y.txt", "old": "hello",
                                                 "new": "world"}}}]
            results = rt.executor.run(calls, agent)
            self.assertIn("edited", results[0]["content"])
            self.assertEqual((Path(self.cwd.name) / "x" / "y.txt").read_text(), "world\n")

            # edit with non-unique old must error
            (Path(self.cwd.name) / "dup.txt").write_text("a a\n")
            calls = [{"function": {"name": "edit_file",
                                   "arguments": {"path": "dup.txt", "old": "a", "new": "b"}}}]
            results = rt.executor.run(calls, agent)
            self.assertIn("ERROR", results[0]["content"])

            # unknown tool -> error string, not an exception
            calls = [{"function": {"name": "nope", "arguments": {}}}]
            results = rt.executor.run(calls, agent)
            self.assertIn("ERROR", results[0]["content"])
        finally:
            rt.close()

    def test_grep_and_glob(self):
        from hydra.runtime import Runtime
        from hydra.agent import Agent

        cfg = self.cfg()
        rt = Runtime(cfg, cwd=self.cwd.name, quiet=True)
        agent = Agent("a0", "worker", "x", cfg, workdir=self.cwd.name)
        try:
            (Path(self.cwd.name) / "one.py").write_text("VALUE = 7\n")
            calls = [{"function": {"name": "grep",
                                   "arguments": {"pattern": "VALUE", "path": "."}}}]
            results = rt.executor.run(calls, agent)
            self.assertIn("one.py:1", results[0]["content"])
            calls = [{"function": {"name": "glob", "arguments": {"pattern": "*.py"}}}]
            results = rt.executor.run(calls, agent)
            self.assertIn("one.py", results[0]["content"])
        finally:
            rt.close()


class CliTests(Base):
    def test_version(self):
        env = dict(os.environ)
        env["HYDRA_HOME"] = self.home.name
        env["PYTHONPATH"] = str(REPO)
        out = subprocess.run([sys.executable, "-m", "hydra", "version"],
                             capture_output=True, text=True, env=env, cwd=self.cwd.name)
        self.assertEqual(out.returncode, 0)
        self.assertIn("hydra", out.stdout)

    def test_doctor_runs(self):
        env = dict(os.environ)
        env["HYDRA_HOME"] = self.home.name
        env["PYTHONPATH"] = str(REPO)
        out = subprocess.run([sys.executable, "-m", "hydra", "doctor"],
                             capture_output=True, text=True, env=env, cwd=self.cwd.name,
                             timeout=60)
        self.assertIn("doctor:", out.stdout)


if __name__ == "__main__":
    unittest.main()
