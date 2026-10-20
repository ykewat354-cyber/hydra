"""WO3.1 P6: the footprint table lists every part and sums to the total."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hydra import config as C
from hydra import footprint


class FootprintTests(unittest.TestCase):
    def _measured(self, tmp: Path):
        from hydra.agent import Agent, build_env_block, load_system_prompt
        from hydra.runtime import Runtime
        home = tmp / "home"
        (home / "runs").mkdir(parents=True)
        cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:1",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ], project_dir=tmp)
        with mock.patch.dict(os.environ, {"HYDRA_HOME": str(home)}):
            rt = Runtime(cfg, cwd=str(tmp), quiet=True)
            self.addCleanup(rt.close)
            env = build_env_block(tmp, cfg, role="lead", caps=rt.caps)
            agent = Agent("a0", "lead", "", cfg, workdir=tmp,
                          system_prompt=load_system_prompt("lead"),
                          env_block=env)
            return footprint.measure(agent, rt)

    def test_parts_sum_to_total(self):
        tmp = Path(tempfile.mkdtemp())
        measured = self._measured(tmp)
        parts = measured["parts"]
        self.assertIn("message framing", parts)
        for label in ("system prompt", "env block", "workspace summary",
                      "recent runs", "project instructions", "skills list",
                      "tool schemas"):
            self.assertIn(label, parts)
        self.assertEqual(int(measured["total"]), sum(parts.values()),
                         (measured, footprint.render(measured)))
        self.assertLess(int(measured["total"]), 5000)
        text = footprint.render(measured)
        for label in parts:
            self.assertIn(label, text)
        self.assertIn(str(measured["total"]), text)


if __name__ == "__main__":
    unittest.main()
