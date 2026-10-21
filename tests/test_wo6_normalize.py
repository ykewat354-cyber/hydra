"""WO6 W6-2 — strict OpenAI message normalization."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hydra.agent import normalize_call                    # noqa: E402
from hydra.client import normalize_messages_for_openai    # noqa: E402


class NormalizerTests(unittest.TestCase):
    def test_dict_arguments_become_json_and_ids_match(self):
        messages = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "write_file",
                                          "arguments": {"path": "x", "content": "y"}}}]},
            {"role": "tool", "tool_name": "write_file", "content": "ok"},
        ]
        out = normalize_messages_for_openai(messages)
        assistant = out[1]
        tool = out[2]
        call = assistant["tool_calls"][0]
        self.assertEqual(call["id"], "call_1_0")
        self.assertEqual(call["function"]["arguments"],
                         json.dumps({"path": "x", "content": "y"}))
        self.assertEqual(tool["tool_call_id"], call["id"])
        self.assertNotIn("tool_name", tool)
        # Pure: the input keeps its dict and has no synthesized ids.
        self.assertIsInstance(
            messages[1]["tool_calls"][0]["function"]["arguments"], dict)
        self.assertNotIn("id", messages[1]["tool_calls"][0])

    def test_existing_id_is_preserved_and_mismatch_is_repaired(self):
        messages = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "call_real", "function": {
                 "name": "add", "arguments": "{\"a\":1}"}}]},
            {"role": "tool", "tool_name": "add", "content": "1"},
        ]
        out = normalize_messages_for_openai(messages)
        self.assertEqual(out[0]["tool_calls"][0]["id"], "call_real")
        self.assertEqual(out[1]["tool_call_id"], "call_real")

    def test_normalize_call_keeps_provider_id(self):
        call = normalize_call({"id": "call_x", "function": {
            "name": "add", "arguments": {"a": 1}}})
        self.assertEqual(call["id"], "call_x")
        self.assertEqual(call["function"]["arguments"], {"a": 1})

    def test_user_images_become_vision_parts(self):
        out = normalize_messages_for_openai([
            {"role": "user", "content": "what is this?",
             "images": ["YWJj", "data:image/jpeg;base64,ZA=="]}])
        parts = out[0]["content"]
        self.assertEqual(parts[0], {"type": "text", "text": "what is this?"})
        self.assertEqual(parts[1]["image_url"]["url"],
                         "data:image/png;base64,YWJj")
        self.assertEqual(parts[2]["image_url"]["url"],
                         "data:image/jpeg;base64,ZA==")


if __name__ == "__main__":
    unittest.main()
