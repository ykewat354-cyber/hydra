"""Phase 10: full-screen TUI, compact plain output, web-fetch fallback,
research artifact verification. Pure stdlib + a local fake web server.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import select
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

from tests.test_phase1 import Base
from tests.test_phase2 import finish_node

REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------- formatting

class FormatTests(unittest.TestCase):
    def test_wrap_never_exceeds_width(self):
        from hydra import ui_format as U
        text = "short " + "b" * 80 + " tail " + "नमस्ते दुनिया " * 6
        for width in (20, 33, 40, 80):
            for row in U.wrap_display(text, width, "❯ ", "  "):
                self.assertLessEqual(U.display_width(row), width, repr(row))

    def test_tool_summary_is_human_not_json(self):
        from hydra import ui_format as U
        s = U.tool_summary("bash", {"cmd": "python test.py", "timeout": 30})
        self.assertEqual(s, "python test.py")
        self.assertNotIn("{", s)
        s2 = U.tool_summary("web_search", {"query": "reverse engineering projects"})
        self.assertIn("reverse engineering projects", s2)
        self.assertNotIn('"query"', s2)
        s3 = U.tool_summary("write_file", {"path": "RESEARCH.md", "content": "x" * 100})
        self.assertEqual(s3, "RESEARCH.md")
        self.assertNotIn("content", s3)

    def test_json_reply_is_not_raw(self):
        from hydra import ui_format as U
        raw = ('{"status":"done","summary":"Created TUI_SMOKE.txt.",'
               '"files_changed":["TUI_SMOKE.txt"],"artifacts":["TUI_SMOKE.txt"]}')
        rows = U.markdown_rows(raw, 70)
        text = "\n".join(r for r, _ in rows)
        self.assertIn("DONE · Created TUI_SMOKE.txt.", text)
        self.assertIn("TUI_SMOKE.txt", text)
        self.assertNotIn("{", text)
        self.assertNotIn('"status"', text)
        generic = U.markdown_rows('{"tools":["Ghidra","Radare2"],"count":2}', 70)
        gtext = "\n".join(r for r, _ in generic)
        self.assertIn("Ghidra", gtext)
        self.assertNotIn("{", gtext)

    def test_streaming_json_shows_a_placeholder_not_braces(self):
        from hydra.tui_app import App, Block
        from hydra import config as C
        cfg = C.load_config(overrides=["api.host=http://127.0.0.1:1"])
        app = App(cfg, cwd=tempfile.gettempdir())
        app.blocks.append(Block("assistant", text='{"status":"done",', final=False))
        text = "\n".join(app.render(80, 20))
        self.assertIn("writing structured output", text)
        self.assertNotIn("{", text)
        app.blocks[-1].text = ('{"status":"done","summary":"Finished.",'
                               '"files_changed":["RESEARCH.md"]}')
        app.blocks[-1].final = True
        app._bump()
        text = "\n".join(app.render(80, 20))
        self.assertIn("DONE · Finished.", text)
        self.assertIn("RESEARCH.md", text)
        self.assertNotIn('"status"', text)

    def test_markdown_rows_keep_prose_and_bullets(self):
        from hydra import ui_format as U
        rows = U.markdown_rows("# Title\nA **bold** line with `code`.\n- one\n- two", 40)
        text = "\n".join(r for r, _ in rows)
        self.assertIn("Title", text)
        self.assertIn("A bold line with code.", text)
        self.assertIn("• one", text)
        self.assertNotIn("**", text)

    def test_result_brief_web_is_compact(self):
        from hydra import ui_format as U
        ok, brief = U.result_brief("web_fetch", "x" * 400, chars=12000, ms=850)
        self.assertTrue(ok)
        self.assertIn("12,000 chars", brief)
        self.assertIn("0.8s", brief)
        ok2, brief2 = U.result_brief("bash", "ERROR: boom\nmore", ms=20)
        self.assertFalse(ok2)
        self.assertIn("ERROR", brief2)


# ---------------------------------------------------------------- plain TUI

class PlainTuiTests(unittest.TestCase):
    def _run(self, ev):
        from hydra.tui import Tui
        t = Tui()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t.event(ev)
        return t, buf.getvalue()

    def test_assistant_text_is_visible(self):
        t, out = self._run({"type": "assistant", "agent": "a0",
                            "text": "Here is the **answer**.\n\n- item one\n- item two"})
        self.assertIn("Here is the answer.", out)
        self.assertIn("• item one", out)
        self.assertNotIn("**", out)

    def test_tool_call_has_no_raw_json(self):
        t, out = self._run({"type": "tool_call", "agent": "a0", "tool": "bash",
                            "args": {"cmd": "echo hi", "secret_key": "do-not-show"}})
        self.assertIn("echo hi", out)
        self.assertNotIn("do-not-show", out)
        self.assertNotIn('{"cmd"', out)

    def test_report_prints_summary_findings_and_artifact(self):
        t, out = self._run({"type": "report", "agent": "a0", "report": {
            "status": "done", "summary": "Wrote the complete list.",
            "findings": ["100 unique projects"], "files_changed": ["RESEARCH.md"],
            "artifacts": ["RESEARCH.md"], "open_issues": []}})
        self.assertIn("Wrote the complete list.", out)
        self.assertIn("100 unique projects", out)
        self.assertIn("RESEARCH.md", out)
        self.assertTrue(t.printed_report)

    def test_narrow_result_fits(self):
        old = os.environ.get("COLUMNS")
        os.environ["COLUMNS"] = "40"
        try:
            from hydra.tui import Tui
            t = Tui()
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                t.event({"type": "tool_result", "agent": "a1", "tool": "bash",
                         "preview": "y" * 300})
            line = buf.getvalue().rstrip("\n")
            self.assertIn("bash", line)
            self.assertLessEqual(len(line), 40, line)
        finally:
            if old is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = old


# ---------------------------------------------------------------- full TUI

class _FakeCfg:
    pass


class AppTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        self._old_nocolor = os.environ.get("NO_COLOR")
        os.environ["HYDRA_HOME"] = self.home.name
        os.environ["NO_COLOR"] = "1"
        from hydra import config as C
        self.cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:1",
            "api.retries=1",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ], project_dir=self.cwd.name)

    def tearDown(self):
        self.home.cleanup()
        self.cwd.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home
        if self._old_nocolor is None:
            os.environ.pop("NO_COLOR", None)
        else:
            os.environ["NO_COLOR"] = self._old_nocolor

    def _app(self):
        from hydra.tui_app import App
        return App(self.cfg, cwd=self.cwd.name)

    def test_bare_main_non_tty_prints_help(self):
        from hydra.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--set", "api.host=http://127.0.0.1:1"])
        self.assertEqual(code, 0)
        self.assertIn("open the full-screen TUI", buf.getvalue())

    def test_assistant_and_tool_events_render_compactly(self):
        app = self._app()
        app.event({"type": "run_start", "run": "r_t", "task": "research",
                   "cwd": self.cwd.name, "ts": time.time()})
        app.event({"type": "assistant", "agent": "a0",
                   "text": "I found **Ghidra** and wrote the list."})
        app.event({"type": "tool_call", "agent": "a0", "tool": "bash",
                   "args": {"cmd": "echo hi", "internal": "hidden"}})
        app.event({"type": "tool_result", "agent": "a0", "tool": "bash",
                   "preview": "hi\n", "chars": 3, "ms": 200, "lines": 2})
        app.drain()
        text = "\n".join(app.render(80, 30))
        self.assertIn("I found Ghidra and wrote the list.", text)
        self.assertIn("echo hi", text)
        self.assertNotIn("hidden", text)
        self.assertIn("bash", text)
        # WO2 §4.6 tool card: bullet + name + summary, result under └, elapsed
        self.assertIn("● bash  echo hi", text)
        self.assertIn("└ hi", text)
        self.assertIn("0.2s", text)

    def test_report_and_artifacts_are_visible(self):
        app = self._app()
        app.event({"type": "run_start", "run": "r_t2", "task": "research",
                   "cwd": self.cwd.name, "ts": time.time()})
        app.event({"type": "report", "agent": "a0", "report": {
            "status": "done", "summary": "Complete list saved.",
            "findings": ["100 unique entries"], "files_changed": ["RESEARCH.md"],
            "artifacts": ["RESEARCH.md"], "open_issues": []}})
        app.drain()
        text = "\n".join(app.render(80, 30))
        self.assertIn("Complete list saved.", text)
        self.assertIn("100 unique entries", text)
        self.assertIn("RESEARCH.md", text)
        # WO2 §4.6 closing card: title case + labelled rows, not "DONE"
        self.assertIn("✓ Done", text)
        self.assertIn("files   RESEARCH.md", text)
        self.assertNotIn("DONE", text)

    def test_input_submit_adds_user_and_history(self):
        app = self._app()
        # don't actually start a network run: replace the worker thread starter
        app.start_run = lambda task: app.add_user(task)
        app.feed_bytes(b"hello world\r")
        self.assertEqual(app.input, "")
        self.assertEqual(app.history[-1], "hello world")
        text = "\n".join(app.render(80, 24))
        self.assertIn("hello world", text)

    def test_slash_help_and_tab_completion(self):
        app = self._app()
        app.feed_bytes(b"/he")
        self.assertIn("/help", app._ac_matches())
        app.feed_bytes(b"\t")
        self.assertTrue(app.input.startswith("/help"))
        app.handle_action("ENTER")
        app.drain()
        text = "\n".join(app.render(80, 40))
        self.assertIn("commands", text)
        self.assertIn("/copy", text)

    def test_double_ctrl_c_quits(self):
        app = self._app()
        app.handle_action("CTRL_C")
        self.assertFalse(app.quit)
        app.handle_action("CTRL_C")
        self.assertTrue(app.quit)


class AppMoreTests(AppTests):
    def test_streaming_delta_is_replaced_by_final_assistant_event(self):
        app = self._app()
        app.delta("a0", "content", "Hel")
        app.delta("a0", "content", "lo")
        app.drain()
        self.assertIn("Hello", "\n".join(app.render(80, 24)))
        app.event({"type": "assistant", "agent": "a0", "text": "Hello there."})
        app.drain()
        text = "\n".join(app.render(80, 24))
        self.assertIn("Hello there.", text)
        self.assertEqual(text.count("Hello there."), 1)

    def test_parallel_results_pair_fifo(self):
        app = self._app()
        for cmd in ("one", "two"):
            app.event({"type": "tool_call", "agent": "a1", "tool": "bash",
                       "args": {"cmd": cmd}})
        app.drain()
        for out in ("one-output", "two-output"):
            app.event({"type": "tool_result", "agent": "a1", "tool": "bash",
                       "preview": out, "chars": len(out), "ms": 10})
        app.drain()
        tools = [b for b in app.blocks if b.kind == "tool"]
        self.assertEqual(len(tools), 2)
        self.assertIn("one-output", tools[0].meta.get("brief", ""))
        self.assertIn("two-output", tools[1].meta.get("brief", ""))

    def test_paste_and_verbose_toggle(self):
        app = self._app()
        app.feed_bytes(b"\x1b[200~pasted text\x1b[201~")
        self.assertEqual(app.input, "pasted text")
        self.assertFalse(app._pasting)
        app.feed_bytes(b"\x14")
        self.assertTrue(app.verbose)
        app.feed_bytes(b"\x14")
        self.assertFalse(app.verbose)

    def test_scroll_keys_do_not_replay_history(self):
        app = self._app()
        for i in range(60):
            app.add_notice(f"line {i}")
        app._add_history("old prompt")
        app.render(80, 20)
        self.assertEqual(app.scroll, 0)
        app.feed_bytes(b"\x1b[A")             # Up with an empty composer
        self.assertGreater(app.scroll, 0)
        self.assertEqual(app.input, "")
        up = app.scroll
        app.feed_bytes(b"\x1b[B")             # Down
        self.assertLess(app.scroll, up)
        app.feed_bytes(b"\x1b[F")             # End = live
        self.assertEqual(app.scroll, 0)
        app.feed_bytes(b"\x10")               # Ctrl-P = history
        self.assertEqual(app.input, "old prompt")
        app.feed_bytes(b"\x0e")               # Ctrl-N = next
        self.assertEqual(app.input, "")
        app.feed_bytes(b"\x1b[1;2A")          # Shift+Up = history even when empty
        self.assertEqual(app.input, "old prompt")
        app.input = "draft"
        app.cursor = len(app.input)
        app.feed_bytes(b"\x1b[A")             # Up scrolls even with a draft
        self.assertGreater(app.scroll, 0)
        self.assertEqual(app.input, "draft")
        app.feed_bytes(b"\x1b[F")             # End = live, draft untouched
        self.assertEqual(app.scroll, 0)
        self.assertEqual(app.input, "draft")
        app.input = ""
        app.feed_bytes(b"\x1b[<64;10;10M")    # SGR wheel up
        self.assertGreater(app.scroll, 0)
        app.feed_bytes(b"\x1b[<65;10;10M")    # SGR wheel down
        self.assertEqual(app.scroll, 0)
        app.feed_bytes(b"\x1b[<0;10;5M")      # touch/left press
        app.feed_bytes(b"\x1b[<32;10;15M")    # drag down = older content
        self.assertGreater(app.scroll, 0)
        app.feed_bytes(b"\x1b[<0;10;15m")     # release
        self.assertFalse(app._mouse_pressed)
        app.feed_bytes(b"\x1b[F")
        self.assertEqual(app.scroll, 0)
        app.feed_bytes(b"\x1b[<64;10;")     # split SGR wheel report
        self.assertFalse(app._canceling)
        app.feed_bytes(b"5M")               # completes the sequence
        self.assertGreater(app.scroll, 0)
        app.feed_bytes(b"\x1b[F")
        self.assertEqual(app.scroll, 0)

    def test_new_output_preserves_scrollback(self):
        app = self._app()
        app.run_id = "r_test"
        for i in range(60):
            app.add_notice(f"line {i}")
        app.render(80, 20)
        app.feed_bytes(b"\x1b[A")
        old = app.scroll
        self.assertGreater(old, 0)
        app.add_assistant("new output while the user reads")
        app.render(80, 20)
        self.assertGreaterEqual(app.scroll, old)
        self.assertNotEqual(app.scroll, 0)
        text = "\n".join(app.render(80, 20))
        self.assertIn("End = live", text)

    def test_lead_prompt_keeps_hinglish(self):
        text = (REPO / "hydra/prompts/lead.md").read_text(encoding="utf-8")
        self.assertIn("Hinglish", text)
        self.assertIn("Devanagari", text)

    def test_sidebar_and_narrow_layout(self):
        app = self._app()
        app.event({"type": "run_start", "run": "r_wide", "task": "x",
                   "cwd": self.cwd.name, "ts": time.time()})
        app.drain()
        app.run_id = "r_wide"
        app.todos = ["do the thing"]
        wide = "\n".join(app.render(120, 30))
        self.assertIn("│", wide)
        self.assertIn("RUN", wide)
        self.assertIn("do the thing", wide)
        narrow = app.render(34, 20)
        from hydra import ui_format as U
        for row in narrow:
            self.assertLessEqual(U.display_width(row), 34, row)


class PlainCliTests(Base):
    def test_plain_pipe_shows_prose_once_and_no_raw_args(self):
        self.srv.add("gpt-oss:120b",
                     {"content": "I will check the folder.",
                      "tool_calls": [{"function": {"name": "bash",
                                                   "arguments": {"cmd": "echo plain-ok"}}}]},
                     {"content": "Done checking.",
                      "tool_calls": [{"function": {"name": "finish",
                                                   "arguments": {
                                                       "status": "done",
                                                       "summary": "Plain report summary."}}}]})
        env = dict(os.environ)
        env.update(HYDRA_HOME=self.home.name, PYTHONPATH=str(REPO), TERM="dumb")
        proc = subprocess.run([
            sys.executable, "-m", "hydra", "--plain",
            "--set", f"api.host={self.srv.url}",
            "--set", "loop.verify=never",
            "--set", "termux.wake_lock=false",
            "--set", "termux.notify_on_finish=false",
            "--set", "termux.notify_on_park=false",
            "create a plain smoke"], cwd=self.cwd.name, env=env,
            capture_output=True, text=True, timeout=40)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("I will check the folder.", out)
        self.assertIn("plain-ok", out)
        self.assertIn("Plain report summary.", out)
        self.assertEqual(out.count("Plain report summary."), 1, out)
        self.assertNotIn('"cmd"', out)
        self.assertNotIn("Traceback", out)

    def test_run_reads_a_task_file_and_rejects_a_missing_one(self):
        task_file = Path(self.cwd.name) / "task.md"
        task_file.write_text("run-file smoke task\n", encoding="utf-8")
        self.srv.add("gpt-oss:120b",
                     {"content": "Read the task file.",
                      "tool_calls": [{"function": {"name": "finish",
                                                   "arguments": {
                                                       "status": "done",
                                                       "summary": "Run-file task done."}}}]})
        env = dict(os.environ)
        env.update(HYDRA_HOME=self.home.name, PYTHONPATH=str(REPO), TERM="dumb")
        base = [sys.executable, "-m", "hydra", "--plain",
                "--set", f"api.host={self.srv.url}",
                "--set", "loop.verify=never",
                "--set", "termux.wake_lock=false",
                "--set", "termux.notify_on_finish=false",
                "--set", "termux.notify_on_park=false"]
        proc = subprocess.run(base + ["run", str(task_file)],
                              cwd=self.cwd.name, env=env,
                              capture_output=True, text=True, timeout=40)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out)
        self.assertIn("Run-file task done.", out)
        self.assertIn("run-file smoke task", out)
        missing = subprocess.run(base + ["run", str(Path(self.cwd.name) / "nope.md")],
                                 cwd=self.cwd.name, env=env,
                                 capture_output=True, text=True, timeout=40)
        self.assertEqual(missing.returncode, 2, missing.stdout + missing.stderr)
        self.assertIn("file not found", missing.stdout + missing.stderr)


class RuntimeTuiTests(Base):
    def test_runtime_streams_assistant_text_and_compact_tools(self):
        from hydra.runtime import Runtime
        from hydra.tui import Tui
        self.srv.add("gpt-oss:120b",
                     {"content": "I will write ANSWER.md.",
                      "tool_calls": [{"function": {"name": "write_file",
                                                   "arguments": {"path": "ANSWER.md",
                                                                 "content": "hello\n"}}}]},
                     finish_node("Wrote ANSWER.md."))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rt = Runtime(self.cfg("loop.verify=never"), ui=Tui(), cwd=self.cwd.name)
            code, report = rt.run("write ANSWER.md", name="tui-integration")
            rt.close()
        out = buf.getvalue()
        self.assertEqual(code, 0, out)
        self.assertIn("I will write ANSWER.md.", out)
        self.assertIn("write_file", out)
        self.assertIn("ANSWER.md", out)
        self.assertIn("Wrote ANSWER.md.", out)
        self.assertNotIn('"path"', out)
        self.assertTrue((Path(self.cwd.name) / "ANSWER.md").exists())


class PtySmokeTests(unittest.TestCase):
    def test_bare_hydra_from_another_folder_opens_tui(self):
        try:
            import pty
        except Exception:
            self.skipTest("pty unavailable")
        home = tempfile.TemporaryDirectory()
        env = dict(os.environ)
        env.update(HYDRA_HOME=home.name, PYTHONPATH=str(REPO),
                   TERM="xterm-256color")
        pid, fd = pty.fork()
        if pid == 0:  # child
            try:
                os.chdir(home.name)
                os.execvpe(sys.executable, [
                    sys.executable, "-m", "hydra",
                    "--set", "api.host=http://127.0.0.1:1",
                    "--set", "termux.wake_lock=false",
                    "--set", "termux.notify_on_finish=false",
                ], env)
            except BaseException:
                os._exit(127)
        out = b""
        sent = False
        deadline = time.time() + 15
        try:
            while time.time() < deadline:
                r, _, _ = select.select([fd], [], [], 0.2)
                if r:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    out += chunk
                if not sent and b"HYDRA" in out and b"/help" in out:
                    time.sleep(0.25)
                    os.write(fd, b"\x04")
                    sent = True
                    deadline = time.time() + 8
                if sent and b"\x1b[?1049l" in out:
                    break
        finally:
            try:
                os.write(fd, b"\x04")
            except OSError:
                pass
            try:
                _, status = os.waitpid(pid, 0)
            except Exception:
                status = -1
            try:
                os.close(fd)
            except Exception:
                pass
            home.cleanup()
        self.assertTrue(sent, out[-1000:])
        self.assertIn(b"\x1b[?1049h", out)   # alternate screen
        self.assertIn(b"HYDRA", out)
        self.assertEqual(os.waitstatus_to_exitcode(status), 0)


# ---------------------------------------------------------------- web fetch

def _http_server():
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/404":
                self.send_response(404)
                self.send_header("Content-Type", "text/plain")
                self.end_headers()
                self.wfile.write(b"missing")
                return
            body = (b"<!doctype html><html><head><title>Demo Page</title>"
                    b"<style>.x{color:red}</style></head><body>"
                    b"<script>evil()</script><h1>Hello</h1>"
                    b"<p>World &amp; more</p><ul><li>one</li><li>two</li></ul>"
                    b"</body></html>")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


class WebFetchTests(unittest.TestCase):
    def setUp(self):
        self.httpd = _http_server()
        self.base = f"http://127.0.0.1:{self.httpd.server_address[1]}"

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def test_raw_html_to_text(self):
        from hydra.tools.web import _fetch_raw
        title, text = _fetch_raw(self.base + "/")
        self.assertEqual(title, "Demo Page")
        self.assertIn("Hello", text)
        self.assertIn("World & more", text)
        self.assertIn("- one", text)
        self.assertNotIn("evil()", text)
        self.assertNotIn("color:red", text)

    def test_hosted_failure_falls_back_to_raw(self):
        from hydra.tools import web
        class Client:
            def web_fetch(self, url):
                raise RuntimeError("hosted is down")
        ctx = SimpleNamespace(client=Client(), cfg={})
        out = web._tool_web_fetch({"url": self.base + "/"}, ctx)
        self.assertIn("World & more", out)
        self.assertIn("raw fetch succeeded", out)
        self.assertIn("<untrusted", out)

    def test_404_is_page_not_found_not_model_missing(self):
        from hydra.tools import web
        from hydra.client import ModelMissing
        class Client:
            def web_fetch(self, url):
                raise ModelMissing(404, '{"error": "not found"}')
        ctx = SimpleNamespace(client=Client(), cfg={})
        out = web._tool_web_fetch({"url": self.base + "/404"}, ctx)
        self.assertIn("page not found (HTTP 404)", out)
        self.assertNotIn("ModelMissing", out)


# ---------------------------------------------------------------- verifier

class ResearchVerificationTests(Base):
    def test_missing_research_file_is_rejected_then_written(self):
        from hydra.runtime import Runtime
        no_file = {"tool_calls": [{"function": {
            "name": "finish", "arguments": {
                "status": "done", "summary": "I compiled 100 projects."}}}]}
        wrote = {"content": "Writing the deliverable now.",
                 "tool_calls": [{"function": {
                     "name": "write_file", "arguments": {
                         "path": "RESEARCH.md",
                         "content": "# Projects\n\n1. Ghidra\n2. Radare2\n"}}}]}
        done = {"tool_calls": [{"function": {
            "name": "finish", "arguments": {
                "status": "done", "summary": "Wrote RESEARCH.md with the list.",
                "files_changed": ["RESEARCH.md"],
                "artifacts": ["RESEARCH.md"]}}}]}
        self.srv.add("gpt-oss:120b", no_file, wrote, done)
        self.srv.add("gemma4:31b",
                     {"content": json.dumps({
                         "verdict": "fail",
                         "issues": [{"what": "RESEARCH.md is missing",
                                     "fix": "Write the full list to RESEARCH.md"}]})},
                     {"content": json.dumps({"verdict": "pass", "issues": []})})
        rt = Runtime(self.cfg("loop.verify=auto", "loop.verify_rounds=2",
                               'roles.reviewer.chain=["gemma4:31b"]'),
                     cwd=self.cwd.name)
        code, report = rt.run("deep research 100 reverse engineering projects; "
                              "give me a proper list", name="research-check")
        rt.close()
        self.assertEqual(code, 0, report)
        self.assertEqual(report.get("status"), "done")
        self.assertIn("RESEARCH.md", report.get("files_changed") or [])
        self.assertTrue((Path(self.cwd.name) / "RESEARCH.md").exists())
        self.assertEqual(self.srv.pending("gpt-oss:120b"), 0)
        self.assertEqual(self.srv.pending("gemma4:31b"), 0)
        seen_reject = False
        for req in self.srv.chat_requests():
            for m in (req.get("body") or {}).get("messages") or []:
                if "reviewer REJECTED" in str(m.get("content") or ""):
                    seen_reject = True
        self.assertTrue(seen_reject, "verifier issues were not injected into the lead")


class VerifierTests(unittest.TestCase):
    def _agent(self, tmp, task, report):
        return SimpleNamespace(
            id="a0", task=task, report=report, data={}, workdir=str(tmp),
            messages=[{"role": "tool", "tool_name": "read_file",
                       "content": "some evidence"}],
            verify_rounds_left=1)

    def test_artifact_stats_expose_duplicates(self):
        from hydra.verifier import _artifact_stats
        stats = _artifact_stats("1. Ghidra\n2. Ghidra\n3. Radare2\n")
        self.assertIn("3 numbered items", stats)
        self.assertIn("2 unique", stats)
        self.assertIn("duplicates: Ghidra", stats)

    def test_research_task_is_verified_even_without_files(self):
        from hydra import verifier
        agent = self._agent(tempfile.gettempdir(),
                            "deep research reverse engineering projects, proper list of 100",
                            {"status": "done", "summary": "I compiled a list"})
        self.assertTrue(verifier.should_verify({}, agent))
        child = self._agent(tempfile.gettempdir(), "sleep a while, then report",
                            {"status": "done", "summary": "done"})
        self.assertFalse(verifier.should_verify({}, child))

    def test_artifact_contents_reach_the_reviewer(self):
        from hydra import verifier
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "RESEARCH.md"
            p.write_text("# Reverse engineering\n\n1. Ghidra\n2. Radare2\n",
                         encoding="utf-8")
            agent = self._agent(tmp, "research reverse engineering projects",
                                {"status": "done", "summary": "wrote the list",
                                 "files_changed": ["RESEARCH.md"],
                                 "artifacts": ["RESEARCH.md"]})
            seen = {}

            class Router:
                def chain_for(self, role, need_tools=False):
                    return ["review-model"]

                def chat(self, messages, **kw):
                    seen["prompt"] = messages[0]["content"]
                    return SimpleNamespace(content='{"verdict":"pass","issues":[]}',
                                           model="review-model")

            rt = SimpleNamespace(router=Router(), journal=None)
            out = verifier.check(rt, agent)
            self.assertEqual(out["verdict"], "pass")
            self.assertIn("ARTIFACT CONTENTS", seen["prompt"])
            self.assertIn("1. Ghidra", seen["prompt"])
            self.assertIn("2. Radare2", seen["prompt"])
            self.assertIn("2 numbered items", seen["prompt"])
            self.assertIn("2 unique", seen["prompt"])


if __name__ == "__main__":
    unittest.main()
