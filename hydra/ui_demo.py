"""``hydra ui-demo`` — scripted UI scenes through the real renderer.

Work Order #2 §6.  No API call, no credits: every scene is built by feeding
synthetic events into the same ``App`` the interactive TUI uses, then
rendering it at a chosen width/height.

    hydra ui-demo [home|running|done|palette|stream|unicode|glyphs|all]
                  [--width N] [--height N] [--ui inline|fullscreen|plain]
                  [--snapshot] [--no-color] [--ascii]
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from . import config as C
from . import ui_format as U
from . import ui_theme as UT

SCENES = ("home", "running", "done", "palette", "stream", "unicode",
          "glyphs", "all")
DONE_VARIANTS = ("done", "partial", "failed")
RUN_ID = "r_demo_1405"


# ------------------------------------------------------------------ scenes

def _app(cfg, width, height, glyphs="auto", ui_mode="fullscreen",
         welcome=True):
    from .tui_app import App, Theme
    app = App(cfg, cwd=os.getcwd())
    app.width, app.height = int(width), int(height)
    app.ui_mode = ui_mode
    app.theme = Theme(color=False, ascii_only=(str(glyphs) == "ascii"),
                      glyphs=str(glyphs), depth="none")
    app.cwd = Path.home() / "gym_website"
    app.model = "nemotron-3-super"
    app.current_model = "nemotron-3-super"
    app.animations = False           # snapshots show the first spinner frame
    if not welcome:
        app.blocks = []
    else:
        app.blocks = []
        app.add_welcome()
    return app

def _tool(app, name, args, preview, ms, *, chars=None, lines=None):
    app.event({"type": "tool_call", "agent": "a0", "tool": name, "args": args})
    app.event({"type": "tool_result", "agent": "a0", "tool": name,
               "preview": preview, "chars": chars if chars is not None
               else len(preview), "ms": ms, "lines": lines})
    app.drain()


def scene_home(cfg, width=54, height=24, glyphs="auto", ui_mode="fullscreen"):
    return _app(cfg, width, height, glyphs, ui_mode)


def scene_running(cfg, width=54, height=24, glyphs="auto", ui_mode="fullscreen",
                  cost=None):
    app = _app(cfg, width, height, glyphs, ui_mode, welcome=False)
    app.running = True
    app.run_id = RUN_ID
    app.run_started = time.time() - 13.2
    app.phase = "checking"
    app.current_tool = None
    app.turns = 4
    app.tokens_in = 21_400
    app.tokens_out = 2_100
    app.cost = 0.004
    if cost is not None:
        app.cost = float(cost)
    app.last_prompt_tokens = 14_880          # ctx 31% of 48k
    app.add_user("add subtract(a,b) to calc.py and test it")
    _tool(app, "read_file", {"path": "calc.py"},
          "\n".join(f"line {i}" for i in range(1, 15)), 100, lines=14)
    _tool(app, "edit_file",
          {"path": "calc.py",
           "old": "def add(a, b):\n    return a + b\n",
           "new": ("def add(a, b):\n    return a + b\n\n\n"
                   "def subtract(a, b):\n    return a - b\n")},
          "edited calc.py", 120)
    _tool(app, "bash", {"cmd": "python -m unittest -q"},
          "FAIL: test_mul (test_calc.TestCalc)\nFAILED (failures=1)", 1200,
          lines=3)
    _tool(app, "bash", {"cmd": "python -m unittest -q"},
          "Ran 3 tests\n\nOK", 1100, lines=3)
    app.add_notice("failover glm-5.3-flash → gemma4:31b (402)")
    app.add_notice("compacted 8.2k → 2.4k tokens")
    app.event({"type": "agent_spawn", "agent": "a1", "role": "tester",
               "model": "gemma4:31b", "task": "run the tests"})
    app.drain()
    app.phase = "checking"          # status line: "Verifying…"
    app.current_tool = None
    app._thinking = False
    for b in app.blocks:
        if b.kind == "spawn":
            b.meta["elapsed_text"] = "8s"
    return app


def scene_done(cfg, width=54, height=24, glyphs="auto", ui_mode="fullscreen",
               variant="done", cost=None):
    app = _app(cfg, width, height, glyphs, ui_mode, welcome=False)
    app.run_id = RUN_ID
    app.last_prompt_tokens = 14_880
    if variant == "done":
        app.run_started = time.time() - 14
        app.turns = 6
        app.cost = 0.004
        app.tokens_in, app.tokens_out = 21_400, 2_100
        app.add_user("add subtract(a,b) to calc.py and test it")
        _tool(app, "edit_file",
              {"path": "calc.py",
               "old": "def add(a, b):\n    return a + b\n",
               "new": ("def add(a, b):\n    return a + b\n\n\n"
                       "def subtract(a, b):\n    return a - b\n")},
              "edited calc.py", 120)
        _tool(app, "write_file",
              {"path": "test_calc.py",
               "content": "import unittest\n\nfrom calc import add, subtract\n\n\n"
                          "class TestCalc(unittest.TestCase):\n"
                          "    def test_add(self):\n"
                          "        self.assertEqual(add(1, 2), 3)\n"},
              "wrote test_calc.py", 90)
        app.add_assistant("Added `subtract(a, b)` to **calc.py** and ran the tests.")
        app.last_verify = {"verdict": "pass", "model": "gemma4:31b",
                           "detail": "tests ran 3/3"}
        app.last_url = "http://127.0.0.1:8080"
        app.event({"type": "report", "agent": "a0", "report": {
            "status": "done",
            "summary": "Added subtract(a,b) to calc.py and tested it.",
            "files_changed": ["calc.py", "test_calc.py"],
            "artifacts": [], "findings": [], "open_issues": []}})
    elif variant == "partial":
        app.run_started = time.time() - 229
        app.turns = 30
        app.cost = 0.014
        app.tokens_in, app.tokens_out = 614_297, 6_050
        app.add_user("build the 3d butterfly site")
        _tool(app, "edit_file",
              {"path": "js/script.js",
               "old": "const a = 1;\n",
               "new": "const a = 1;\nconst b = 2;\nconst c = 3;\n"},
              "edited js/script.js", 640)
        app.add_notice("turn cap reached (30) — finishing as partial", "warn")
        app.last_verify = {"verdict": "fail", "model": "gemma4:31b",
                           "reason": "no HTTP 200 evidence for the page",
                           "issues": ["no HTTP 200 evidence for the page"]}
        app.event({"type": "report", "agent": "a0", "report": {
            "status": "partial", "summary": "",
            "files_changed": ["js/script.js"],
            "artifacts": [], "findings": [], "open_issues": []}})
    else:
        app.run_started = time.time() - 9
        app.turns = 2
        app.cost = 0.001
        app.tokens_in, app.tokens_out = 800, 120
        app.add_user("summarise the repo")
        app._on_done({"code": 1,
                      "report": {"status": "failed",
                                 "summary": "401 invalid key — run hydra doctor"},
                      "usage": {"in": 800, "out": 120, "est_usd": 0.001},
                      "turns": 2, "agents": 1})
    app.drain()
    app._flash_until = 0            # snapshots stay deterministic
    app._flash_text = ""
    if cost is not None:
        app.cost = float(cost)
        for b in app.blocks:
            if b.kind == "report":
                b.meta["cost"] = app.cost
    return app


def scene_palette(cfg, width=54, height=24, glyphs="auto", ui_mode="fullscreen"):
    app = _app(cfg, width, height, glyphs, ui_mode)
    app.run_id = RUN_ID
    app.last_prompt_tokens = 14_880
    app.cost = 0.004
    app.input = "/m"
    app.cursor = 2
    app.ac_index = 0
    return app


def scene_stream(cfg, width=54, height=24, glyphs="auto", ui_mode="fullscreen"):
    app = _app(cfg, width, height, glyphs, ui_mode)
    app.running = True
    app.run_id = RUN_ID
    app.run_started = time.time() - 4.45
    app.phase = "thinking"
    app.tokens_in = 8_400
    app.tokens_out = 900
    app.last_prompt_tokens = 8_400
    app.add_user("document the fix")
    _tool(app, "read_file", {"path": "calc.py"},
          "def add(a, b):\n    return a + b\n", 90, lines=2)
    _tool(app, "edit_file",
          {"path": "calc.py",
           "old": "def add(a, b):\n    return a + b\n",
           "new": ("def add(a, b):\n    return a + b\n\n\n"
                   "def subtract(a, b):\n    return a - b\n")},
          "edited calc.py", 140)
    _tool(app, "bash", {"cmd": "python -m unittest -q"},
          "FAIL: test_mul (test_calc.TestCalc)\nFAILED (failures=1)", 1200,
          lines=3)
    _tool(app, "bash", {"cmd": "python -m unittest -q"},
          "Ran 3 tests\n\nOK", 1100, lines=3)
    app.add_notice("failover glm-5.3-flash → gemma4:31b (402)")
    app.add_notice("compacted 8.2k → 2.4k tokens")
    app.event({"type": "agent_spawn", "agent": "a1", "role": "reviewer",
               "model": "gemma4:31b", "task": "check the diff"})
    app.add_assistant(
        "# Result\n\nThe failing test is fixed in `calc.py`:\n\n"
        "```python\ndef subtract(a, b):\n    return a - b\n```\n\n"
        "- 3 tests pass\n- no regressions\n", final=True)
    app.delta("a0", "content", "Now writing the final summary")
    app.drain()
    app.phase = "thinking"
    for b in app.blocks:
        if b.kind == "spawn":
            b.meta["elapsed_text"] = "4s"
    return app


def scene_unicode(cfg, width=54, height=24, glyphs="auto", ui_mode="fullscreen"):
    app = _app(cfg, width, height, glyphs, ui_mode)
    app.run_id = RUN_ID
    app.last_prompt_tokens = 14_880
    app.add_user("नमस्ते दुनिया 🙂 — क्या हाल है?")
    app.add_assistant("नमस्ते! हिन्दी ठीक है। 你好世界 🙂 テスト中文字符\n"
                      "क्षत्रिय और प्रणाम — combining marks stay attached.")
    _tool(app, "read_file",
          {"path": "~/projects/very/long/path/with/a/deep/tree/नमस्ते/script.js"},
          "\n".join(f"line {i}" for i in range(1, 6)), 80, lines=5)
    app.drain()
    return app


def build_scene(scene, width=54, height=24, cfg=None, glyphs="auto",
                ui_mode="fullscreen", cost=None):
    """Return ``(rows, cursor)``: spans rows from the real renderer."""
    if cfg is None:
        cfg = C.load_config(overrides=[
            "api.host=http://127.0.0.1:1",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ], project_dir=os.getcwd())
    name = str(scene or "home").lower()
    if name == "done":
        app = scene_done(cfg, width, height, glyphs, ui_mode, "done", cost=cost)
    elif name == "partial":
        app = scene_done(cfg, width, height, glyphs, ui_mode, "partial",
                         cost=cost)
    elif name == "failed":
        app = scene_done(cfg, width, height, glyphs, ui_mode, "failed",
                         cost=cost)
    elif name == "running":
        app = scene_running(cfg, width, height, glyphs, ui_mode, cost=cost)
    elif name == "palette":
        app = scene_palette(cfg, width, height, glyphs, ui_mode)
    elif name == "stream":
        app = scene_stream(cfg, width, height, glyphs, ui_mode)
    elif name == "unicode":
        app = scene_unicode(cfg, width, height, glyphs, ui_mode)
    else:
        app = scene_home(cfg, width, height, glyphs, ui_mode)
    if cost is not None and name not in ("done", "partial", "failed", "running"):
        app.cost = float(cost)
    rows, cursor = app._compose(int(width), int(height))
    return rows, cursor


# ------------------------------------------------------------------ output

def _render_rows(rows, theme: "Theme") -> str:
    out = []
    for spans in rows:
        line = []
        for text, style in spans:
            code = theme.s(style)
            if code:
                line.append(code)
                line.append(text)
                line.append("\x1b[0m")
            else:
                line.append(text)
        out.append("".join(line))
    return "\n".join(out)


def _glyphs(width=80):
    from .tui_app import Theme
    lines = [f"terminal width: {width} columns", ""]
    for name in ("ascii", "safe", "rich"):
        theme = Theme(color=True, glyphs=name, depth=UT.color_depth())
        syms = " ".join(theme.sym(k) for k in
                        ("prompt", "tool", "ok", "bad", "conn", "tree_tee",
                         "tree_elbow", "arrow", "up", "down", "bullet", "sep",
                         "rule", "vbar", "stream", "box_tl", "box_tr",
                         "box_bl", "box_br", "box_h", "box_v"))
        lines.append(f"{name:<6} {syms}")
    lines += ["", "styles: normal | bold | dim | italic | underline",
              "italic sample: \x1b[3mAsk HYDRA to build, fix, debug or research\x1b[0m",
              "normal sample: Ask HYDRA to build, fix, debug or research", "",
              "Kaise padhein:",
              "- 'safe' line ke sab symbols saaf dikhein to wahi default tier hai.",
              "- 'rich' mein koi toota symbol dikhe to rich tier mat use karna.",
              "- italic sample garbled hai to UI mein italic kabhi nahi use hoga."]
    return "\n".join(lines)


def cli(cfg, args, opts=None) -> int:
    scene = "all"
    width = None
    height = None
    ui_mode = None
    glyphs = None
    snapshot = False
    no_color = False
    cost = None
    i = 0
    args = list(args or [])
    while i < len(args):
        a = args[i]
        if a in ("--width",) and i + 1 < len(args):
            i += 1
            width = int(args[i])
        elif a.startswith("--width="):
            width = int(a.split("=", 1)[1])
        elif a in ("--height",) and i + 1 < len(args):
            i += 1
            height = int(args[i])
        elif a.startswith("--height="):
            height = int(a.split("=", 1)[1])
        elif a in ("--cost",) and i + 1 < len(args):
            i += 1
            cost = float(args[i])
        elif a.startswith("--cost="):
            cost = float(a.split("=", 1)[1])
        elif a == "--ui" and i + 1 < len(args):
            i += 1
            ui_mode = args[i]
        elif a.startswith("--ui="):
            ui_mode = a.split("=", 1)[1]
        elif a == "--glyphs":
            scene = "glyphs"          # scenes are instant; tier via --ascii
        elif a == "--snapshot":
            snapshot = True
        elif a == "--no-color":
            no_color = True
        elif a == "--ascii":
            glyphs = "ascii"
        elif a in ("--speed",) and i + 1 < len(args):
            i += 1                     # accepted, scenes are instant
        elif a.startswith("--speed="):
            pass                       # accepted, scenes are instant
        elif a in SCENES:
            scene = a
        else:
            print(f"hydra ui-demo: unknown argument {a!r}\n"
                  f"scenes: {', '.join(SCENES)}")
            return 2
        i += 1

    width = int(width or 54)
    height = int(height or 24)
    glyphs = glyphs or str(C.get(cfg, "ui.glyphs", "auto") or "auto")
    ui_mode = ui_mode or str(C.get(cfg, "ui.mode", "fullscreen") or "fullscreen")
    if ui_mode not in ("inline", "fullscreen", "plain"):
        print("hydra ui-demo: --ui must be inline, fullscreen or plain")
        return 2
    width = max(30, min(width, 200))

    if scene == "glyphs":
        text = _glyphs(width)
        sys.stdout.write(text + "\n")
        return 0

    color = (not no_color and not snapshot and sys.stdout.isatty()
             and os.environ.get("NO_COLOR") is None)
    if getattr(opts, "no_color", False):
        color = False
    depth = UT.color_depth() if color else "none"
    from .tui_app import Theme
    theme = Theme(color=color, ascii_only=glyphs == "ascii", glyphs=glyphs,
                  depth=depth)

    names = (["home", "running", "done", "partial", "failed", "palette",
              "stream", "unicode"] if scene == "all" else [scene])
    chunks = []
    for name in names:
        rows, _cursor = build_scene(name, width, height, cfg, glyphs, ui_mode,
                                    cost=cost)
        if snapshot:
            chunks.append("\n".join("".join(t for t, _ in r) for r in rows))
        else:
            label = name
            chunks.append(f"── {label} " + "─" * max(0, width - len(label) - 4))
            chunks.append(_render_rows(rows, theme))
    out = "\n\n".join(chunks)
    if not snapshot and os.environ.get("NO_COLOR") is not None:
        out = UT.strip_ansi(out)
    try:
        sys.stdout.write(out + "\n")
    except BrokenPipeError:
        return 0
    return 0
