"""T13 regression: compaction counts the pinned prefix and enforces a turn gap.

A small ``working_ctx`` is dominated by the system prompt + tool schemas, which
no compaction can remove. The trigger must therefore measure the full prompt
(messages + tools), not just the message tail; and consecutive compactions must
be at least ``loop.compact_min_gap`` turns apart.
"""
from __future__ import annotations

import json
import unittest

from tests.test_phase1 import Base
from tests.test_phase2 import finish_node


class CompactPrefixTests(Base):
    def _rt(self, *extra):
        from hydra.runtime import Runtime
        rt = Runtime(self.cfg(*extra), cwd=self.cwd.name, quiet=True)
        self.addCleanup(rt.close)
        return rt

    def test_prefix_tokens_include_tool_schemas(self):
        rt = self._rt()
        lead = rt.start_run("prefix accounting")
        msgs_only = lead.estimate_tokens()
        full = lead.prompt_tokens(rt)
        pinned = lead.pinned_tokens(rt)
        self.assertGreater(pinned, msgs_only)
        self.assertGreaterEqual(full, pinned)
        tools_chars = len(json.dumps(rt.tools.schemas_for(lead)))
        self.assertGreater(tools_chars, 1000)

    def test_small_working_ctx_compacts_on_full_prompt(self):
        """The baseline bug: 6 short turns reached 5.7k real prompt tokens but
        the message-only estimate stayed ~2k, so compaction never fired."""
        from hydra.runtime import Runtime
        from hydra import journal as J

        for _ in range(6):
            self.srv.add("gpt-oss:120b",
                         {"tool_calls": [{"function": {
                             "name": "bash",
                             "arguments": {"cmd": "seq 1 500"}}}]})
        self.srv.add("gpt-oss:120b", finish_node("done"))
        for _ in range(20):
            self.srv.add("*", {"content": json.dumps({
                "goal": "g", "status": "s", "next": ["n"]})})
        rt = Runtime(self.cfg("loop.working_ctx=9000", "loop.verify=never"),
                     cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("keep going")
            rid = rt.run_id
        finally:
            rt.close()
        self.assertEqual(code, 0, report)
        compacts = [e for e in J.read_events(rid) if e.get("type") == "compact"]
        self.assertTrue(compacts, "pinned prefix must count towards working_ctx")
        ev = compacts[0]
        self.assertGreater(ev.get("pinned_tokens") or 0, 1000)
        self.assertGreater(ev.get("est_before") or 0, 5000)
        self.assertLess(ev.get("est_after") or 0, ev.get("est_before") or 0)

    def test_digest_always_renders_all_sections(self):
        from hydra.agent import _render_digest
        text = _render_digest({"goal": "g", "status": "s"})
        for label in ("GOAL", "STATUS", "DECISIONS", "FILES", "COMMANDS",
                      "FINDINGS", "BLOCKERS", "NEXT"):
            self.assertIn(label + ":", text)
        self.assertIn("- (none)", text)

    def test_min_turn_gap_blocks_back_to_back_compaction(self):
        rt = self._rt("loop.working_ctx=12000")
        lead = rt.start_run("gap")
        lead.turns = 4
        lead.data["last_compact_turn"] = 4
        lead.messages = [{"role": "assistant", "content": "x" * 40000},
                         {"role": "tool", "tool_name": "t", "content": "y"},
                         {"role": "assistant", "content": "z"},
                         {"role": "tool", "tool_name": "t", "content": "w"},
                         {"role": "assistant", "content": "v"},
                         {"role": "tool", "tool_name": "t", "content": "u"}]
        # far over any threshold, but the gap (default 3 turns) forbids it
        self.assertFalse(lead.maybe_compact(rt))
        lead.turns = 7
        self.srv.add("*", {"content": json.dumps({
            "goal": "g", "status": "s", "next": ["n"]})})
        self.assertTrue(lead.maybe_compact(rt))


if __name__ == "__main__":
    unittest.main()
