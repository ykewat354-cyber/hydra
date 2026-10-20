"""WO3.1 P3: scan_roots are reported and shown on the closing card."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from hydra import config as C
from hydra import ui_format as U
from hydra.tui_app import App, render_block


class ScanRootsReportTests(unittest.TestCase):
    @staticmethod
    def _text(rows):
        lines = []
        for row in rows:
            if isinstance(row, list):
                lines.append("".join(t for t, _ in row))
            elif isinstance(row, tuple):
                lines.append(str(row[0]))
            else:
                lines.append(str(row))
        return "\n".join(lines)

    def test_report_card_prints_scanned_roots(self):
        tmp = Path(tempfile.mkdtemp())
        root = tmp / "site"
        root.mkdir()
        cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:1",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ], project_dir=tmp)
        with mock.patch.dict(os.environ, {"HYDRA_HOME": str(tmp / "home")}):
            app = App(cfg, cwd=str(tmp))
        app.run_id = "r_test"
        app._on_report("a0", {
            "status": "done", "summary": "wrote a file",
            "files_changed": ["app.js"], "scan_complete": True,
            "scan_roots": [str(root)], "findings": [], "open_issues": [],
            "artifacts": [],
        })
        rows = render_block(app.blocks[-1], 120, app.theme)
        text = self._text(rows)
        self.assertIn("scanned", text)
        self.assertIn(str(root), text)
        self.assertTrue(all(U.display_width(line) <= 120
                            for line in text.splitlines()))

    def test_null_report_card_says_why(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:1",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ], project_dir=tmp)
        with mock.patch.dict(os.environ, {"HYDRA_HOME": str(tmp / "home")}):
            app = App(cfg, cwd=str(tmp))
        app.run_id = "r_test"
        app._on_report("a0", {
            "status": "done", "summary": "unknown writes",
            "files_changed": None, "scan_complete": False,
            "scan_roots": [str(tmp)], "scan_note":
                "writes outside scanned roots possible",
            "findings": [], "open_issues": [], "artifacts": [],
        })
        rows = render_block(app.blocks[-1], 120, app.theme)
        text = self._text(rows)
        self.assertIn("not tracked", text)
        self.assertIn("outside", text)


if __name__ == "__main__":
    unittest.main()
