"""Semantic colour tokens and glyph tiers for the HYDRA terminal UI.

Work Order #2 §4.3–§4.5.  Stdlib only.

Rules encoded here:

* six semantic tokens (``accent``/``muted``/``ok``/``warn``/``err``/``info``);
* depth detection: ``NO_COLOR`` -> none, ``COLORTERM=truecolor|24bit`` ->
  truecolor, ``TERM`` contains ``256`` -> 256-colour, else 16-colour;
* **no painted backgrounds** and no italic/underline/blink/reverse/strike;
* glyph tiers ``safe`` (default), ``rich`` and ``ascii``.

Widget code must ask for a token name, never a raw SGR number, so the
"banned SGR" invariant is enforced in one place.
"""
from __future__ import annotations

import os
import re

BANNED_SGR = {3, 4, 5, 6, 7, 9}

# token -> ((r, g, b), 256-colour index, 16-colour SGR code)
DARK = {
    "accent": ((95, 215, 255), 81, 36),
    "muted": ((138, 138, 138), 245, 90),
    "ok": ((95, 215, 135), 78, 32),
    "warn": ((255, 175, 95), 215, 33),
    "err": ((255, 95, 95), 203, 31),
    "info": ((95, 135, 255), 69, 34),
}
LIGHT = {
    "accent": ((0, 95, 135), 24, 36),
    "muted": ((90, 90, 90), 240, 90),
    "ok": ((0, 135, 0), 28, 32),
    "warn": ((135, 95, 0), 130, 33),
    "err": ((175, 0, 0), 124, 31),
    "info": ((0, 95, 175), 25, 34),
}


def color_depth(env=None) -> str:
    """Return ``none`` | ``16`` | ``256`` | ``truecolor``."""
    env = os.environ if env is None else env
    if str(env.get("NO_COLOR") or "").strip():
        return "none"
    forced = str(env.get("HYDRA_COLOR") or "").strip().lower()
    if forced in ("none", "0", "off", "never", "false"):
        return "none"
    if forced in ("truecolor", "24bit"):
        return "truecolor"
    if forced == "256":
        return "256"
    if forced in ("16", "16color"):
        return "16"
    term = str(env.get("TERM") or "")
    if term == "dumb":
        return "none"
    ct = str(env.get("COLORTERM") or "").lower()
    if "truecolor" in ct or "24bit" in ct:
        return "truecolor"
    if "256" in term:
        return "256"
    if not term:
        return "none"
    return "16"


def fg(name: str, depth: str = "256", light: bool = False) -> str:
    """Return the SGR foreground code for a semantic token ("" when off)."""
    table = LIGHT if light else DARK
    rgb, c256, c16 = table.get(name, DARK["muted"])
    if depth == "none":
        return ""
    if depth == "truecolor":
        return f"\x1b[38;2;{rgb[0]};{rgb[1]};{rgb[2]}m"
    if depth == "256":
        return f"\x1b[38;5;{c256}m"
    return f"\x1b[{c16}m"


# ------------------------------------------------------------------ glyphs

LOGO = [
    "█ █ █ █ █▀▄ █▀▄ ▄▀▄",
    "█▀█ ▀█▀ █ █ ██▀ █▀█",
    "▀ ▀  ▀  ▀▀  ▀ ▀ ▀ ▀",
]
LOGO_W = 19

SAFE = {
    "prompt": "❯", "tool": "●", "ok": "✓", "bad": "✗", "warn": "!",
    "dot": "●", "run": "▸", "spawn": "◆", "conn": "└",
    "tree_tee": "├", "tree_elbow": "└",
    "arrow": "→", "up": "↑", "down": "↓", "bullet": "•",
    "sep": "·", "rule": "─", "vbar": "│", "stream": "▍",
    "box_tl": "╭", "box_tr": "╮", "box_bl": "╰", "box_br": "╯",
    "box_h": "─", "box_v": "│",
    "todo_open": "[ ]", "todo_run": "[~]", "todo_done": "[x]",
    "todo": "[ ]", "done": "[x]",
}
RICH_EXTRA = {"conn": "⎿", "tree_elbow": "└", "stream": "▍"}
ASCII_G = {
    "prompt": ">", "tool": "*", "ok": "+", "bad": "x", "warn": "!",
    "dot": "*", "run": "#", "spawn": "+", "conn": "|_",
    "tree_tee": "+-", "tree_elbow": "\\-",
    "arrow": "->", "up": "^", "down": "v", "bullet": "-",
    "sep": "-", "rule": "-", "vbar": "|", "stream": "_",
    "box_tl": "+", "box_tr": "+", "box_bl": "+", "box_br": "+",
    "box_h": "-", "box_v": "|",
    "todo_open": "[ ]", "todo_run": "[~]", "todo_done": "[x]",
    "todo": "[ ]", "done": "[x]",
}
SPIN_SAFE = ["▖", "▘", "▝", "▗"]
SPIN_RICH = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]
SPIN_ASCII = ["|", "/", "-", "\\"]


def resolve_tier(name, ascii_only: bool = False) -> str:
    n = str(name or "auto").strip().lower()
    if ascii_only or n == "ascii":
        return "ascii"
    if n == "rich":
        return "rich"
    return "safe"          # auto / safe / anything unknown


def tier(name, ascii_only: bool = False) -> dict:
    n = resolve_tier(name, ascii_only)
    if n == "ascii":
        return dict(ASCII_G)
    if n == "rich":
        glyphs = dict(SAFE)
        glyphs.update(RICH_EXTRA)
        return glyphs
    return dict(SAFE)


def spinner_frames(name, ascii_only: bool = False) -> list:
    n = resolve_tier(name, ascii_only)
    if n == "ascii":
        return SPIN_ASCII
    if n == "rich":
        return SPIN_RICH
    return SPIN_SAFE


# ------------------------------------------------------------------ sanitising

_CSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_OSC_RE = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
_SGR_RE = re.compile(r"\x1b\[([0-9;]*)m")


def strip_ansi(text: str) -> str:
    """Remove every escape sequence (snapshots, golden files, pipes)."""
    s = str(text or "")
    s = _OSC_RE.sub("", s)
    s = _CSI_RE.sub("", s)
    return s


def banned_sgr(text: str) -> list:
    """Return the SGR sequences that §4.5 forbids (empty when clean)."""
    bad = []
    for m in _SGR_RE.finditer(str(text or "")):
        params = [p for p in m.group(1).split(";") if p != ""]
        i = 0
        while i < len(params):
            try:
                v = int(params[i])
            except ValueError:
                i += 1
                continue
            if v == 38:
                # extended foreground: 38;5;N or 38;2;r;g;b — skip its params
                nxt = params[i + 1] if i + 1 < len(params) else ""
                if nxt == "5" and i + 2 < len(params):
                    i += 3
                    continue
                if nxt == "2" and i + 4 < len(params):
                    i += 5
                    continue
                i += 1
                continue
            if v == 48 or v in BANNED_SGR or 40 <= v <= 49 or 100 <= v <= 107:
                bad.append(m.group(0))
                break
            i += 1
    return bad
