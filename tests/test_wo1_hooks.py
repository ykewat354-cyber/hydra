"""T19 regressions: hook observers must add notes but never gate the turn."""
from __future__ import annotations

import shlex
import sys
import time
import unittest
from pathlib import Path

from tests.test_phase1 import Base
from tests.test_phase2 import finish_node


def _cmd(*parts) -> str:
    return " ".join(shlex.quote(str(p)) for p in parts)


class HookObserverTests(Base):
    def _write_hook(self, body: str) -> str:
        p = Path(self.cwd.name) / "hook.py"
        p.write_text(body, encoding="utf-8")
        return _cmd(sys.executable, p)

    @staticmethod
    def _tool_text(reqs, needle):
        last = reqs[-1]["body"]["messages"]
        return [str(m.get("content") or "") for m in last
                if m.get("role") == "tool" and needle in str(m.get("content") or "")]

    def test_post_edit_hook_detects_syntax_error_in_tool_result(self):
        from hydra.runtime import Runtime

        hook = self._write_hook(
            "import ast, json, sys\n"
            "p = json.load(sys.stdin)\n"
            "src = str((p.get('args') or {}).get('content') or '')\n"
            "try:\n"
            "    ast.parse(src)\n"
            "except SyntaxError as e:\n"
            "    print(f'syntax error at line {e.lineno}')\n")
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {
                         "name": "write_file",
                         "arguments": {"path": "broken.py",
                                       "content": "def broken(:\n"}}}]},
                     finish_node("hook checked"))
        rt = Runtime(self.cfg("loop.verify=never", "autoverify.enabled=false",
                              f"hooks.post_edit={hook}"),
                     cwd=self.cwd.name, quiet=True)
        try:
            code, _ = rt.run("write broken.py")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0)
        notes = self._tool_text(self.srv.chat_requests(), "broken.py")
        self.assertTrue(notes)
        self.assertIn("[hook]", notes[0])
        self.assertIn("syntax error at line 1", notes[0])
        from hydra import journal as J
        hooks = [e for e in J.read_events(rid)
                 if e.get("type") == "hook" and e.get("hook") == "post_edit"]
        self.assertTrue(hooks)

    def test_hook_exit_1_does_not_stop_turn(self):
        from hydra.runtime import Runtime

        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {
                         "name": "write_file",
                         "arguments": {"path": "ok.txt", "content": "kept\n"}}}]},
                     finish_node("continued after hook"))
        rt = Runtime(self.cfg("loop.verify=never", "autoverify.enabled=false",
                              "hooks.post_edit=exit 1"),
                     cwd=self.cwd.name, quiet=True)
        try:
            code, _ = rt.run("write ok.txt")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0)
        self.assertEqual((Path(self.cwd.name) / "ok.txt").read_text(), "kept\n")
        notes = self._tool_text(self.srv.chat_requests(), "ok.txt")
        self.assertTrue(notes)
        self.assertNotIn("[hook]", notes[0])
        from hydra import journal as J
        self.assertTrue(any(e.get("type") == "report" for e in J.read_events(rid)))

    def test_hook_timeout_is_cut_off_and_ignored(self):
        from hydra.runtime import Runtime

        hook = self._write_hook(
            "import time\n"
            "time.sleep(10)\n"
            "print('LATE-HOOK-OUTPUT')\n")
        self.srv.add("gpt-oss:120b",
                     {"tool_calls": [{"function": {
                         "name": "write_file",
                         "arguments": {"path": "fast.txt", "content": "x\n"}}}]},
                     finish_node("timeout ignored"))
        rt = Runtime(self.cfg("loop.verify=never", "autoverify.enabled=false",
                              f"hooks.post_edit={hook}", "hooks.timeout=0.5"),
                     cwd=self.cwd.name, quiet=True)
        started = time.time()
        try:
            code, _ = rt.run("write fast.txt")
            rid = rt.run_id
        finally:
            rt.close()
        elapsed = time.time() - started
        self.assertEqual(code, 0)
        self.assertLess(elapsed, 5, "hook timeout was not enforced")
        notes = self._tool_text(self.srv.chat_requests(), "fast.txt")
        self.assertTrue(notes)
        self.assertNotIn("LATE-HOOK-OUTPUT", "\n".join(notes))
        from hydra import journal as J
        self.assertTrue(any(e.get("type") == "report" for e in J.read_events(rid)))


if __name__ == "__main__":
    unittest.main()
