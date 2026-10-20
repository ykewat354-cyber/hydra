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
from hydra import ui_demo as D
from hydra import ui_format as U
from hydra import ui_theme as UT
from hydra.tui_app import App, Theme, _card_bits, _cost, _cost_style

GOLDEN = Path(__file__).parent / "ui_golden"


def _cfg(tmp: Path):
    return C.load_config(overrides=[
        "api.host=http://127.0.0.1:1",
        "termux.wake_lock=false",
        "termux.notify_on_finish=false",
        "termux.notify_on_park=false",
        "ui.mode=inline",
    ], project_dir=str(tmp))


class CostFooterTests(unittest.TestCase):
    def test_cost_threshold_colours(self):
        self.assertEqual(_cost(0), "$0")
        self.assertEqual(_cost(0.0004), "<$0.001")
        self.assertEqual(_cost_style(0), "ok")
        self.assertEqual((_cost_style(0.05), _cost_style(0.12),
                          _cost_style(0.60)), ("ok", "warn", "err"))
        free = dict(_card_bits({"turns": 2, "duration": 1.5, "cost": 0}, "·"))
        warn = dict(_card_bits({"turns": 2, "duration": 1.5, "cost": 0.12,
                                "cost_warn": 0.10, "cost_alert": 0.50}, "·"))
        alert = dict(_card_bits({"turns": 2, "duration": 1.5, "cost": 0.60,
                                 "cost_warn": 0.10, "cost_alert": 0.50}, "·"))
        self.assertEqual(free[_cost(0)], "ok")
        self.assertEqual(warn[_cost(0.12)], "warn")
        self.assertEqual(alert[_cost(0.60)], "err")

    def test_config_override_moves_thresholds(self):
        tmp = Path(tempfile.mkdtemp())
        cfg = _cfg(tmp)
        C.set_path(cfg, "ui.cost_warn", 0.01)
        C.set_path(cfg, "ui.cost_alert", 0.05)
        with mock.patch.dict(os.environ, {"HYDRA_HOME": str(tmp / "home")}):
            app = App(cfg, cwd=str(tmp))
        self.assertEqual(_cost_style(0.02, cfg=cfg), "warn")
        self.assertEqual(_cost_style(0.06, cfg=cfg), "err")
        app.running = True
        app.run_id = "r_test"
        app.run_started = time.time() - 1
        app.phase = "thinking"
        app.cost = 0.02
        rows, _ = app._compose(54, 24)
        self.assertTrue(any(t.strip() and st == "warn"
                            for row in rows for t, st in row), rows)

    def test_ui_demo_cost_previews_and_goldens(self):
        cases = ((0.05, "ok", "cost_005_54.txt"),
                 (0.12, "warn", "cost_012_54.txt"),
                 (0.60, "err", "cost_060_54.txt"))
        cfg = _cfg(Path(tempfile.mkdtemp()))
        for cost, style, name in cases:
            with self.subTest(cost=cost):
                rows, _ = D.build_scene("done", 54, 31, cfg, "safe",
                                        "fullscreen", cost=cost)
                text = "\n".join("".join(t for t, _ in r) for r in rows) + "\n"
                path = GOLDEN / name
                self.assertEqual(path.read_text(encoding="utf-8"), text,
                                 f"golden changed: {name}")
                theme = Theme(color=True, glyphs="safe", depth="256")
                sgr = D._render_rows(rows, theme)
                self.assertEqual([], UT.banned_sgr(sgr))
                self.assertEqual(_cost_style(cost), style)
                code = theme.s(style)
                self.assertTrue(code and code in sgr, (name, style))
        buf = io.StringIO()
        from hydra import cli
        with redirect_stdout(buf):
            code = cli.main(["--set", "api.host=http://127.0.0.1:1",
                             "ui-demo", "done", "--cost", "0.12",
                             "--snapshot", "--no-color"])
        self.assertEqual(code, 0)
        self.assertIn("$0.120", buf.getvalue())

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
