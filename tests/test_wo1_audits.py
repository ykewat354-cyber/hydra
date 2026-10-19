"""T03 static audits: stdlib-only imports, no gates, prefix stability.

Run from the repo root:  python -m unittest tests.test_wo1_audits -v
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parents[1]
HYDRA = REPO / "hydra"
TOOLS = REPO / "tools"
ALLOWLIST = Path(__file__).with_name("nogate_allowlist.txt")

# The exact T03(b) scan pattern from the work order.
NOGATE_RE = re.compile(
    r"input\(|getpass|\[y/N\]|\[Y/n\]|are you sure|confirm|approve|"
    r"proceed\?|denylist|blocklist|blacklist|dangerous|forbidden")


def _hydra_files():
    for path in sorted(HYDRA.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            yield path


def _lines(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []


def _scan_hydra() -> list[tuple[str, int, str]]:
    hits = []
    for path in _hydra_files():
        for i, line in enumerate(_lines(path), 1):
            if NOGATE_RE.search(line):
                hits.append((str(path.relative_to(REPO)), i, line))
    return hits


def _allowlist_patterns() -> list[str]:
    patterns = []
    for raw in ALLOWLIST.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()  # "#" starts a comment
        if line:
            patterns.append(line)
    return patterns


class AuditImportsTests(unittest.TestCase):
    def _run(self, *args):
        return subprocess.run(
            [sys.executable, str(TOOLS / "audit_imports.py"), *args],
            cwd=str(REPO), capture_output=True, text=True)

    def test_hydra_is_stdlib_only(self):
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("audit_imports: OK", proc.stdout)

    def test_auditor_flags_third_party_imports(self):
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "pkg"
            pkg.mkdir()
            (pkg / "mod.py").write_text(
                "import os\nfrom . import sibling\nimport requests\n"
                "from flask import Flask\n", encoding="utf-8")
            proc = self._run(tmp)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("third-party import 'requests'", proc.stdout)
        self.assertIn("third-party import 'flask'", proc.stdout)
        self.assertNotIn("third-party import 'os'", proc.stdout)


class NoGatesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.hits = _scan_hydra()

    def test_no_unallowlisted_gate_hits(self):
        patterns = [re.compile(p) for p in _allowlist_patterns()]
        new = [f"{rel}:{lineno}: {line.strip()}"
               for rel, lineno, line in self.hits
               if not any(p.search(line) for p in patterns)]
        self.assertEqual([], new, "new gate-like lines under hydra/:\n"
                                  + "\n".join(new))

    def test_allowlist_has_no_stale_patterns(self):
        for pattern in _allowlist_patterns():
            rx = re.compile(pattern)
            self.assertTrue(
                any(rx.search(line) for _, _, line in self.hits),
                f"allowlist pattern matches nothing any more: {pattern!r}")


class PrefixStabilityTests(unittest.TestCase):
    """Lead request prefix (system prompt + tools JSON) must never vary.

    Only the env block (first user message) may carry cwd/time/env/task text.
    """

    TASK_EN = "create a file containing only: hello world, and tell me what you did"
    TASK_HI = ("ek file banao jisme sirf ye likha ho: namaste duniya, "
               "aur mujhe bata do kya kiya")
    STAMP_A = "2020-01-02 03:04"
    STAMP_B = "2031-12-31 23:59"

    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.cwd_a = tempfile.TemporaryDirectory()
        self.cwd_b = tempfile.TemporaryDirectory()
        self._old_env = {k: os.environ.get(k) for k in ("HYDRA_HOME", "PREFIX")}
        os.environ["HYDRA_HOME"] = self.home.name

    def tearDown(self):
        for key, val in self._old_env.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        self.home.cleanup()
        self.cwd_a.cleanup()
        self.cwd_b.cleanup()

    def _build_lead_request(self, cwd: str, task: str, stamp: str, prefix: str) -> dict:
        """Build one real lead request (no API call): system, tools, env block."""
        from hydra import config as C
        from hydra.runtime import Runtime

        os.environ["PREFIX"] = prefix
        cfg = C.load_config(project_dir=cwd)
        real_strftime = time.strftime

        def fake_strftime(fmt, *args, **kwargs):
            if fmt == "%Y-%m-%d %H:%M":
                return stamp
            return real_strftime(fmt, *args, **kwargs)

        with mock.patch("time.strftime", fake_strftime):
            rt = Runtime(cfg, cwd=cwd, quiet=True)
            try:
                lead = rt.start_run(task)
                msgs = lead.api_messages()
                return {
                    "system": msgs[0]["content"],
                    "env": msgs[1]["content"],
                    "tools": json.dumps(rt.tools.schemas_for(lead),
                                        sort_keys=True, ensure_ascii=False),
                }
            finally:
                rt.close()

    def test_system_prompt_and_tools_are_byte_identical(self):
        a = self._build_lead_request(self.cwd_a.name, self.TASK_EN,
                                     self.STAMP_A, "/usr/local")
        b = self._build_lead_request(self.cwd_b.name, self.TASK_HI,
                                     self.STAMP_B, "/opt/other")

        self.assertEqual(a["system"], b["system"])
        self.assertEqual(a["tools"], b["tools"])
        self.assertIn('"write_file"', a["tools"])
        self.assertGreater(len(a["tools"]), 1000)

        # The dynamic bits belong to the env block only.
        self.assertNotEqual(a["env"], b["env"])
        self.assertIn(self.STAMP_A, a["env"])
        self.assertIn(self.STAMP_B, b["env"])
        self.assertIn(self.cwd_a.name, a["env"])
        self.assertIn(self.cwd_b.name, b["env"])
        self.assertIn("namaste", b["env"])

        # Task language never leaks into the prefix.
        self.assertNotIn("namaste", a["system"])
        self.assertNotIn("namaste", b["system"])
        self.assertNotIn("namaste", b["tools"])

        # The matching rule is one static sentence in the prompt.
        rule = "Reply in the same language and script as the user."
        self.assertIn(rule, a["system"])
        self.assertIn(rule, b["system"])


class LanguageRuleTests(unittest.TestCase):
    def test_language_rule_lives_only_in_prompt_text(self):
        code_hits = [str(p.relative_to(REPO)) for p in _hydra_files()
                     if p.suffix == ".py"
                     and ("Hinglish" in p.read_text(encoding="utf-8")
                          or "Devanagari" in p.read_text(encoding="utf-8"))]
        self.assertEqual([], code_hits,
                         "language logic must stay static in prompts/*.md")
        lead = (HYDRA / "prompts" / "lead.md").read_text(encoding="utf-8")
        self.assertIn("Reply in the same language and script as the user.", lead)


if __name__ == "__main__":
    unittest.main()
