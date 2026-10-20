"""WO3 B3/E1: coloured cost footer, run/session totals, steerable composer."""
from __future__ import annotations

import copy
import io
import os
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from hydra import config as C
from hydra import ui_format as U
from hydra.tui_app import App, _card_bits, _cost, _cost_style


def _cfg(tmp: Path):
    return C.load_config(overrides=[
        "api.host=http://127.0.0.1:1",
        "termux.wake_lock=false",
        "termux.notify_on_finish=false",
        "termux.notify_on_park=false",
        "ui.mode=inline",
    ], project_dir=str(tmp))


class CostFooterTests(unittest.TestCase):
    def test_free_is_green_and_paid_is_highlighted(self):
        self.assertEqual(_cost(0), "$0")
        self.assertEqual(_cost(0.0004), "<$0.001")
        self.assertEqual(_cost_style(0), "ok")
        self.assertEqual(_cost_style(0.0004), "accent")
        self.assertEqual(_cost_style(0.002), "accent")
        free = dict(_card_bits({"turns": 2, "duration": 1.5, "cost": 0}, "·"))
        paid = dict(_card_bits({"turns": 2, "duration": 1.5,
                                "cost": 0.0015}, "·"))
        self.assertEqual(free[_cost(0)], "ok")
        self.assertEqual(paid[_cost(0.0015)], "accent")

    def test_cost_command_card_and_exit_agree(self):
        tmp = Path(tempfile.mkdtemp())
        with mock.patch.dict(os.environ, {"HYDRA_HOME": str(tmp / "home")}):
            app = App(_cfg(tmp), cwd=str(tmp))
        app.run_id = "r_test"
        app.cost = 0.0015
        app.session_cost = 0.0015
        app.running = False
        notices = []
        app.add_notice = lambda text, *a, **k: notices.append(text)
        app.submit("/cost")
        text = notices[-1]
        self.assertIn(f"{_cost(0.0015)} this run", text)
        self.assertIn(f"{_cost(0.0015)} session", text)
        card = dict(_card_bits({"cost": 0.0015}, "·"))
        self.assertIn(_cost(0.0015), card)
        out = io.StringIO()
        with redirect_stdout(out):
            app._print_exit()
        self.assertIn(f"run {_cost(0.0015)}", out.getvalue())
        self.assertIn(f"session {_cost(0.0015)}", out.getvalue())


class RunningComposerTests(unittest.TestCase):
    def _app(self, tmp: Path):
        with mock.patch.dict(os.environ, {"HYDRA_HOME": str(tmp / "home")}):
            return App(_cfg(tmp), cwd=str(tmp))

    def test_composer_status_and_footer_at_all_widths(self):
        tmp = Path(tempfile.mkdtemp())
        app = self._app(tmp)
        app.running = True
        app.run_id = "r_test"
        app.run_started = time.time() - 3.2
        app.phase = "thinking"
        app.current_model = "fake:1b"
        app.tokens_in = 1200
        app.tokens_out = 80
        app.cost = 0.0
        app.input = "steer this run"
        app.cursor = len(app.input)
        for width in (40, 54, 72, 120):
            rows, (crow, ccol) = app._compose(width, 24)
            lines = ["".join(t for t, _ in row) for row in rows]
            self.assertTrue(all(U.display_width(line) <= width
                                for line in lines), (width, lines))
            text = "\n".join(lines)
            self.assertIn("steer this run", text)
            self.assertIn("Thinking", text)
            self.assertIn("esc to stop", text)
            self.assertIn("$0", text)
            self.assertNotIn("✗", text)
            top = next(i for i, line in enumerate(lines) if line.startswith("╭"))
            bottom = next(i for i, line in enumerate(lines) if line.startswith("╰"))
            footer = bottom + 1
            self.assertGreater(footer, bottom)
            self.assertGreaterEqual(crow, top)
            self.assertLessEqual(crow, bottom)
            self.assertTrue(lines[footer].startswith(" ctx"), (width, lines[footer]))
            # free money keeps its own green span inside the footer
            self.assertTrue(any(t.strip() == "$0" and st == "ok"
                                for t, st in rows[footer]), (width, rows[footer]))
            self.assertGreaterEqual(ccol, 0)


if __name__ == "__main__":
    unittest.main()
