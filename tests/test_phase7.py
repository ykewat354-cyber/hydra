"""Phase 7 acceptance: plugins + polish (§9.7, §11 narrow screens, §13).

Acceptance (§17):
* in one run an agent writes a plugin tool, calls `reload_tools`, then uses it
* narrow terminals (< 60 cols) get shrunk one-line events
"""
from __future__ import annotations

import contextlib
import io
import os
import unittest
from pathlib import Path

from tests.test_phase1 import Base
from tests.test_phase2 import finish_node


class PluginTests(Base):
    def test_agent_writes_plugin_reloads_and_uses_it(self):
        from hydra.runtime import Runtime
        from hydra import journal as J

        plugin_path = Path(self.home.name) / "tools" / "echoer.py"
        plugin_src = (
            '"""A tiny plugin the agent writes to extend itself."""\n'
            "SPEC = {\n"
            '    "name": "echoer",\n'
            '    "description": "Echo text back with a PLUGIN-ECHO prefix.",\n'
            '    "parameters": {"type": "object",\n'
            '                   "properties": {"text": {"type": "string"}},\n'
            '                   "required": ["text"]},\n'
            "}\n"
            "\n"
            "\n"
            "def run(args, ctx):\n"
            '    return "PLUGIN-ECHO " + str(args.get("text", ""))\n'
        )
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {"name": "write_file",
                                                   "arguments": {"path": str(plugin_path),
                                                                 "content": plugin_src}}}]},
                     {"tool_calls": [{"function": {"name": "reload_tools",
                                                   "arguments": {}}}]},
                     {"tool_calls": [{"function": {"name": "echoer",
                                                   "arguments": {"text": "hello-plugin"}}}]},
                     finish_node("plugin written, reloaded and used"))
        rt = Runtime(self.cfg("loop.verify=never"), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("extend yourself with an echoer plugin")
            rid = rt.run_id
            registry_names = [t.name for t in rt.tools.tools.values()]
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        self.assertEqual(report["status"], "done")
        self.assertTrue(plugin_path.exists())
        self.assertIn("echoer", registry_names)
        events = J.read_events(rid)
        results = {e.get("tool"): e for e in events if e.get("type") == "tool_result"}
        self.assertIn("echoer", results)
        self.assertIn("PLUGIN-ECHO hello-plugin", results["echoer"]["preview"])
        reload_ev = results.get("reload_tools")
        self.assertIn("echoer", reload_ev["preview"])


class TuiTests(unittest.TestCase):
    def _render(self, cols, ev):
        old = os.environ.get("COLUMNS")
        os.environ["COLUMNS"] = str(cols)

        def restore():
            if old is None:
                os.environ.pop("COLUMNS", None)
            else:
                os.environ["COLUMNS"] = old

        self.addCleanup(restore)
        from hydra.tui import Tui
        t = Tui()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            t.event(ev)
        restore()
        return t, buf.getvalue().rstrip("\n")

    def test_narrow_mode_shrinks_lines(self):
        ev = {"type": "tool_call", "agent": "a0", "tool": "bash",
              "args": {"cmd": "x" * 300}}
        t, line = self._render(40, ev)
        self.assertTrue(t.narrow)
        self.assertLessEqual(len(line), 40, line)
        t2, line2 = self._render(100, ev)
        self.assertFalse(t2.narrow)
        self.assertGreater(len(line2), 40)

    def test_narrow_mode_keeps_keywords(self):
        t, line = self._render(40, {"type": "tool_result", "agent": "a1",
                                    "tool": "bash", "preview": "y" * 300})
        self.assertTrue(t.narrow)
        self.assertIn("ok bash", line)
        self.assertLessEqual(len(line), 40, line)

    def test_quiet_and_json_modes_silent(self):
        from hydra.tui import Tui
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            Tui(quiet=True).event({"type": "tool_call", "agent": "a0",
                                   "tool": "bash", "args": {}})
        self.assertEqual(buf.getvalue(), "")


if __name__ == "__main__":
    unittest.main()
