"""WO6 W6-3 — public `hydra commit` and the secret-leak guard."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


class CommitCliTests(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.path = Path(self.repo.name)
        self._old = {k: os.environ.get(k) for k in
                     ("HYDRA_HOME", "WO6_ACTIVE_KEY")}
        os.environ["HYDRA_HOME"] = self.home.name
        self.git("init", "-q")
        self.git("config", "user.email", "t@example.invalid")
        self.git("config", "user.name", "Tester")
        (self.path / "app.txt").write_text("one\n", encoding="utf-8")
        self.git("add", ".")
        self.git("commit", "-qm", "initial")

    def tearDown(self):
        self.repo.cleanup()
        self.home.cleanup()
        for key, val in self._old.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def git(self, *args):
        return subprocess.run(["git", *args], cwd=self.path, text=True,
                              capture_output=True, check=False)

    def hydra(self, *args):
        env = dict(os.environ)
        env.update(HYDRA_HOME=self.home.name, PYTHONPATH=str(REPO),
                   NO_COLOR="1")
        return subprocess.run([sys.executable, "-m", "hydra", *args],
                              cwd=self.path, env=env, text=True,
                              capture_output=True, timeout=60)

    def test_clean_tree_is_a_no_op(self):
        proc = self.hydra("commit", "-m", "nothing")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("nothing to commit (working tree clean)", proc.stdout)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1")

    def test_message_argument_stages_and_commits_with_hash_card(self):
        (self.path / "app.txt").write_text("one\ntwo\n", encoding="utf-8")
        proc = self.hydra("commit", "fix", "bug")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("fix bug", proc.stdout)
        self.assertRegex(proc.stdout, r"\b[0-9a-f]{7,}\b")
        self.assertEqual(self.git("log", "-1", "--pretty=%s").stdout.strip(),
                         "fix bug")
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "2")

    def test_auto_message_uses_latest_report_summary(self):
        rid = "r_1021_9999"
        run_dir = Path(self.home.name) / "runs" / rid
        run_dir.mkdir(parents=True)
        (run_dir / "run.json").write_text(json.dumps({
            "cwd": str(self.path), "status": "done"}), encoding="utf-8")
        (run_dir / "events.jsonl").write_text(json.dumps({
            "type": "report", "agent": "a0",
            "report": {"summary": "add the login form"}}) + "\n",
            encoding="utf-8")
        (self.path / "app.txt").write_text("one\ntwo\n", encoding="utf-8")
        proc = self.hydra("commit")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(self.git("log", "-1", "--pretty=%s").stdout.strip(),
                         "add the login form")

    def test_hydra_and_ignored_files_are_not_staged(self):
        (self.path / ".gitignore").write_text("ignored.txt\n",
                                              encoding="utf-8")
        self.git("add", ".gitignore")
        self.git("commit", "-qm", "ignore")
        (self.path / ".hydra").mkdir()
        (self.path / ".hydra" / "config.toml").write_text("secret = true\n",
                                                           encoding="utf-8")
        (self.path / "ignored.txt").write_text("do not commit\n",
                                               encoding="utf-8")
        (self.path / "app.txt").write_text("one\ntwo\n", encoding="utf-8")
        proc = self.hydra("commit", "-m", "safe files")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        tracked = self.git("ls-files").stdout
        self.assertNotIn(".hydra/", tracked)
        self.assertNotIn("ignored.txt", tracked)
        self.assertIn("app.txt", tracked)

    def test_secret_pattern_blocks_before_staging(self):
        token = "ghp_" + "a" * 36       # constructed; never a real credential
        (self.path / "secret.txt").write_text("safe\n" + token + "\n",
                                              encoding="utf-8")
        proc = self.hydra("commit", "-m", "leak")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("commit blocked: secret pattern github_token", proc.stdout)
        self.assertIn("secret.txt", proc.stdout)
        self.assertNotIn(token, proc.stdout + proc.stderr)
        self.assertEqual(self.git("rev-list", "--count", "HEAD").stdout.strip(), "1")
        status = self.git("status", "--porcelain").stdout
        self.assertIn("?? secret.txt", status)

    def test_active_api_key_blocks_without_printing_it(self):
        key = "wo6-active-" + "z" * 24
        os.environ["WO6_ACTIVE_KEY"] = key
        (self.path / "cfg.txt").write_text("value = \"" + key + "\"\n",
                                           encoding="utf-8")
        proc = self.hydra("--set", "api.key_env=WO6_ACTIVE_KEY",
                          "commit", "-m", "key")
        self.assertNotEqual(proc.returncode, 0)
        self.assertIn("active_api_key", proc.stdout)
        self.assertNotIn(key, proc.stdout + proc.stderr)


class TuiCommitRegistrationTests(unittest.TestCase):
    def test_commit_is_registered_as_an_argument_command(self):
        from hydra import tui_app
        self.assertIn("/commit", {name for name, _ in tui_app.COMMANDS})
        self.assertIn("/commit", tui_app._ARG_COMMANDS)
        self.assertIn("/commit", tui_app.HELP_TEXT)


if __name__ == "__main__":
    unittest.main()
