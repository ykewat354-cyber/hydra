"""WO3 C3: prompted JSON matrix classification + prompt shape."""
from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "json_prompt_matrix", REPO / "tools" / "json_prompt_matrix.py")
JPM = importlib.util.module_from_spec(spec)
spec.loader.exec_module(JPM)


class JsonPromptMatrixTests(unittest.TestCase):
    def test_class_thresholds(self):
        self.assertEqual(JPM._classify(12, 12, 12), "strict")
        self.assertEqual(JPM._classify(10, 12, 12), "tolerant")
        self.assertEqual(JPM._classify(9, 9, 12), "no")
        self.assertEqual(JPM._classify(0, 0, 0), "no")

    def test_prompt_explicitly_asks_for_json_and_has_schema(self):
        schema = {"type": "object", "required": ["ok"]}
        prompt = JPM._prompt("verdict", "judge it", schema)
        self.assertIn("Return ONLY one JSON value", prompt)
        self.assertIn("JSON SCHEMA", prompt)
        self.assertIn('"required"', prompt)
        self.assertIn("Do not add markdown", prompt)


if __name__ == "__main__":
    unittest.main()
