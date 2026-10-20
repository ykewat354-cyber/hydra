"""WO3.1 P4: exact-output guidance and the on-demand skill."""
from __future__ import annotations

import unittest
from pathlib import Path


class ExactOutputTests(unittest.TestCase):
    def test_lead_prompt_has_exact_output_guidance(self):
        from hydra.agent import load_system_prompt
        prompt = load_system_prompt("lead").lower()
        self.assertIn("exact lists", prompt)
        self.assertIn("exact-output", prompt)
        self.assertIn("diff", prompt)

    def test_exact_output_skill_has_three_examples(self):
        from hydra import project
        skills = project.find_skills(Path.cwd())
        self.assertIn("exact-output", skills)
        body = skills["exact-output"]["body"]
        self.assertIn("grep", body)
        self.assertIn("csv", body.lower())
        self.assertIn("json", body.lower())
        self.assertIn("diff", body)
        self.assertGreaterEqual(body.count("\n## "), 3, body[:200])


if __name__ == "__main__":
    unittest.main()
