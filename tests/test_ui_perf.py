"""WO2 M4 — T-UI7 performance: frame budget and the render caches.

The requirement (WO2 §5.8): p95 frame ≤ 15 ms at 54×31 with a 10k-line
transcript, and a 10k-line stream under ~10% CPU. Both are measured here on
whatever machine runs the suite; the numbers are printed with ``-s`` and are
recorded in UI_REPORT.md for the phone run.

No network, no key: the transcript is built in memory.
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from hydra import config as C                     # noqa: E402
import hydra.tui_app as T                         # noqa: E402
from hydra.tui_app import App, Block              # noqa: E402


def _app(width: int = 54, height: int = 31) -> App:
    cfg = C.load_config(overrides=[
        "api.host=http://127.0.0.1:1",
        "termux.wake_lock=false",
        "termux.notify_on_finish=false",
        "termux.notify_on_park=false",
    ], project_dir=str(REPO))
    app = App(cfg, cwd=str(REPO))
    # pin the terminal so the cache key never thrashes (a real terminal keeps
    # one width for the whole session)
    app._term_size = lambda: (width, height)
    app.width, app.height = width, height
    app._write = lambda text: None
    return app


def _big_transcript(app: App, cards: int = 4000, paragraphs: int = 5) -> None:
    """≈10k rendered rows: tool cards + long final answers."""
    app.add_user("build the thing")
    for i in range(cards):
        app.blocks.append(Block("tool", agent="a0", meta={
            "name": "read_file",
            "args": {"path": f"src/file_{i}.py"},
            "summary": f"src/file_{i}.py",
            "ok": True, "brief": "12 lines", "ms": 30,
            "preview": "line\n" * 12}))
    for k in range(paragraphs):
        app.blocks.append(Block("assistant", final=True, text="\n".join(
            f"paragraph line {k}-{i} with some words to wrap a bit"
            for i in range(200))))


class FrameBudgetTests(unittest.TestCase):
    """T-UI7: p95 ≤ 15 ms per inline frame at 54×31 with ~10k rows."""

    def test_frame_time_and_cpu(self):
        app = _app()
        _big_transcript(app)
        app.add_assistant("streaming answer text " * 4, final=False)
        first = time.perf_counter()
        app.draw()                       # cold frame fills the caches
        cold_ms = (time.perf_counter() - first) * 1000
        committed, _live = app._inline_frames(54)
        self.assertGreater(len(committed), 9000)
        times = []
        for i in range(200):
            app._rev += 1
            app.blocks[-1].text += f" more{i}"
            t0 = time.perf_counter()
            app.draw()
            t1 = time.perf_counter()
            times.append((t1 - t0) * 1000)
        times.sort()
        n = len(times)
        p50, p95, p99 = times[n // 2], times[int(n * 0.95)], times[int(n * 0.99)]
        cpu = p95 * 10 / 1000 * 100         # frames are capped at 10 fps
        print(f"\nT-UI7 54x31, {len(committed)} committed rows: cold {cold_ms:.0f} ms, "
              f"p50 {p50:.2f} ms, p95 {p95:.2f} ms, p99 {p99:.2f} ms, "
              f"max {times[-1]:.2f} ms, cpu@p10fps {cpu:.2f}%")
        self.assertLessEqual(p95, 15.0, f"p95 frame {p95:.1f} ms > 15 ms")
        self.assertLessEqual(cpu, 10.0, f"cpu estimate {cpu:.1f}% > 10%")

    def test_committed_rows_render_once(self):
        """Warm frames must not re-render committed blocks."""
        app = _app()
        _big_transcript(app, cards=300, paragraphs=1)
        app.add_assistant("streaming answer", final=False)
        app.draw()                        # cold frame renders everything
        calls = {"n": 0}
        orig = T.render_block

        def counted(*a, **k):
            calls["n"] += 1
            return orig(*a, **k)

        T.render_block = counted
        try:
            for i in range(5):
                app._rev += 1
                app.blocks[-1].text += " x"
                app.draw()
        finally:
            T.render_block = orig
        # one live block per frame; everything committed is cached
        self.assertLessEqual(calls["n"], 5 * 2)


class ConsistencyTests(unittest.TestCase):
    """The cached inline frames must equal the plain fullscreen transcript."""

    def _flat(self, app: App, width: int = 54) -> list:
        rows = []
        for _b, rs in app._block_rows(width):
            rows.extend(rs)
        return rows

    def test_frames_match_transcript_in_every_phase(self):
        app = _app()
        app.add_notice("→ failover to gemma4:31b (402)")
        app.event({"type": "tool_call", "agent": "a0", "tool": "read_file",
                   "args": {"path": "calc.py"}})
        app.event({"type": "tool_result", "agent": "a0", "tool": "read_file",
                   "preview": "print(1)\n" * 14, "chars": 126, "ms": 80,
                   "lines": 14})
        app.drain()
        checks = 0
        for phase in range(4):
            if phase == 1:                       # a streaming answer starts
                app.add_assistant("first line\nsecond line\nthird",
                                  final=False)
            elif phase == 2:                     # stream keeps growing
                app.blocks[-1].text += " and more text that must wrap nicely"
                app._rev += 1
            elif phase == 3:                     # stream finalises
                app.blocks[-1].final = True
                app.blocks[-1].text += "\nfinal line"
                app._rev += 1
            committed, live = app._inline_frames(54)
            self.assertEqual(self._flat(app), committed + live,
                             f"phase {phase} mismatch")
            checks += 1
        self.assertEqual(4, checks)

    def test_width_change_reflows_cleanly(self):
        app = _app()
        _big_transcript(app, cards=50, paragraphs=1)
        app.add_assistant("stream", final=False)
        app.draw()
        app._inline_state = None                 # simulate a fresh width
        app.width, app.height = 40, 24
        app._term_size = lambda: (40, 24)
        app.draw()
        committed, live = app._inline_frames(40)
        self.assertEqual(self._flat(app, 40), committed + live)


if __name__ == "__main__":
    unittest.main()
