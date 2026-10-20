"""HYDRA full-screen terminal UI.

A small, dependency-free TUI inspired by Claude Code / Codex CLI / Hermes:

    ┌ HYDRA ──────────────────────────── r_… · ~/project · $0.004 ┐
    │  ❯ your task                                                 │
    │  ● bash  python test_calc.py                                 │
    │    ✓ ALL-OK · 0.8s                                           │
    │  assistant text streams here…                                │
    ├──────────────────────────────────────────────────────────────┤
    │ ⠹ thinking · gpt-oss:120b · 12k↑ 0.3k↓ · $0.004 · 0:17      │
    │ ❯ type a task or /help                                       │
    └──────────────────────────────────────────────────────────────┘

Design rules: stdlib only, phone-first (works at 40 columns), assistant prose
is always visible, tool calls are collapsed to one line (``v`` expands), and
the composer never blocks: type while the agent runs to steer it.
"""
from __future__ import annotations

import base64
import codecs
import json
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__
from . import config as C
from . import journal as J
from . import ui_format as U
from . import ui_theme as UT

# ------------------------------------------------------------------ theme

class Theme:
    """Semantic token -> SGR code.  Never paints a background, never italic."""

    def __init__(self, color=True, ascii_only=False, glyphs=None, depth=None,
                 light=False):
        self.color = bool(color)
        self.depth = depth or ("256" if self.color else "none")
        self.light = bool(light)
        self.tier_name = UT.resolve_tier(glyphs, ascii_only)
        self.ascii = self.tier_name == "ascii"
        self.glyphs = UT.tier(self.tier_name)
        c = {name: UT.fg(name, self.depth, self.light)
             for name in ("accent", "muted", "ok", "warn", "err", "info")}
        c.update({
            "brand": c["accent"], "bar": c["muted"], "head": c["accent"],
            "user": "", "dim": c["muted"], "tool": c["accent"],
            "spawn": c["accent"], "code": "", "border": c["muted"],
            "status": c["muted"], "input": "", "placeholder": c["muted"],
            "assistant": "", "added": c["ok"], "removed": c["err"],
        })
        self.codes = c

    def s(self, name: str) -> str:
        return self.codes.get(name, "")

    def sym(self, name: str) -> str:
        return self.glyphs.get(name, "?")

    def spinner_frames(self) -> list:
        return UT.spinner_frames(self.tier_name)

    def spinner(self, tick: int) -> str:
        frames = self.spinner_frames()
        return frames[int(tick) % len(frames)]


# ------------------------------------------------------------------ model

@dataclass
class AgentInfo:
    id: str
    role: str = "worker"
    depth: int = 0
    parent: str = ""
    model: str = ""
    tin: int = 0
    tout: int = 0
    turns: int = 0
    status: str = "running"


@dataclass
class Block:
    kind: str
    text: str = ""
    agent: str = ""
    meta: dict = field(default_factory=dict)
    final: bool = True
    ts: float = field(default_factory=time.time)


# ------------------------------------------------------------------ rendering

def _aligned(width: int, left: list, right: list = (), pad_style: str = "") -> list:
    """Compose spans to exactly *width* cells (left + gap + right)."""
    lw = sum(U.display_width(t) for t, _ in left)
    rw = sum(U.display_width(t) for t, _ in right)
    if lw + rw + 1 > width:
        # clip the left part, keep the right edge if it is small
        keep = max(0, width - rw - 1)
        left = _clip_spans(left, keep)
        lw = keep
        if lw + rw > width:
            right = _clip_spans(right, max(0, width - lw))
            rw = sum(U.display_width(t) for t, _ in right)
    gap = max(0, width - lw - rw)
    out = list(left)
    if gap:
        out.append((" " * gap, pad_style))
    out.extend(right)
    return out


def _clip_spans(spans, width: int):
    out, used = [], 0
    for text, style in spans:
        if used >= width:
            break
        piece = U.clip_display(text, width - used)
        if piece:
            out.append((piece, style))
            used += U.display_width(piece)
    return out


def _pad_spans(spans, width: int, style: str = ""):
    spans = _clip_spans(spans, width)
    used = sum(U.display_width(t) for t, _ in spans)
    if used < width:
        spans = list(spans) + [(" " * (width - used), style)]
    return spans


def _report_status_style(status: str) -> str:
    return {"done": "ok", "partial": "warn", "failed": "err",
            "killed": "err", "interrupted": "warn"}.get(str(status), "warn")


def _cost(value) -> str:
    """Money for the new UI: ``$0``, ``<$0.001`` or three decimals."""
    try:
        v = float(value or 0)
    except Exception:
        return "$0"
    if v <= 0:
        return "$0"
    if v < 0.001:
        return "<$0.001"
    return f"${v:.3f}"


def _row_lr(left: str, right: str, width: int, style: str) -> tuple:
    """One row with *right* right-aligned; drop the stat when there is no room."""
    left = str(left or "")
    right = str(right or "")
    width = max(8, int(width))
    lw, rw = U.display_width(left), U.display_width(right)
    if right and lw + rw + 1 > width:
        return (U.clip_display(left, width), style)
    if not right:
        return (U.clip_display(left, width), style)
    return (left + " " * max(1, width - lw - rw) + right, style)


def _diff_rows(old, new, width: int, limit: int = 4):
    """Return ``(added, removed, rows)``: a bounded, coloured line diff."""
    import difflib
    old_lines = str(old or "").splitlines()
    new_lines = str(new or "").splitlines()
    added = removed = 0
    rows = []
    for line in difflib.unified_diff(old_lines, new_lines, lineterm="", n=0):
        if line.startswith(("+++", "---", "@@")):
            continue
        if line.startswith("+"):
            added += 1
            style = "added"
        elif line.startswith("-"):
            removed += 1
            style = "removed"
        else:
            continue
        if len(rows) < limit and line[1:].strip():
            rows.append(("  " + U.clip_display(line[0] + " " + line[1:],
                                               max(4, width - 2)), style))
    return added, removed, rows


def render_block(b: Block, width: int, theme: Theme, verbose: bool = False) -> list:
    """Render one transcript block to a list of ``(text, style)`` rows."""
    width = max(16, int(width))
    k = b.kind
    out: list = []
    sym = theme.sym

    if k == "welcome":
        m = b.meta
        version = str(m.get("version") or __version__)
        model = str(m.get("model") or "?")
        cwd = str(m.get("cwd") or "~")
        logo = str(m.get("logo") or "auto")
        out.append(("", ""))
        show_logo = (logo == "on") or (logo == "auto" and width >= 44
                                       and not theme.ascii)
        if show_logo:
            right = [f"HYDRA {version}", model,
                     f"{m.get('plan', 'free')} plan {sym('sep')} key {m.get('key', '?')}"]
            for i in range(3):
                line = " " + UT.LOGO[i] + "  " + right[i]
                out.append((U.clip_display(line, width),
                            "head" if i == 0 else
                            ("accent" if i == 1 else "dim")))
        else:
            mark = "▌" if not theme.ascii else "|"
            out.append((U.end_ellipsis(
                f" {mark}HYDRA {version} {sym('sep')} {model}",
                width), "head"))
        out.append(("", ""))
        out.append((" " + U.clip_display(cwd, max(4, width - 1)), "dim"))
        if m.get("tips", True):
            out.append(("", ""))
            out.append((U.clip_display(" Try  fix the failing tests", width),
                        "dim"))
            out.append((U.clip_display(
                f"      @file attach {sym('sep')} !cmd shell {sym('sep')} /help",
                width), "dim"))
        return out

    if k in ("user", "steer"):
        prefix = (sym("prompt") + " ") if k == "user" else (sym("arrow") + " ")
        style = "user" if k == "user" else "dim"
        for row in U.wrap_display(b.text, width, prefix, "  "):
            out.append((row, style))
        return out

    if k == "assistant":
        if not b.final and U.json_start(b.text):
            out.append((sym("stream") + " writing structured output…", "dim"))
            out.append(("", ""))
            return out
        rows = U.markdown_rows(b.text, width)
        styles = {"head": "head", "code": "code", "border": "border",
                  "dim": "dim"}
        for row, style in rows:
            out.append((row, styles.get(style or "", "assistant")))
        if not b.final:
            for i in range(len(out) - 1, -1, -1):
                text, style = out[i]
                if text.strip():
                    if U.display_width(text) + 1 <= width:
                        out[i] = (text + sym("stream"), "accent")
                    break
        if not out:
            out.append((sym("stream") + " …", "dim"))
        return out

    if k == "tool":
        m = b.meta
        name = str(m.get("name") or "tool")
        tag = "" if b.agent in ("", "a0") else f"[{b.agent}] "
        head = f"{sym('tool')} {tag}{name}"
        summary = str(m.get("summary") or "")
        ms = m.get("ms")
        elapsed = ""
        if ms is not None:
            try:
                elapsed = U.fmt_elapsed(int(ms) / 1000.0)
            except Exception:
                elapsed = ""
        ok = m.get("ok")
        head_style = "tool" if ok is None else ("ok" if ok else "err")
        args = m.get("args") if isinstance(m.get("args"), dict) else {}
        added = removed = 0
        stat = ""
        if (name in ("edit_file", "apply_patch") and ok
                and (args.get("old") is not None
                     or args.get("new") is not None)):
            added, removed, _unused = _diff_rows(args.get("old"),
                                                 args.get("new"), width, limit=0)
            stat = f"+{added} -{removed}"
        right = f"{stat}  {elapsed}" if stat else elapsed
        room = max(6, width - U.display_width(head) - U.display_width(right) - 2)
        if summary:
            if name in ("read_file", "write_file", "edit_file", "apply_patch",
                        "create_file", "ls", "view_image", "repo_map"):
                head += "  " + U.middle_ellipsis(summary, room)
            else:
                head += "  " + U.end_ellipsis(summary, room)
        out.append(_row_lr(head, right, width, head_style))
        if ok is not None:
            brief = str(m.get("brief") or "")
            conn = "   " + sym("conn") + " "
            if (name in ("edit_file", "apply_patch") and (added or removed)):
                _a, _r, diff = _diff_rows(args.get("old"), args.get("new"),
                                          width, limit=4)
                for i, (row, rstyle) in enumerate(diff):
                    body = row[2:] if row.startswith("  ") else row
                    out.append((conn + body if i == 0 else "     " + body,
                                rstyle))
            elif name == "write_file" and ok and args.get("content") is not None:
                content = str(args.get("content") or "")
                n = content.count("\n") + (1 if content
                                           and not content.endswith("\n") else 0)
                out.append((conn + f"+{n} lines", "dim"))
            elif name in ("bash", "python") and not ok:
                out.append((conn + brief, "err"))
                prev = str(m.get("preview") or "")
                tail = [ln.strip() for ln in prev.splitlines() if ln.strip()][-3:]
                for ln in tail:
                    out.append(("     " + U.clip_display(ln, max(4, width - 5)),
                                "removed"))
            else:
                out.append((conn + brief, "dim" if ok else "err"))
        if verbose:
            args_v = m.get("args")
            if isinstance(args_v, (dict, list)):
                try:
                    args_v = json.dumps(args_v, ensure_ascii=False)
                except Exception:
                    args_v = str(args_v)
            if args_v:
                out.extend((row, "dim") for row in
                           U.wrap_display(str(args_v), width, "  args: ",
                                          "        ")[:6])
            prev = m.get("preview")
            if prev:
                out.extend((row, "code") for row in
                           U.wrap_display(str(prev), width, "  out:  ",
                                          "        ")[:10])
        return out

    if k == "spawn":
        m = b.meta
        aid = b.agent or m.get("id") or "?"
        role = m.get("role") or "worker"
        model = m.get("model") or ""
        state = str(m.get("state") or "running")
        head = f"{sym('tree_tee')} {aid} {role}"
        if model:
            head += f" {sym('sep')} {model}"
        right = str(m.get("elapsed_text") or "")
        if not right and state == "running" and m.get("ts"):
            try:
                right = U.fmt_elapsed(time.time() - float(m["ts"]))
            except Exception:
                right = ""
        if right:
            right = f"{state} {right}"
        elif state != "running":
            right = state
        out.append(_row_lr(head, right, width,
                           "spawn" if state == "running" else "dim"))
        return out

    if k == "subreport":
        m = b.meta
        aid = b.agent or "?"
        role = m.get("role") or "worker"
        status = str(m.get("status") or "done")
        st = _report_status_style(status)
        out.append((U.clip_display(
            f"{sym('tree_tee')} {aid} {role} {sym('sep')} {status}", width), st))
        summary = str(m.get("summary") or "")
        for row in U.wrap_display(summary, width, "  ", "  ")[:4]:
            out.append((row, "dim"))
        return out

    if k in ("notice", "warn", "error"):
        style = {"notice": "dim", "warn": "warn", "error": "err"}[k]
        text = str(b.text or "")
        arrows = re.compile(r"^(failover|retry|resumed|waiting|parked|stall)")
        if k == "notice":
            prefix = (sym("arrow") + " ") if arrows.match(text) else "  "
        else:
            prefix = (sym("warn") + " ") if k == "warn" else (sym("bad") + " ")
        for row in U.wrap_display(text, width, prefix, "  "):
            out.append((row, style))
        return out

    if k == "verify":
        m = b.meta
        verdict = str(m.get("verdict") or "pass")
        model = m.get("model") or "?"
        if verdict == "pass":
            out.append((f"{sym('ok')} verify pass {sym('sep')} {model}", "ok"))
        elif verdict == "skipped":
            out.append((f"{sym('sep')} verify skipped {sym('sep')} "
                        f"{m.get('error') or ''}", "dim"))
        else:
            out.append((f"{sym('bad')} verify fail", "err"))
            reason = str(m.get("reason") or "")
            if reason:
                out.append((f"   {'verify':<8}"
                            + U.end_ellipsis(reason, max(4, width - 11)),
                            "warn"))
        return out

    if k == "report":
        m = b.meta
        status = str(m.get("status") or "done")
        st = _report_status_style(status)
        icon = {"done": sym("ok"), "partial": sym("tool"),
                "failed": sym("bad")}.get(status, sym("tool"))
        title = {"done": "Done", "partial": "Partial",
                 "failed": "Failed"}.get(status, status.title())
        bits = []
        if m.get("turns") is not None:
            bits.append(f"{m['turns']} turns")
        if m.get("duration") is not None:
            bits.append(U.fmt_elapsed(m["duration"]))
        if m.get("cost") is not None:
            bits.append(_cost(m["cost"]))
        out.append(_row_lr(f" {icon} {title}",
                           f" {sym('sep')} ".join(bits), width, st))
        stats = m.get("file_stats") if isinstance(m.get("file_stats"), dict) else {}
        files = m.get("files")
        if files is None:
            out.append(("   files   not tracked", "dim"))
        else:
            pieces = []
            for x in files:
                name = str(x).strip()
                if not name:
                    continue
                a, r = stats.get(name, (0, 0))
                if a or r:
                    name += f" +{a}" + (f" -{r}" if r else "")
                pieces.append(name)
            if pieces:
                text = "  ".join(pieces)
                room = max(10, width - 11)
                if U.display_width(text) > room:
                    text = U.clip_display(text, max(1, room - 1)) + "…"
                out.append(("   files   " + text, "dim"))
            else:
                out.append(("   files   none", "dim"))
        verify = m.get("verify") or {}
        if verify.get("verdict") == "pass":
            detail = str(verify.get("detail") or "").strip()
            out.append(("   verify  pass" + (f" {sym('sep')} {detail}"
                                        if detail else ""),
                        "ok"))
        elif verify.get("verdict") == "fail":
            reason = str(verify.get("reason") or "verifier rejected the finish")
            out.append(("   verify  " + sym("bad") + " "
                        + U.end_ellipsis(reason, max(8, width - 14)), "warn"))
        elif verify.get("verdict") == "skipped":
            out.append(("   verify  skipped", "dim"))
        url = str(m.get("open_url") or "")
        if url and status == "done":
            out.append(("   open    "
                        + U.end_ellipsis(url, max(8, width - 19)) + " (/open)",
                        "info"))
        if status == "partial":
            out.append((f"   next    /resume to continue {sym('sep')} /why",
                        "dim"))
        if status == "failed":
            why = str(m.get("why") or m.get("summary") or "").strip()
            if why:
                out.append(("   why     "
                            + U.end_ellipsis(why, max(8, width - 11)), "err"))
        if m.get("run"):
            out.append((f"   run     {m['run']}", "dim"))
        summary = str(m.get("summary") or "").strip()
        if summary and status != "failed":
            out.append(("", ""))
            for row, rstyle in U.markdown_rows(summary, width):
                out.append((row, {"head": "head", "code": "code",
                                  "border": "border", "dim": "dim"}.get(
                                      rstyle or "", "assistant")))
        findings = m.get("findings") or []
        if findings:
            out.append(("", ""))
            out.append(("findings", "dim"))
            shown = 0
            for f in findings:
                for row in U.wrap_display(U.issue_text(f), width, "  • ", "    "):
                    out.append((row, "assistant"))
                shown += 1
                if shown >= 12:
                    break
            if len(findings) > shown:
                out.append((f"  … {len(findings) - shown} more", "dim"))
        arts = [str(x) for x in (m.get("artifacts") or []) if str(x).strip()]
        if arts:
            text = "  ".join(arts[:4])
            room = max(10, width - 14)
            if U.display_width(text) > room:
                text = U.clip_display(text, max(1, room - 1)) + "…"
            out.append(("   artifacts " + text, "accent"))
            if len(arts) > 4:
                out.append((f"             +{len(arts) - 4} more", "dim"))
        issues = [str(x) for x in (m.get("open_issues") or []) if str(x).strip()]
        if issues:
            out.append(("", ""))
            out.append(("open issues", "warn"))
            for it in issues[:8]:
                for row in U.wrap_display(U.issue_text(it), width, "  • ", "    "):
                    out.append((row, "warn"))
            if len(issues) > 8:
                out.append((f"  … {len(issues) - 8} more", "dim"))
        return out

    if k == "done":
        m = b.meta
        status = str(m.get("status") or "done")
        st = _report_status_style(status)
        icon = {"done": sym("ok"), "partial": sym("tool"),
                "failed": sym("bad")}.get(status, sym("tool"))
        title = {"done": "Done", "partial": "Partial",
                 "failed": "Failed"}.get(status, status.title())
        bits = []
        if m.get("turns") is not None:
            bits.append(f"{m['turns']} turns")
        if m.get("duration") is not None:
            bits.append(U.fmt_elapsed(m["duration"]))
        if m.get("cost") is not None:
            bits.append(_cost(m["cost"]))
        out.append(_row_lr(f" {icon} {title}",
                           f" {sym('sep')} ".join(bits), width, st))
        if status == "failed":
            why = str(m.get("why") or "").strip()
            if why:
                out.append(("   why     "
                            + U.end_ellipsis(why, max(8, width - 11)), "err"))
        if m.get("saved"):
            out.append((f"   run     {m['run'] or m['saved']}", "dim"))
        return out

    if k == "plan":
        out.append(("plan", "head"))
        for line in str(b.text or "").splitlines():
            s = line.strip()
            if not s:
                continue
            low = s.lower()
            if low[:3] in ("[ ]", "[x]", "[~]"):
                mark = s[:3]
                rest = s[3:].strip()
                style = "dim" if low.startswith("[x]") else (
                    "accent" if low.startswith("[~]") else "assistant")
                out.append((f" {mark} "
                            + U.end_ellipsis(rest, max(4, width - 5)), style))
            else:
                for row in U.wrap_display(s, width, "  ", "  "):
                    out.append((row, "dim"))
        out.append(("", ""))
        return out

    if k == "rule":
        out.append((sym("rule") * min(width, 72), "border"))
        return out

    # unknown -> plain wrapped text
    for row in U.wrap_display(b.text, width, "", ""):
        out.append((row, "dim"))
    return out


# ------------------------------------------------------------------ commands

COMMANDS = [
    ("/help", "commands and keys"),
    ("/new", "fresh screen and session"),
    ("/clear", "clear the screen"),
    ("/compact", "compact the context now"),
    ("/copy", "copy the last answer / report"),
    ("/open", "open the last served URL"),
    ("/expand", "expand a tool card: /expand N"),
    ("/collapse", "collapse expanded tool output"),
    ("/why", "why the last run finished that way"),
    ("/plan", "ask the lead for a plan"),
    ("/pause", "pause between turns"),
    ("/unpause", "continue a paused run"),
    ("/resume", "resume an interrupted run"),
    ("/tree", "agent tree and status"),
    ("/agents", "alias for /tree"),
    ("/kill", "kill a sub-agent: /kill a2"),
    ("/model", "pin a model: /model [role=]name"),
    ("/models", "role chains"),
    ("/cost", "tokens and cost"),
    ("/note", "write to the blackboard"),
    ("/undo", "restore a snapshot"),
    ("/runs", "recent runs"),
    ("/doctor", "environment check"),
    ("/init", "explore this repo into HYDRA.md"),
    ("/ui", "show or set the UI mode"),
    ("/glyphs", "which symbols this font can draw"),
    ("/keys", "key bindings"),
    ("/quit", "leave the TUI"),
    ("/verbose", "show full tool output"),
    ("/export", "write the transcript to a file"),
]
_ARG_COMMANDS = {"/kill", "/model", "/note", "/undo", "/resume",
                 "/expand", "/collapse", "/export", "/ui"}

HELP_TEXT = """\
Type a task and press Enter. The agent starts immediately; there are no approval
prompts. While it runs, keep typing to steer it (Enter sends a steer). Ctrl-C
cancels the in-flight call; press it twice within 2s to stop the run.

commands
  /help              this help
  /clear             clear the screen (the run keeps going)
  /copy              copy the last answer or report file to the clipboard
  /plan              ask the lead to write a todo/DAG plan (then follow it)
  /pause /unpause    pause between turns
  /resume [RUN]      resume an interrupted run
  /tree              agent tree with status and tokens
  /kill aN           stop one sub-agent (subtree included)
  /model [role=]m    pin a model first for a role (default lead)
  /models            role chains
  /cost              token ledger + estimate
  /note text         write to the shared blackboard
  /undo [RUN]        restore the workdir from a shadow snapshot
  /runs              recent runs
  /doctor            key / host / models / tmux checks
  /init              explore this repo and write HYDRA.md
  /keys              key bindings
  /quit              leave (an active run is journaled; hydra resume continues)

keys
  Enter send · Tab complete a /command · Up/Down (+ touch) scroll transcript
  Ctrl-P/Ctrl-N history · Shift+Up/Down history · PgUp/PgDn scroll
  End live output when scrolled · mouse wheel also scrolls
  Ctrl-C cancel (twice = stop) · Esc cancel · Ctrl-D quit · Ctrl-O copy
  Ctrl-L clear · Ctrl-T toggle verbose tool output
"""

KEYS_TEXT = """\
Enter       send the task / steer
Tab         complete a slash command
Up / Down   scroll the transcript (touch-scroll sends these too)
Ctrl-P/N    previous / next input history
Shift+Up/Dn previous / next input history
PgUp/PgDn   scroll the transcript
End         jump to live output when scrolled
Ctrl-C      cancel the in-flight call; twice within 2s stops the run
Esc         cancel the in-flight call (same as Ctrl-C)
Ctrl-D      quit when the input is empty
Ctrl-L      clear the screen
Ctrl-O      copy the last answer / report
Ctrl-T      toggle verbose tool output
/           start a command
"""


# ------------------------------------------------------------------ app

class App:
    """State + render + input. ``run()`` owns the real terminal.

    Event methods are thread-safe: the agent run calls ``event``/``info``/
    ``delta`` from worker threads and everything lands in one queue which the
    main loop drains. Rendering is pure over the state, so tests can call
    ``render(width, height)`` without a TTY.
    """

    def __init__(self, cfg: dict, opts=None, *, cwd=None, initial: str = ""):
        self.cfg = cfg
        self.opts = opts
        self.initial = str(initial or "")
        self.cwd = Path(cwd or os.getcwd())
        self.quiet = False
        self.json_out = False
        self.plan = str(C.get(cfg, "api.plan", "free") or "free")
        try:
            self.model = str((C.get(cfg, "roles.lead.chain") or ["?"])[0])
        except Exception:
            self.model = "?"
        self.prices = C.load_prices(cfg)
        self.key_ok = bool(C.load_key(cfg))
        self.host = str(C.get(cfg, "api.host", "") or "")
        self.work_ctx = int(C.get(cfg, "loop.working_ctx", 48000) or 48000)

        self.ui_mode = str(C.get(cfg, "ui.mode", "fullscreen") or "fullscreen")
        if os.environ.get("HYDRA_UI"):
            self.ui_mode = os.environ["HYDRA_UI"]
        self.show_thinking = bool(C.get(cfg, "ui.show_thinking", False))
        self.animations = bool(C.get(cfg, "ui.animations", True))
        self.mouse_opt = bool(C.get(cfg, "ui.mouse", False))
        self._logo_opt = str(C.get(cfg, "ui.logo", "auto") or "auto")
        glyphs = str(C.get(cfg, "ui.glyphs", "auto") or "auto")
        if os.environ.get("HYDRA_ASCII") == "1":
            glyphs = "ascii"
        theme_opt = str(C.get(cfg, "ui.theme", "auto") or "auto").lower()
        depth = UT.color_depth()
        self.theme = Theme(color=depth != "none", ascii_only=glyphs == "ascii",
                           glyphs=glyphs, depth=depth,
                           light=theme_opt == "light")

        self.width, self.height = self._term_size()
        self.blocks: list = []
        self.agents: dict = {}
        self.todos: list = []
        self.files: list = []
        self.q: queue.Queue = queue.Queue()
        self._rev = 0
        self._cache_key = None
        self._cache_lines: list = []
        self._last_total = 0
        self.scroll = 0
        self.verbose = False
        self._force = True
        self._dirty = True

        self.input = ""
        self.cursor = 0
        self.history: list = []
        self.hist_idx = None
        self.ac_index = 0

        self.running = False
        self.phase = "idle"
        self.run_id = None
        self.run_task = ""
        self.run_started = None
        self.done_at = None
        self.current_model = self.model
        self.current_role = "lead"
        self.current_tool = None
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost = 0.0
        self.session_cost = 0.0
        self.turns = 0
        self.agent_count = 0
        self.last_report = None
        self.last_assistant = ""
        self.last_prompt_tokens = 0
        self.last_verify = None
        self.last_url = ""
        self.steer_queued = 0
        self.paste_chip = None
        self._welcome_idx = None
        self._report_seen = False
        self._reported_agents: set = set()
        self._stream_block = None
        self._stream_agent = None
        self._thinking = False
        self._model_started = 0.0

        self.rt = None
        self.thread = None
        self.pending_steers: list = []
        self.quit = False
        self.return_code = 0
        self.sig_stop = False
        self._sig = 0
        self._resize = False
        self._flash_text = ""
        self._flash_until = 0.0
        self._last_ctrl_c = 0.0
        self._last_esc = 0.0
        self._canceling = False
        self._pasting = False
        self._paste_buf = ""
        self._mouse_pressed = False
        self._mouse_last_y = 0
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._keybuf = ""
        self._last_draw = 0.0
        self._last_transcript_height = 10
        self._old_term = None
        self._old_sigs = {}
        self._session_started = time.time()

        self.add_welcome()

    # -------------------------------------------------- terminal size
    @staticmethod
    def _term_size():
        try:
            size = shutil.get_terminal_size((80, 24))
            return max(30, size.columns), max(8, size.lines)
        except Exception:
            return 80, 24

    # -------------------------------------------------- small state helpers
    def _bump(self):
        self._rev += 1
        self._force = True
        self._dirty = True

    def flash(self, text: str, seconds: float = 2.0):
        self._flash_text = str(text)
        self._flash_until = time.monotonic() + float(seconds)
        self._force = True

    def _cwd_short(self) -> str:
        return U.short_path(self.cwd)

    def _key_label(self) -> str:
        if self.key_ok:
            return "✓ loaded"
        if "ollama.com" not in self.host:
            return "local/fake (no key)"
        return "missing"

    def add_welcome(self):
        self.blocks.append(Block("welcome", meta={
            "cwd": self._cwd_short(), "model": self.model,
            "key": self._key_label(), "plan": self.plan,
            "version": __version__, "tips": True, "logo": self._logo_opt}))
        self._welcome_idx = len(self.blocks) - 1
        self._bump()

    def add_notice(self, text: str, kind: str = "notice"):
        text = str(text or "").strip()
        if not text:
            return
        self.blocks.append(Block(kind, text=text))
        self._bump()

    def add_assistant(self, text: str, final: bool = True):
        text = str(text or "").strip("\n")
        if not text:
            return
        self.blocks.append(Block("assistant", text=text, final=final))
        self.last_assistant = text
        self._bump()

    def add_user(self, text: str, steer: bool = False):
        self.blocks.append(Block("steer" if steer else "user", text=str(text)))
        if steer:
            self.steer_queued += 1
        else:
            self.paste_chip = None
            self._hide_tips()
        self.scroll = 0
        self._bump()

    def _hide_tips(self):
        idx = self._welcome_idx
        if idx is not None and 0 <= idx < len(self.blocks):
            try:
                self.blocks[idx].meta["tips"] = False
            except Exception:
                pass

    def _ensure_agent(self, aid: str, **kw) -> AgentInfo:
        aid = str(aid or "a0")
        a = self.agents.get(aid)
        if a is None:
            a = AgentInfo(id=aid)
            self.agents[aid] = a
        for key, val in kw.items():
            if val is not None and hasattr(a, key):
                setattr(a, key, val)
        self.agent_count = len(self.agents)
        return a

    def _add_file(self, path):
        s = U.short_path(path, self.cwd)
        if s and s not in self.files:
            self.files.append(s)
            if len(self.files) > 12:
                self.files = self.files[-12:]

    # -------------------------------------------------- Runtime UI protocol
    def event(self, ev: dict):
        try:
            self.q.put(("event", ev))
        except Exception:
            pass

    def info(self, text):
        try:
            self.q.put(("info", str(text)))
        except Exception:
            pass

    def delta(self, agent, kind, text):
        try:
            self.q.put(("delta", (str(agent or "a0"), str(kind or ""),
                                  str(text or ""))))
        except Exception:
            pass

    # -------------------------------------------------- event queue
    def drain(self) -> int:
        n = 0
        while True:
            try:
                item = self.q.get_nowait()
            except queue.Empty:
                break
            n += 1
            try:
                kind = item[0]
                if kind == "event":
                    self._on_event(item[1])
                elif kind == "info":
                    self.add_notice(item[1])
                elif kind == "delta":
                    self._on_delta(*item[1])
                elif kind == "done":
                    self._on_done(item[1])
                elif kind == "plan":
                    self._on_plan(*item[1])
            except Exception as exc:
                # A UI bug must never take the run down.
                self.blocks.append(Block("warn", text=f"ui: {type(exc).__name__}: {exc}"))
                self._bump()
        return n

    def _on_event(self, ev: dict):
        if not isinstance(ev, dict):
            return
        t = str(ev.get("type") or "")
        aid = ev.get("agent")
        aid = str(aid) if aid else "a0"

        if t == "run_start":
            self.run_id = ev.get("run")
            self.run_started = float(ev.get("ts") or time.time())
            self.done_at = None
            self.run_task = str(ev.get("task") or self.run_task)
            self.tokens_in = self.tokens_out = 0
            self.cost = 0.0
            self.turns = 0
            self.agent_count = 0
            self.agents = {}
            self.todos = []
            self.files = []
            self._report_seen = False
            self._reported_agents = set()
            self._stream_block = None
            self._stream_agent = None
            self.phase = "thinking"
            self._model_started = time.time()
            cwd = U.short_path(ev.get("cwd") or self.cwd)
            self.blocks.append(Block("run", meta={
                "run": self.run_id, "cwd": cwd, "model": self.model}))
            # run blocks are rendered as a compact notice
            self.blocks[-1].kind = "notice"
            self.blocks[-1].text = (
                f"{self.theme.sym('run')} {self.run_id} {self.theme.sym('sep')} "
                f"{cwd} {self.theme.sym('sep')} {self.model}")
            self._flush_pending()
            self._bump()
            return

        if t == "run_resume":
            self.run_id = ev.get("run") or self.run_id
            self.run_started = self.run_started or time.time()
            self.phase = "thinking"
            self.add_notice(f"resumed {self.run_id}")
            return

        if t == "agent_start":
            self._ensure_agent(aid, role=ev.get("role"),
                               depth=ev.get("depth"), parent=ev.get("parent"))
            self._bump()
            return

        if t == "agent_spawn":
            self._ensure_agent(aid, role=ev.get("role"), depth=ev.get("depth"),
                               parent=ev.get("parent"), model=ev.get("model"),
                               status="running")
            self.blocks.append(Block("spawn", agent=aid, meta={
                "id": aid, "role": ev.get("role") or "worker",
                "task": ev.get("task") or "", "model": ev.get("model") or "",
                "state": "running",
                "ts": float(ev.get("ts") or time.time())}))
            self._bump()
            return

        if t == "agent_end":
            a = self._ensure_agent(aid, status=ev.get("status") or "done")
            if aid not in self._reported_agents and aid != "a0":
                self.add_notice(f"{aid} {a.role} ended "
                            f"{self.theme.sym('sep')} {a.status}")
            self._bump()
            return

        if t == "model_request":
            role = str(ev.get("role") or "lead")
            self.current_role = role
            self.current_model = str(ev.get("model") or self.current_model)
            self.phase = "checking" if role in ("reviewer", "verifier", "critic") else "thinking"
            self._thinking = True
            self._model_started = time.time()
            self.current_tool = None
            if int(ev.get("attempt") or 1) > 1:
                self.flash(f"retrying with {self.current_model}", 1.5)
            self._bump()
            return

        if t == "model_response":
            a = self._ensure_agent(aid)
            model = str(ev.get("model") or "")
            if model:
                a.model = model
                self.current_model = model
            tin = int(ev.get("in_tokens") or 0)
            tout = int(ev.get("out_tokens") or 0)
            self.last_prompt_tokens = tin or self.last_prompt_tokens
            a.tin += tin
            a.tout += tout
            a.turns += 1
            self.tokens_in += tin
            self.tokens_out += tout
            self.turns += 1
            price = self.prices.get(model, {}) or {}
            try:
                self.cost += (tin * float(price.get("in", 0))
                              + tout * float(price.get("out", 0))) / 1e6
            except Exception:
                pass
            if self._stream_block is not None:
                try:
                    self.blocks[self._stream_block].final = True
                except Exception:
                    self._stream_block = None
            self.phase = "thinking"
            self._thinking = False
            self._bump()
            return

        if t == "assistant":
            text = str(ev.get("text") or "").strip("\n")
            if not text:
                return
            idx = self._stream_block
            if idx is not None and 0 <= idx < len(self.blocks):
                b = self.blocks[idx]
                if b.kind == "assistant" and b.agent == aid:
                    b.text = text
                    b.final = True
                    self.last_assistant = text
                    self._stream_block = None
                    self._stream_agent = None
                    self._bump()
                    return
            self.add_assistant(text)
            return

        if t == "tool_call":
            if self._stream_block is not None:
                try:
                    self.blocks[self._stream_block].final = True
                except Exception:
                    pass
                self._stream_block = None
                self._stream_agent = None
            name = str(ev.get("tool") or "tool")
            args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
            self.phase = "tool"
            self.current_tool = name
            self._ensure_agent(aid)
            b = Block("tool", agent=aid, meta={
                "name": name, "args": args, "idx": len(self.blocks),
                "summary": U.tool_summary(name, args), "ok": None})
            self.blocks.append(b)
            if name == "todo" and isinstance(args.get("items"), list):
                self.todos = [str(x) for x in args["items"]]
            for key in ("path", "file"):
                if name in ("write_file", "edit_file", "apply_patch", "create_file"):
                    if args.get(key):
                        self._add_file(args[key])
            self._bump()
            return

        if t == "tool_result":
            name = str(ev.get("tool") or "tool")
            target = None
            # results are journaled in call order -> pair FIFO, not LIFO
            for b in self.blocks:
                if (b.kind == "tool" and b.agent == aid and b.meta.get("name") == name
                        and b.meta.get("ok") is None):
                    target = b
                    break
            chars = ev.get("chars")
            ms = ev.get("ms")
            lines = ev.get("lines")
            prev = str(ev.get("preview") or "")
            ok, brief = U.result_brief(name, prev, chars=chars, ms=ms,
                                       lines=lines, width=max(20, self.width - 14))
            # the card shows the elapsed time right-aligned; drop the copy that
            # result_brief appends for the plain renderer
            brief = re.sub(r"\s*·\s*\d+(?:\.\d+)?s\s*$", "", str(brief or "")).strip()
            if target is None:
                target = Block("tool", agent=aid, meta={
                    "name": name, "args": {}, "summary": "",
                    "ok": None})
                self.blocks.append(target)
            target.meta["ok"] = bool(ok)
            target.meta["brief"] = brief
            target.meta["preview"] = prev
            target.meta["chars"] = chars
            target.meta["ms"] = ms
            target.meta["lines"] = lines
            m = re.search(r"https?://(?:127\.0\.0\.1|localhost):\d{2,5}[^\s\"'<>]*",
                          prev)
            if m:
                self.last_url = m.group(0).rstrip(".,);")
            pending = any(b.kind == "tool" and b.agent == aid
                          and b.meta.get("ok") is None for b in self.blocks)
            if not pending:
                self.phase = "thinking"
                self.current_tool = None
            self._bump()
            return

        if t == "todos":
            items = ev.get("items")
            if isinstance(items, list):
                self.todos = [str(x) for x in items]
                self._bump()
            return

        if t == "verify":
            verdict = str(ev.get("verdict") or "pass")
            issues = ev.get("issues") or []
            reason = str(ev.get("reason") or "")
            if not reason and issues:
                try:
                    reason = U.issue_text(issues[0])
                except Exception:
                    reason = str(issues[0])
            self.last_verify = {"verdict": verdict,
                                "model": str(ev.get("model") or ""),
                                "reason": reason, "issues": issues}
            self.blocks.append(Block("verify", agent=aid, meta={
                "verdict": verdict, "model": ev.get("model") or "",
                "issues": issues, "error": ev.get("error") or "",
                "reason": reason}))
            if verdict == "fail":
                self.phase = "thinking"
            self._bump()
            return

        if t == "verify_cap":
            self.add_notice("verification cap reached — finishing as partial", "warn")
            return

        if t == "report":
            rep = ev.get("report") if isinstance(ev.get("report"), dict) else {}
            self._on_report(aid, rep)
            return

        if t == "error":
            self.phase = "error"
            self.add_notice(str(ev.get("error") or "unknown error"), "error")
            return

        if t == "park":
            self.phase = "parked"
            self.add_notice(f"parked: {ev.get('reason') or 'credits exhausted'}", "warn")
            return

        if t == "stall":
            self.add_notice(
                f"stall detected (x{ev.get('repeated')})"
                + (" — switching model" if ev.get("escalate") else ""), "warn")
            return

        if t == "compact":
            self.add_notice(f"context compacted {self.theme.sym('sep')} "
                            f"{ev.get('retired') or 0} messages retired")
            return

        if t == "sigint":
            self.phase = "canceling"
            self.add_notice("cancelled the in-flight work "
                            f"{self.theme.sym('sep')} the run continues", "warn")
            return

        if t in ("snapshot", "hook", "parsed_from_text", "swarm_reattach",
                 "agent_reattach"):
            if self.verbose:
                self.add_notice(f"{t} {U.short(ev, 120)}")
            return

        if t == "user_msg":
            return

        if self.verbose:
            self.add_notice(f"event {t} {U.short(ev, 120)}")

    def _file_stats(self) -> dict:
        """Per-file ``(added, removed)`` from the tool cards of this run."""
        stats: dict = {}
        for b in self.blocks:
            if b.kind != "tool":
                continue
            m = b.meta
            if m.get("ok") is False:
                continue
            args = m.get("args") if isinstance(m.get("args"), dict) else {}
            path = args.get("path") or args.get("file")
            if not path:
                continue
            a, r = stats.get(str(path), (0, 0))
            if m.get("name") == "write_file" and args.get("content") is not None:
                content = str(args.get("content") or "")
                n = content.count("\n") + (1 if content
                                           and not content.endswith("\n") else 0)
                stats[str(path)] = (a + n, r)
            elif (m.get("name") in ("edit_file", "apply_patch")
                    and (args.get("old") is not None
                         or args.get("new") is not None)):
                added, removed, _ = _diff_rows(args.get("old"), args.get("new"),
                                               80, limit=0)
                stats[str(path)] = (a + added, r + removed)
        return stats

    def _on_report(self, aid: str, rep: dict):
        status = str(rep.get("status") or "done")
        summary = str(rep.get("summary") or "")
        if aid in ("", "a0", "None"):
            self.last_report = rep
            self._report_seen = True
            self._reported_agents.add("a0")
            self.phase = status
            for p in (rep.get("files_changed") or []) + (rep.get("artifacts") or []):
                if p:
                    self._add_file(p)
            meta = {
                "status": status, "summary": summary,
                "findings": rep.get("findings") or [],
                "files": rep.get("files_changed"),   # None = not tracked
                "artifacts": rep.get("artifacts") or [],
                "issues": rep.get("open_issues") or [],
                "open_issues": rep.get("open_issues") or [],
                "verify": dict(self.last_verify or {}),
                "open_url": self.last_url or "",
                "file_stats": self._file_stats(),
                "why": (str(rep.get("summary") or "").strip()
                        if status == "failed" else ""),
                "run": self.run_id, "turns": self.turns,
                "agents": self.agent_count, "cost": self.cost,
                "duration": (time.time() - self.run_started
                             if self.run_started else None),
                "saved": str(C.run_dir(self.run_id)) if self.run_id else "",
            }
            self.blocks.append(Block("report", agent=aid, meta=meta))
            self._bump()
        else:
            a = self._ensure_agent(aid, status=status)
            self._reported_agents.add(aid)
            self.blocks.append(Block("subreport", agent=aid, meta={
                "role": a.role, "status": status, "summary": summary}))
            self._bump()

    def _on_delta(self, aid: str, kind: str, text: str):
        if kind == "thinking":
            self._thinking = True
            return
        if kind != "content":
            return
        if self._stream_block is None or self._stream_agent != aid:
            self.blocks.append(Block("assistant", text="", agent=aid, final=False))
            self._stream_block = len(self.blocks) - 1
            self._stream_agent = aid
        idx = self._stream_block
        if idx is None or not (0 <= idx < len(self.blocks)):
            return
        b = self.blocks[idx]
        b.text += text
        if len(b.text) > 400_000:
            b.text = b.text[-400_000:]
        b.final = False
        self._bump()

    def _flush_pending(self):
        if not self.pending_steers:
            return
        lead = None
        if self.rt is not None:
            try:
                lead = self.rt.agents.get("a0")
            except Exception:
                lead = None
        if lead is None:
            return
        n = len(self.pending_steers)
        for text in self.pending_steers:
            try:
                lead.send(text)
            except Exception:
                pass
        self.pending_steers.clear()
        self.flash(f"{n} steer(s) delivered", 1.5)

    def _on_done(self, info: dict):
        self.running = False
        self._canceling = False
        self.done_at = time.time()
        self.phase = "idle"
        usage = info.get("usage") or {}
        self.turns = int(info.get("turns") or self.turns)
        self.agent_count = int(info.get("agents") or self.agent_count)
        if usage:
            self.tokens_in = int(usage.get("in") or self.tokens_in)
            self.tokens_out = int(usage.get("out") or self.tokens_out)
            self.cost = float(usage.get("est_usd") or self.cost)
        self.session_cost += max(0.0, self.cost)
        self.rt = info.get("rt") or self.rt
        report = info.get("report") if isinstance(info.get("report"), dict) else {}
        if not self._report_seen and report:
            self._on_report("a0", report)
        if self._report_seen:
            # refresh the latest report with exact ledger numbers
            for b in reversed(self.blocks):
                if b.kind == "report":
                    b.meta.update({
                        "turns": self.turns, "agents": self.agent_count,
                        "cost": self.cost,
                        "duration": (self.done_at - self.run_started
                                     if self.run_started else None),
                        "saved": str(C.run_dir(self.run_id)) if self.run_id else "",
                    })
                    break
        status = report.get("status") or ("done" if info.get("code") == 0 else "failed")
        if not self._report_seen:
            self.blocks.append(Block("done", meta={
                "status": status, "turns": self.turns,
                "agents": self.agent_count, "tin": self.tokens_in,
                "tout": self.tokens_out, "cost": self.cost,
                "duration": (self.done_at - self.run_started
                             if self.run_started else None),
                "why": str(report.get("summary") or info.get("error") or ""),
                "saved": str(C.run_dir(self.run_id)) if self.run_id else "",
            }))
        if info.get("code") not in (0, None) and status == "failed" and not report:
            self.add_notice("run failed — see the log above", "error")
        self.pending_steers.clear()
        self.steer_queued = 0
        self.current_tool = None
        self._stream_block = None
        self._stream_agent = None
        sep = self.theme.sym("sep")
        self.flash(
            f"{status} {sep} "
            f"{U.fmt_duration(self.done_at - (self.run_started or self.done_at))}"
            + (f" {sep} End = latest" if self.scroll else ""),
            2.0)
        self._bump()

    # -------------------------------------------------- render
    def _transcript_lines(self, width: int) -> list:
        key = (width, self.verbose, self._rev)
        if key == self._cache_key:
            return self._cache_lines
        lines = []
        last_kind = None
        for b in self.blocks:
            rows = render_block(b, width, self.theme, self.verbose)
            if not rows:
                continue
            quiet = (b.kind in ("tool",) and last_kind == "tool") or \
                    (b.kind in ("notice", "warn", "error")
                     and last_kind in ("notice", "warn", "error"))
            if lines and not quiet:
                lines.append(("", ""))
            lines.extend(rows)
            last_kind = b.kind
        total = len(lines)
        if self.scroll > 0 and total != self._last_total:
            # keep the reader's place: new output shifts the window, it never
            # yanks them back to the bottom.
            self.scroll = max(0, self.scroll + (total - self._last_total))
        self._last_total = total
        self._cache_key = key
        self._cache_lines = lines
        return lines

    def _sidebar(self, width: int) -> list:
        rows = []
        rows.append(("AGENTS", "dim"))
        if not self.agents:
            rows.append(("  (none yet)", "dim"))
        else:
            for aid, a in sorted(self.agents.items()):
                mark = self.theme.sym("dot") if a.status == "running" else \
                    (self.theme.sym("ok") if a.status == "done"
                     else (self.theme.sym("bad") if a.status in ("failed", "killed")
                           else self.theme.sym("dot")))
                role = U.short(a.role, 9)
                tokens = U.fmt_tokens(a.tin + a.tout)
                text = f"{mark} {aid} {role}"
                if width >= 20:
                    text = U.pad_display(text, max(10, width - len(tokens) - 1)) + tokens
                rows.append((text, "accent" if a.status == "running" else "dim"))
        if self.todos:
            rows.append(("", ""))
            rows.append(("TODO", "dim"))
            for t in self.todos[:8]:
                s = str(t)
                done = s.strip().lower().startswith("x ")
                txt = s[2:] if done else s
                mark = self.theme.sym("done") if done else self.theme.sym("todo")
                rows.append((mark + " " + U.short(txt, max(4, width - 2)),
                             "dim" if done else ""))
            if len(self.todos) > 8:
                rows.append((f"  +{len(self.todos) - 8} more", "dim"))
        if self.files:
            rows.append(("", ""))
            rows.append(("FILES", "dim"))
            for f in self.files[-7:]:
                rows.append(("  " + U.short(Path(f).name, max(4, width - 2)), "dim"))
        rows.append(("", ""))
        rows.append(("RUN", "dim"))
        if self.run_id:
            rows.append((U.clip_display(str(self.run_id), width), "dim"))
            rows.append((f"{self.turns} turns {self.theme.sym('sep')} "
                         f"{U.fmt_money(self.cost)}", "dim"))
            if self.current_model:
                rows.append((U.short(self.current_model, width), "dim"))
        else:
            rows.append(("(no run yet)", "dim"))
        if self.verbose:
            rows.append(("", ""))
            rows.append(("verbose: on (v)", "warn"))
        return rows

    def _header(self, width: int) -> list:
        left = [(" HYDRA ", "brand"), (__version__, "muted")]
        right_bits = []
        if self.run_id:
            right_bits.append(str(self.run_id))
        right_bits.append(self._cwd_short())
        if width >= 72 and self.current_model:
            right_bits.append(U.short(self.current_model, 20))
        right_text = f" {self.theme.sym('sep')} ".join(right_bits)
        lw = sum(U.display_width(t) for t, _ in left)
        rw = U.display_width(right_text)
        if lw + rw + 3 > width:
            right_text = U.middle_ellipsis(right_text, max(4, width - lw - 3))
            rw = U.display_width(right_text)
        gap = max(1, width - lw - rw - 1)
        return left + [(" " * gap, ""), (right_text + " ", "muted")]

    def _verb(self) -> str:
        phase = self.phase
        if phase == "thinking":
            return "Responding" if self._stream_block is not None else "Thinking"
        if phase == "tool" and self.current_tool:
            verb = {"read_file": "Reading", "ls": "Listing", "glob": "Searching",
                    "grep": "Searching", "edit_file": "Editing",
                    "write_file": "Writing", "bash": "Running",
                    "python": "Running", "web_search": "Searching the web",
                    "web_fetch": "Fetching", "http": "Fetching",
                    "finish": "Finishing"}.get(self.current_tool)
            return verb or f"Running {self.current_tool}"
        if phase == "checking":
            return "Verifying"
        if phase == "canceling":
            return "Stopping"
        if phase == "starting":
            return "Starting"
        if phase == "parked":
            return "Parked"
        return "Working"

    def _palette_active(self) -> bool:
        return (self.input.startswith("/") and " " not in self.input
                and bool(self._ac_matches()))

    def _palette_rows(self, width: int) -> list:
        if not self._palette_active():
            return []
        matches = self._ac_matches()
        descs = {c: d for c, d in COMMANDS}
        rows = []
        for i, c in enumerate(matches[:6]):
            desc = U.end_ellipsis(descs.get(c, ""), max(8, width - 14))
            selected = i == (self.ac_index % len(matches))
            marker = self.theme.sym("prompt") if selected else " "
            style = "accent" if selected else "muted"
            rows.append([(f" {marker} {c:<10.10} {desc}", style)])
        return rows

    def _status(self, width: int) -> list:
        now = time.monotonic()
        sep = self.theme.sym("sep")
        if self._flash_text and now < self._flash_until:
            left = [(self._flash_text, "accent")]
            right_text = ""
        elif self.running:
            spin = self.theme.spinner(int(now * 8) if self.animations else 0)
            elapsed = (U.fmt_elapsed(time.time() - self.run_started)
                       if self.run_started else "")
            left = [(f"{spin} {self._verb()}…", "accent")]
            if elapsed:
                left.append((f"  {elapsed}", "muted"))
            if self.tokens_out or self.tokens_in:
                left.append((f" {sep} ↓ {U.fmt_tokens(self.tokens_out)} tok",
                         "muted"))
            if self.scroll:
                left.append((f" {sep} ↑{self.scroll} more", "muted"))
            right_text = "esc to stop"
        elif self.scroll:
            left = [(f"↑{self.scroll} more", "muted"),
                    (f" {sep} End = live", "accent")]
            right_text = ""
        else:
            return []
        lw = sum(U.display_width(t) for t, _ in left)
        rw = U.display_width(right_text)
        if right_text and lw + rw + 2 <= width:
            gap = max(1, width - lw - rw)
            return [left + [(" " * gap, ""), (right_text, "muted")]]
        if lw > width:
            left = [(U.clip_display("".join(t for t, _ in left), width), left[0][1])]
        return [left]

    def _footer(self, width: int) -> list:
        sep = self.theme.sym("sep")
        ctx = 0
        if self.work_ctx > 0:
            ctx = min(100, int(round(100.0 * self.last_prompt_tokens
                                     / self.work_ctx)))
        cost = (self.cost if (self.running or self.run_id)
                else (self.session_cost or self.cost))
        if self._palette_active():
            left_text = f"↑↓ select {sep} tab complete"
            right_text = f"ctx {ctx}% {sep} {_cost(cost)}"
        elif self.running:
            left_text = f"ctx {ctx}% {sep} {_cost(cost)}"
            right_text = (U.short(self.current_model, max(8, width // 3))
                          if self.current_model else "")
        else:
            left_text = f"/ commands {sep} @ files {sep} ! shell"
            right_text = f"ctx {ctx}% {sep} {_cost(cost)}"
        if self.steer_queued:
            left_text = f"{self.steer_queued} queued {sep} " + left_text
        lw, rw = U.display_width(left_text), U.display_width(right_text)
        if lw + rw + 2 > width:
            return [(" " + U.clip_display(left_text, max(4, width - 1)),
                     "muted")]
        return [(" " + left_text, "muted"), (" " * (width - lw - rw - 1), ""),
                (right_text, "muted")]

    def _input_box(self, width: int, max_rows: int):
        prompt = self.theme.sym("prompt") + " "
        vbar = self.theme.sym("box_v")
        avail = max(1, width - 5)
        text = self.input
        cursor = max(0, min(len(text), int(self.cursor)))
        rows = [""]
        positions = {}
        row = col = 0
        for i, ch in enumerate(text):
            if ch == "\n":
                positions[i] = (row, col)
                row += 1
                col = 0
                rows.append("")
                continue
            w = U.char_width(ch)
            if col + w > avail and col > 0:
                row += 1
                col = 0
                rows.append("")
            positions[i] = (row, col)
            rows[row] += ch
            col += w
        positions[len(text)] = (row, col)
        cr, cc = positions.get(cursor, positions[len(text)])
        max_rows = max(1, int(max_rows))
        offset = max(0, cr - max_rows + 1)
        visible = rows[offset:offset + max_rows] or [""]
        placeholder = ""
        if not text:
            if self.paste_chip:
                placeholder = self.paste_chip
            else:
                placeholder = ("type to steer, enter to send" if self.running
                               else "Ask HYDRA to build, fix, debug or research")
        box = [[(U.clip_display(self.theme.sym("box_tl")
                                + self.theme.sym("box_h") * max(1, width - 2)
                                + self.theme.sym("box_tr"), width), "border")]]
        prefix_w = U.display_width(prompt)
        for i, line in enumerate(visible):
            actual = offset + i
            if not text and i == 0:
                body = U.clip_display(placeholder, avail)
                spans = [(vbar + " ", "border"), (prompt, "accent"),
                         (body, "placeholder")]
                content_w = prefix_w + U.display_width(body)
            else:
                pre = prompt if actual == 0 else "  "
                body = U.clip_display(line, avail)
                spans = [(vbar + " ", "border"), (pre, "accent"), (body, "input")]
                content_w = U.display_width(pre) + U.display_width(body)
            pad = max(0, width - 3 - content_w)
            spans.append((" " * pad + vbar, "border"))
            box.append(spans)
        box.append([(U.clip_display(self.theme.sym("box_bl")
                                    + self.theme.sym("box_h") * max(1, width - 2)
                                    + self.theme.sym("box_br"), width), "border")])
        return box, 1 + (cr - offset), 2 + prefix_w + cc

    def render(self, width: int = None, height: int = None) -> list:
        """Return plain text rows (tests / fallback). Cursor via ``cursor_pos``."""
        rows, _ = self._compose(int(width or self.width), int(height or self.height))
        self.cursor_pos = None
        out = []
        for spans in rows:
            out.append("".join(t for t, _ in spans))
        return out

    def _compose(self, width: int, height: int):
        width = max(30, int(width))
        height = max(6, int(height))
        palette = self._palette_rows(width)
        input_max = min(6, max(1, height // 5))
        box, cur_row, cur_col = self._input_box(width, input_max)
        status_lines = self._status(width)
        footer = self._footer(width)
        chrome = len(palette) + len(status_lines) + len(box) + 1
        transcript_height = max(0, height - chrome)
        side_w = 0
        main_w = width
        if width >= 96 and self.ui_mode != "inline":
            side_w = min(30, max(20, width // 4))
            main_w = width - side_w - 1
        lines = self._transcript_lines(main_w)
        total = len(lines)
        max_scroll = max(0, total - transcript_height)
        self.scroll = max(0, min(self.scroll, max_scroll))
        if self.scroll:
            end = max(0, total - self.scroll)
            start = max(0, end - transcript_height)
        else:
            start = max(0, total - transcript_height)
            end = total
        view = lines[start:end]
        sidebar = self._sidebar(side_w) if side_w else []

        rows: list = []
        for i in range(transcript_height):
            text, style = view[i] if i < len(view) else ("", "")
            spans = [(U.pad_display(text, main_w), style)]
            if side_w:
                stext, sstyle = sidebar[i] if i < len(sidebar) else ("", "")
                spans += [(self.theme.sym("box_v"), "border"),
                          (U.pad_display(stext, side_w), sstyle)]
            rows.append(_pad_spans(spans, width))
        for sp in palette:
            rows.append(_pad_spans(sp, width))
        for sp in status_lines:
            rows.append(_pad_spans(sp, width))
        input_top = len(rows)
        for sp in box:
            rows.append(_pad_spans(sp, width))
        rows.append(_pad_spans(footer, width))
        while len(rows) < height:
            rows.append([("", "")])
        rows = rows[:height]
        self._last_transcript_height = transcript_height
        cursor = None
        cand_r = input_top + cur_row
        cand_c = cur_col
        if 0 <= cand_r < height and 0 <= cand_c <= width:
            cursor = (cand_r, min(cand_c, width - 1))
        return rows, cursor

    # -------------------------------------------------- input
    def _ac_matches(self) -> list:
        if not self.input.startswith("/") or " " in self.input:
            return []
        pre = self.input
        return [c for c, _desc in COMMANDS if c.startswith(pre)]

    def feed_bytes(self, data: bytes):
        if not data:
            return
        try:
            text = self._decoder.decode(data)
        except Exception:
            text = data.decode("utf-8", "replace")
        if not text:
            return
        self._keybuf += text
        self._parse_keys()

    def _handle_mouse(self, code: int, x: int, y: int, final: str):
        """Wheel and drag-to-scroll; taps are ignored (Ctrl-O copies text)."""
        if code & 64:                      # wheel: 64 up, 65 down
            self.handle_action("WHEELDN" if code & 1 else "WHEELUP")
            return
        if code & 32:                      # motion while a button is held
            if self._mouse_pressed:
                dy = int(y) - int(self._mouse_last_y)
                if dy:
                    # dragging down pulls older content into view
                    self._scroll_by(dy)
                    self._mouse_last_y = int(y)
            return
        if final == "m" or (code & 3) == 3:  # release
            self._mouse_pressed = False
            return
        self._mouse_pressed = True
        self._mouse_last_y = int(y)

    def _parse_keys(self):
        buf = self._keybuf
        while buf:
            if self._pasting:
                end = buf.find("\x1b[201~")
                if end < 0:
                    self._paste_buf += buf
                    buf = ""
                    break
                self._paste_buf += buf[:end]
                buf = buf[end + 6:]
                self._insert_text(self._paste_buf)
                self._paste_buf = ""
                self._pasting = False
                continue
            if buf.startswith("\x1b[200~"):
                self._pasting = True
                buf = buf[6:]
                continue
            if buf.startswith("\x1b"):
                # SGR mouse wheel/drag (mouse tracking is enabled in
                # _install_terminal; Ctrl-O still copies the answer)
                m = re.match(r"^\x1b\[<(\d+);(\d+);(\d+)([Mm])", buf)
                if m:
                    code, x, y = (int(m.group(1)), int(m.group(2)),
                                  int(m.group(3)))
                    final = m.group(4)
                    buf = buf[m.end():]
                    self._handle_mouse(code, x, y, final)
                    continue
                if buf.startswith("\x1b[<"):
                    # an SGR mouse report split across reads: wait, never treat
                    # the ESC as "cancel the run".
                    if len(buf) < 64:
                        break
                    buf = buf[1:]
                    continue
                # SS3 / CSI sequences
                m = re.match(r"^\x1b\[([0-9;]*)([A-Za-z~])", buf)
                if m:
                    seq = m.group(1)
                    final = m.group(2)
                    key = None
                    if final == "A":
                        key = "HISTUP" if ";" in seq else "UP"
                    elif final == "B":
                        key = "HISTDN" if ";" in seq else "DOWN"
                    elif final == "C":
                        key = "RIGHT"
                    elif final == "D":
                        key = "LEFT"
                    elif final == "H" or seq == "1":
                        key = "HOME"
                    elif final == "F" or seq == "4":
                        key = "END"
                    elif final == "~" and seq == "3":
                        key = "DELETE"
                    elif final == "~" and seq == "5":
                        key = "PGUP"
                    elif final == "~" and seq == "6":
                        key = "PGDN"
                    buf = buf[m.end():]
                    if key:
                        self.handle_action(key)
                    continue
                m = re.match(r"^\x1bO([A-Za-z])", buf)
                if m:
                    key = {"A": "UP", "B": "DOWN", "C": "RIGHT",
                           "D": "LEFT", "H": "HOME", "F": "END"}.get(m.group(1))
                    buf = buf[m.end():]
                    if key:
                        self.handle_action(key)
                    continue
                # a sequence split across reads: wait for the rest
                if (buf.startswith("\x1b[") or buf.startswith("\x1bO")) and len(buf) < 8:
                    break
                # lone ESC or an unknown Alt+key
                buf = buf[1:]
                self.handle_action("ESC")
                continue
            # take a run of plain characters
            nxt = buf.find("\x1b")
            chunk = buf if nxt < 0 else buf[:nxt]
            buf = "" if nxt < 0 else buf[nxt:]
            for ch in chunk:
                if ch in ("\r", "\n"):
                    self.handle_action("ENTER")
                elif ch in ("\x7f", "\x08"):
                    self.handle_action("BACKSPACE")
                elif ch == "\t":
                    self.handle_action("TAB")
                elif ch == "\x03":
                    self.handle_action("CTRL_C")
                elif ch == "\x04":
                    self.handle_action("CTRL_D")
                elif ch == "\x0c":
                    self.handle_action("CTRL_L")
                elif ch == "\x0f":
                    self.handle_action("CTRL_O")
                elif ch == "\x10":
                    self.handle_action("PREV")
                elif ch == "\x0e":
                    self.handle_action("NEXT")
                elif ch == "\x14":
                    self.handle_action("CTRL_T")
                elif ch == "\x01":
                    self.handle_action("HOME")
                elif ch == "\x05":
                    self.handle_action("END")
                elif ch == "\x0b":
                    self.handle_action("CTRL_K")
                elif ch == "\x15":
                    self.handle_action("CTRL_U")
                elif ch == "\x17":
                    self.handle_action("CTRL_W")
                elif ord(ch) < 32:
                    continue
                else:
                    self._insert_text(ch)
        self._keybuf = buf

    def _insert_text(self, text: str):
        if not text:
            return
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if len(self.input) + len(text) > 300_000:
            text = text[:max(0, 300_000 - len(self.input))]
        self.input = self.input[:self.cursor] + text + self.input[self.cursor:]
        self.cursor += len(text)
        self.hist_idx = None
        self.ac_index = 0
        self._force = True
        self._dirty = True

    def _delete_back(self):
        if self.cursor <= 0:
            return
        self.input = self.input[:self.cursor - 1] + self.input[self.cursor:]
        self.cursor -= 1
        self.hist_idx = None
        self._force = True

    def _delete_forward(self):
        if self.cursor >= len(self.input):
            return
        self.input = self.input[:self.cursor] + self.input[self.cursor + 1:]
        self.hist_idx = None
        self._force = True

    def _history(self, delta: int):
        if not self.history:
            return
        if self.hist_idx is None:
            self.hist_idx = len(self.history)
        self.hist_idx = max(0, min(len(self.history), self.hist_idx + delta))
        if self.hist_idx == len(self.history):
            self.input = ""
        else:
            self.input = self.history[self.hist_idx]
        self.cursor = len(self.input)
        self.ac_index = 0
        self._force = True

    def _complete(self):
        matches = self._ac_matches()
        if not matches:
            return
        pick = matches[self.ac_index % len(matches)]
        self.input = pick + (" " if pick in _ARG_COMMANDS else "")
        self.cursor = len(self.input)
        self.hist_idx = None
        self._force = True

    def _scroll_by(self, delta: int):
        """Scroll the transcript; ``scroll == 0`` means live (follow bottom)."""
        total = max(0, int(getattr(self, "_last_total", 0)))
        height = max(1, int(getattr(self, "_last_transcript_height", 1)))
        max_scroll = max(0, total - height)
        old = self.scroll
        self.scroll = max(0, min(max_scroll, self.scroll + int(delta)))
        if old == 0 and self.scroll > 0:
            self.flash("scrollback — End = live output", 1.4)
        elif old > 0 and self.scroll == 0:
            self.flash("following live output", 1.0)
        self._force = True
        self._dirty = True

    def handle_action(self, action: str):
        if action == "ENTER":
            self.submit_input()
        elif action == "BACKSPACE":
            self._delete_back()
        elif action == "DELETE":
            self._delete_forward()
        elif action == "LEFT":
            self.cursor = max(0, self.cursor - 1)
            self._force = True
        elif action == "RIGHT":
            self.cursor = min(len(self.input), self.cursor + 1)
            self._force = True
        elif action == "HOME":
            self.cursor = 0
            self._force = True
        elif action == "END":
            if self.scroll:
                # reading scrollback? End returns to the live bottom instead of
                # silently editing the composer behind the user's back.
                self.scroll = 0
                self.flash("following live output", 1.2)
                self._force = True
            else:
                self.cursor = len(self.input)
                self._force = True
        elif action == "UP":
            matches = self._ac_matches()
            if matches and self.input.startswith("/"):
                self.ac_index = (self.ac_index - 1) % len(matches)
                self.input = matches[self.ac_index]
                self.cursor = len(self.input)
                self._force = True
            else:
                # On a phone the terminal turns a touch-scroll into arrow keys.
                # Scrolling must scroll, not replay old prompts — even when a
                # draft is already in the composer. Ctrl-P/Shift+Up = history.
                self._scroll_by(3)
        elif action == "DOWN":
            matches = self._ac_matches()
            if matches and self.input.startswith("/"):
                self.ac_index = (self.ac_index + 1) % len(matches)
                self.input = matches[self.ac_index]
                self.cursor = len(self.input)
                self._force = True
            else:
                self._scroll_by(-3)
        elif action in ("HISTUP", "PREV"):
            self._history(-1)
        elif action in ("HISTDN", "NEXT"):
            self._history(1)
        elif action == "TAB":
            self._complete()
        elif action == "PGUP":
            self._scroll_by(max(2, self._last_transcript_height - 2))
        elif action == "PGDN":
            self._scroll_by(-max(2, self._last_transcript_height - 2))
        elif action == "WHEELUP":
            self._scroll_by(3)
        elif action == "WHEELDN":
            self._scroll_by(-3)
        elif action == "CTRL_C":
            self._cancel_pressed()
        elif action == "ESC":
            self._cancel_pressed(esc=True)
        elif action == "CTRL_D":
            if not self.input:
                self.request_quit()
            else:
                self._delete_forward()
        elif action == "CTRL_L":
            self.blocks = []
            self.add_welcome()
            self.scroll = 0
            self._force = True
        elif action == "CTRL_O":
            self.copy_last()
        elif action == "CTRL_T":
            self.verbose = not self.verbose
            self._cache_key = None
            self.flash(f"verbose {'on' if self.verbose else 'off'}", 1.2)
            self._bump()
        elif action == "CTRL_K":
            self.input = self.input[:self.cursor]
            self._force = True
        elif action == "CTRL_U":
            self.input = self.input[self.cursor:]
            self.cursor = 0
            self._force = True
        elif action == "CTRL_W":
            i = self.cursor
            while i > 0 and self.input[i - 1].isspace():
                i -= 1
            while i > 0 and not self.input[i - 1].isspace():
                i -= 1
            self.input = self.input[:i] + self.input[self.cursor:]
            self.cursor = i
            self._force = True
        self._dirty = True

    def submit_input(self):
        text = self.input
        if not text.strip():
            return
        self.input = ""
        self.cursor = 0
        self.hist_idx = None
        self.ac_index = 0
        self.submit(text)

    def submit(self, text: str):
        text = str(text).strip()
        if not text:
            return
        if text.startswith("/"):
            self.handle_command(text)
            return
        if self.running:
            self.add_user(text, steer=True)
            lead = None
            if self.rt is not None:
                try:
                    lead = self.rt.agents.get("a0")
                except Exception:
                    lead = None
            if lead is not None:
                try:
                    lead.send(text)
                    self.flash("steer sent", 1.5)
                except Exception as exc:
                    self.add_notice(f"steer failed: {exc}", "warn")
            else:
                self.pending_steers.append(text)
                self.flash("queued until the run is ready", 1.5)
            return
        self._add_history(text)
        self.start_run(text)

    def _add_history(self, text: str):
        if text and (not self.history or self.history[-1] != text):
            self.history.append(text)
            if len(self.history) > 100:
                self.history = self.history[-100:]

    # -------------------------------------------------- run lifecycle
    def start_run(self, task: str):
        if self.running:
            self.add_notice("a run is already active — ctrl-c twice stops it", "warn")
            return
        if self.rt is not None:
            try:
                self.rt.close()
            except Exception:
                pass
            self.rt = None
        if self.blocks and self.blocks[-1].kind not in ("welcome", "rule"):
            self.blocks.append(Block("rule"))
        self.add_user(task)
        self.running = True
        self.phase = "starting"
        self.run_id = None
        self.run_task = task
        self.run_started = time.time()
        self.done_at = None
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.turns = 0
        self.agent_count = 0
        self.agents = {}
        self.todos = []
        self.files = []
        self.last_report = None
        self._report_seen = False
        self._reported_agents = set()
        self._stream_block = None
        self._stream_agent = None
        self.pending_steers = []
        self.scroll = 0
        self.flash("starting…", 1.5)
        self.thread = threading.Thread(target=self._worker, args=(task, None),
                                       daemon=True, name="hydra-ui-run")
        self.thread.start()
        self._bump()

    def resume_run(self, which=None):
        if self.running:
            self.add_notice("a run is already active", "warn")
            return
        if self.rt is not None:
            try:
                self.rt.close()
            except Exception:
                pass
            self.rt = None
        if self.blocks and self.blocks[-1].kind not in ("welcome", "rule"):
            self.blocks.append(Block("rule"))
        self.add_notice(f"resuming {which or 'the last run in this directory'}…")
        self.running = True
        self.phase = "starting"
        self.run_started = time.time()
        self.done_at = None
        self._report_seen = False
        self._reported_agents = set()
        self._stream_block = None
        self.scroll = 0
        self.flash("resuming…", 1.5)
        self.thread = threading.Thread(target=self._worker, args=("", which),
                                       daemon=True, name="hydra-ui-resume")
        self.thread.start()
        self._bump()

    def _worker(self, task: str, resume=None):
        rt = None
        code, report = 1, {}
        try:
            from .runtime import Runtime
            from . import mentions
            rt = Runtime(self.cfg, cwd=self.cwd, ui=self)
            self.rt = rt
            rt.wake_lock()
            if resume is not None:
                code, report = rt.resume_run(resume)
            else:
                expanded = mentions.expand(self.cfg, task, self.cwd, rt.client)
                code, report = rt.run(
                    expanded, name=getattr(self.opts, "name", None),
                    lead_model=getattr(self.opts, "lead", None))
        except BaseException as exc:
            name = type(exc).__name__
            if name in ("CallCancelled", "KeyboardInterrupt"):
                code = 130
                report = {"status": "interrupted",
                          "summary": "cancelled by user (Ctrl-C)"}
            elif name == "SystemExit":
                code = 2
                report = {"status": "failed", "summary": str(exc)}
            else:
                code = 1
                report = {"status": "failed",
                          "summary": f"{name}: {exc}"}
        finally:
            usage = {}
            turns = agents = 0
            if rt is not None:
                try:
                    usage = rt.usage_totals()
                except Exception:
                    usage = {}
                try:
                    turns = sum(int(getattr(a, "turns", 0) or 0)
                                for a in rt.agents.values())
                    agents = len(rt.agents)
                except Exception:
                    pass
                try:
                    rt.wake_unlock()
                except Exception:
                    pass
            try:
                self.q.put(("done", {"code": code, "report": report,
                                     "usage": usage, "turns": turns,
                                     "agents": agents, "rt": rt}))
            except Exception:
                pass

    # -------------------------------------------------- cancel / quit
    def _cancel_pressed(self, esc=False):
        now = time.monotonic()
        last = self._last_esc if esc else self._last_ctrl_c
        if self.running:
            if now - last <= 2.0:
                self.request_stop()
                return
            if esc:
                self._last_esc = now
            else:
                self._last_ctrl_c = now
            self._cancel_inflight()
            self.flash("cancelling the in-flight call — press again to stop", 2.0)
        else:
            if now - last <= 2.0:
                self.request_quit()
            else:
                if esc:
                    self._last_esc = now
                else:
                    self._last_ctrl_c = now
                if self.input:
                    self.input = ""
                    self.cursor = 0
                    self.flash("input cleared — ctrl-d or /quit to leave", 1.5)
                else:
                    self.flash("press ctrl-c again (or ctrl-d) to leave", 1.5)

    def _cancel_inflight(self):
        self._canceling = True
        self.phase = "canceling"
        try:
            if self.rt is not None and getattr(self.rt, "sigint", None) is not None:
                self.rt.sigint.set()
        except Exception:
            pass
        self._bump()

    def request_stop(self):
        self._canceling = True
        self.phase = "canceling"
        if self.rt is not None:
            try:
                for a in list(self.rt.agents.values()):
                    a.cancelled.set()
            except Exception:
                pass
        try:
            if self.rt is not None and getattr(self.rt, "sigint", None) is not None:
                self.rt.sigint.set()
        except Exception:
            pass
        self.add_notice("stopping the run — checkpoint saved; /resume or hydra resume continues",
                        "warn")
        self.flash("stopping…", 2.0)

    def request_quit(self):
        self.quit = True
        self.return_code = 0
        if self.running:
            self.request_stop()
        self._force = True

    # -------------------------------------------------- commands
    def handle_command(self, line: str):
        parts = str(line).split(None, 1)
        cmd = parts[0]
        arg = parts[1].strip() if len(parts) > 1 else ""
        if cmd in ("/quit", "/exit", "/q"):
            self.request_quit()
            return
        if cmd in ("/help", "/?"):
            self.add_notice(HELP_TEXT)
            return
        if cmd == "/keys":
            self.add_notice(KEYS_TEXT)
            return
        if cmd in ("/clear", "/new"):
            self.blocks = []
            self.add_welcome()
            self.scroll = 0
            self._report_seen = False
            if cmd == "/new":
                self.session_cost = 0.0
            self.flash("screen cleared", 1.2)
            return
        if cmd == "/copy":
            self.copy_last()
            return
        if cmd == "/verbose":
            self.verbose = not self.verbose
            self.flash(f"verbose {'on' if self.verbose else 'off'}", 1.2)
            self._bump()
            return
        if cmd == "/tree":
            if self.rt is None:
                self.add_notice("no run yet")
            else:
                try:
                    self.add_notice(self.rt.swarm.status_lines())
                except Exception as exc:
                    self.add_notice(f"tree failed: {exc}", "warn")
            return
        if cmd == "/kill":
            if not arg:
                self.add_notice("usage: /kill aN")
            elif self.rt is None:
                self.add_notice("no run yet")
            else:
                try:
                    self.add_notice(self.rt.swarm.kill(arg))
                except Exception as exc:
                    self.add_notice(f"kill failed: {exc}", "warn")
            return
        if cmd == "/model":
            role, _, model = arg.partition("=")
            if not model:
                model, role = role.strip(), "lead"
            role = role.strip() or "lead"
            model = model.strip()
            if not model:
                self.add_notice("usage: /model [role=]name")
                return
            base = list(C.get(self.cfg, f"roles.{role}.chain") or [])
            C.set_path(self.cfg, f"roles.{role}.chain",
                       [model] + [m for m in base if m != model])
            if role == "lead":
                self.model = model
                self.current_model = model
            self.add_notice(f"{role} chain: {model} first")
            return
        if cmd == "/models":
            lines = []
            for role in ("lead", "worker", "researcher", "critic", "summarizer",
                         "reviewer", "planner", "debugger", "tester", "scribe"):
                chain = C.get(self.cfg, f"roles.{role}.chain") or []
                lines.append(f"{role:<10} " + " → ".join(str(m) for m in chain))
            self.add_notice("\n".join(lines))
            return
        if cmd in ("/cost", "/usage"):
            session = self.session_cost + (self.cost if self.running else 0)
            lines = [f"run   {self.run_id or '(none)'}",
                     f"tokens {U.fmt_tokens(self.tokens_in)} in / "
                     f"{U.fmt_tokens(self.tokens_out)} out · {self.turns} model calls",
                     f"cost  {U.fmt_money(self.cost)} this run · "
                     f"{U.fmt_money(session)} session"]
            if self.rt is not None and self.run_id:
                try:
                    tot = self.rt.usage_totals()
                    by = tot.get("by_model") or {}
                    for m, d in sorted(by.items()):
                        lines.append(f"  {m:<24} {U.fmt_tokens(d.get('in', 0))}↑ "
                                     f"{U.fmt_tokens(d.get('out', 0))}↓ "
                                     f"{U.fmt_money(d.get('est_usd', 0))}")
                except Exception:
                    pass
            self.add_notice("\n".join(lines))
            return
        if cmd == "/plan":
            self._request_plan()
            return
        if cmd == "/pause":
            if self.rt is None:
                self.add_notice("no run yet")
            else:
                self.rt.paused.set()
                self.add_notice("paused between turns · /unpause to continue")
            return
        if cmd == "/unpause":
            if self.rt is None:
                self.add_notice("no run yet")
            else:
                self.rt.paused.clear()
                self.add_notice("resumed")
            return
        if cmd == "/resume":
            self.resume_run(arg or None)
            return
        if cmd == "/note":
            if not arg:
                self.add_notice("usage: /note text")
            elif self.rt is None or self.rt.journal is None:
                self.add_notice("no active run to note into", "warn")
            else:
                try:
                    self.rt.journal.blackboard_append("ui", arg, persist=False)
                    self.add_notice("noted")
                except Exception as exc:
                    self.add_notice(f"note failed: {exc}", "warn")
            return
        if cmd == "/undo":
            self._undo(arg)
            return
        if cmd == "/runs":
            self._runs()
            return
        if cmd == "/doctor":
            self._doctor()
            return
        if cmd == "/init":
            from .cli import INIT_TASK
            if self.running:
                self.add_notice("a run is already active", "warn")
            else:
                self.start_run(INIT_TASK)
            return
        # project slash commands: ~/.hydra/commands or .hydra/commands
        try:
            from .project import find_commands, render_command
            cmds = find_commands(self.cwd)
            head = cmd.lstrip("/")
            if head in cmds:
                rendered = render_command(cmds[head], arg)
                if self.running:
                    self.submit(rendered)
                else:
                    self.start_run(rendered)
                self.add_notice(f"/{head} sent")
                return
        except Exception:
            pass
        self.add_notice(f"unknown command {cmd} — /help", "warn")

    def _request_plan(self):
        if not self.running:
            if self.run_task:
                # start a fresh task that asks for a plan first
                self.start_run("PLAN FIRST: " + self.run_task)
            else:
                self.add_notice("start a task first")
            return
        rt = self.rt
        lead = None
        if rt is not None:
            try:
                lead = rt.agents.get("a0")
            except Exception:
                lead = None
        if lead is None:
            self.add_notice("the lead is still starting — try /plan again in a moment", "warn")
            return

        def work():
            try:
                from . import planner
                dag, err = planner.make_dag(rt, lead.task or self.run_task or "the current task")
            except Exception as exc:
                dag, err = None, f"{type(exc).__name__}: {exc}"
            try:
                self.q.put(("plan", (dag, err)))
            except Exception:
                pass

        threading.Thread(target=work, daemon=True, name="hydra-ui-plan").start()
        self.flash("planning in the background…", 2.0)

    def _on_plan(self, dag, err):
        if dag is not None:
            try:
                from . import planner
                text = planner.render(dag)
            except Exception as exc:
                text = f"plan: {exc}"
            self.blocks.append(Block("plan", text=text))
            lead = None
            if self.rt is not None:
                try:
                    lead = self.rt.agents.get("a0")
                except Exception:
                    lead = None
            if lead is not None:
                try:
                    lead.nudge("Here is the plan the user requested. Follow this "
                               "DAG (todo tool to track it) and execute step by step:\n"
                               + text)
                except Exception:
                    pass
            self.flash("plan sent to the lead", 2.0)
        else:
            lead = None
            if self.rt is not None:
                try:
                    lead = self.rt.agents.get("a0")
                except Exception:
                    lead = None
            if lead is not None:
                try:
                    lead.nudge("PLAN FIRST: before any further tool action, emit your "
                               "plan as a todo list (todo tool), then execute it step "
                               "by step.")
                except Exception:
                    pass
            self.add_notice(f"plan requested from the lead ({err})")
        self._bump()

    def _undo(self, arg):
        from . import snapshots
        dry = "--dry-run" in arg
        rid = None
        to = None
        bits = arg.split()
        i = 0
        while i < len(bits):
            b = bits[i]
            if b == "--to" and i + 1 < len(bits):
                i += 1
                try:
                    to = int(bits[i])
                except ValueError:
                    self.add_notice("usage: /undo [RUN] [--to N] [--dry-run]", "warn")
                    return
            elif b.startswith("--to="):
                try:
                    to = int(b.split("=", 1)[1])
                except ValueError:
                    self.add_notice("usage: /undo [RUN] [--to N] [--dry-run]", "warn")
                    return
            elif not b.startswith("-"):
                rid = b
            i += 1
        try:
            info = snapshots.undo(str(self.cwd), run_id=rid or self.run_id, to=to,
                                  dry_run=dry)
        except Exception as exc:
            self.add_notice(f"undo failed: {exc}", "warn")
            return
        if not info.get("ok"):
            self.add_notice(f"undo: {info.get('note')}", "warn")
            return
        verb = "would restore" if dry else "restored"
        lines = [f"run {info.get('run')} turn {info.get('turn')} "
                 f"({str(info.get('sha'))[:12]}): {verb}"]
        for p in (info.get("restore") or [])[:12]:
            lines.append(("  restore " if not dry else "  change  ") + str(p))
        for p in (info.get("remove") or [])[:12]:
            lines.append(("  remove  " if not dry else "  delete  ") + str(p))
        self.add_notice("\n".join(lines))

    def _runs(self):
        ids = J.run_ids()[:8]
        if not ids:
            self.add_notice("no runs yet")
            return
        lines = []
        for rid in ids:
            data = J.load_run(rid) or {}
            task = U.short(data.get("task") or "", 60)
            lines.append(f"{rid}  {str(data.get('status') or '?'):<10} {task}")
        self.add_notice("\n".join(lines))

    def _doctor(self):
        lines = [
            f"python   {sys.version.split()[0]}",
            f"cwd      {self.cwd}",
            f"host     {self.host}",
            f"key      {self._key_label()}",
            f"plan     {self.plan}",
            f"lead     {self.model}",
            f"tmux     {'found' if shutil.which('tmux') else 'missing (plain fallback works)'}",
            f"clipboard {'termux-clipboard-set' if shutil.which('termux-clipboard-set') else 'OSC52'}",
            f"runs     {len(J.run_ids())}",
        ]
        self.add_notice("\n".join(lines))

    # -------------------------------------------------- clipboard
    def copy_last(self):
        text = ""
        name = ""
        rep = self.last_report or {}
        for p in rep.get("artifacts") or []:
            try:
                fp = Path(p)
                if not fp.is_absolute():
                    fp = self.cwd / fp
                if fp.is_file() and fp.stat().st_size <= 500_000:
                    text = fp.read_text(encoding="utf-8", errors="replace")
                    name = str(p)
                    break
            except Exception:
                continue
        if not text:
            text = self.last_assistant or ""
            name = "last answer"
        if not text.strip():
            self.flash("nothing to copy yet", 1.5)
            return
        ok = False
        exe = shutil.which("termux-clipboard-set")
        if exe:
            try:
                subprocess.run([exe], input=text.encode("utf-8"),
                               timeout=10, check=False)
                ok = True
            except Exception:
                ok = False
        if not ok:
            try:
                b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
                os.write(1, f"\x1b]52;c;{b64}\x07".encode("ascii"))
                ok = True
            except Exception:
                ok = False
        self.flash(f"copied {name}" if ok else "could not access the clipboard",
                   2.0)

    # -------------------------------------------------- real terminal
    def _write(self, text: str):
        try:
            os.write(1, text.encode("utf-8", "replace"))
        except Exception:
            pass

    def draw(self):
        w, h = self._term_size()
        if (w, h) != (self.width, self.height):
            self.width, self.height = w, h
            self._cache_key = None
            self._write("\x1b[2J")
        rows, cursor = self._compose(self.width, self.height)
        out = ["\x1b[H"]
        for i, spans in enumerate(rows):
            out.append("\x1b[2K")
            for text, style in spans:
                code = self.theme.s(style)
                if code:
                    out.append(code)
                    out.append(text)
                    out.append("\x1b[0m")
                else:
                    out.append(text)
            if i < len(rows) - 1:
                out.append("\r\n")
        out.append("\x1b[?25l")
        if cursor is not None:
            r, c = cursor
            out.append(f"\x1b[{r + 1};{c + 1}H\x1b[?25h")
        self._write("".join(out))
        self._last_draw = time.monotonic()
        self._force = False
        self._dirty = False

    def _on_winch(self, *_):
        self._resize = True

    def _on_signal(self, signum, frame):
        self.sig_stop = True
        self._sig = signum

    def _install_terminal(self):
        import termios
        import tty
        self._old_term = termios.tcgetattr(0)
        # Install the signal handlers BEFORE any escape byte leaves the
        # process: tests (and terminals) may react to seeing full-screen
        # mode by signalling immediately, and that signal must not kill us
        # with the tty left raw.
        for sig in (signal.SIGWINCH, signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            try:
                self._old_sigs[sig] = signal.getsignal(sig)
                if sig == signal.SIGWINCH:
                    signal.signal(sig, self._on_winch)
                else:
                    signal.signal(sig, self._on_signal)
            except Exception:
                pass
        tty.setraw(0)
        # v1.2.4: do NOT capture the mouse by default.  `?1007h` is
        # alternate-scroll: a swipe arrives as arrow keys, while Termux keeps
        # native swipe-scroll and long-press selection (WO2 D8 / §5.2).
        seq = ("\x1b[?1049h\x1b[?25l\x1b[?2004h\x1b[?1007h"
               "\x1b[2J\x1b[H")
        if self.mouse_opt:
            seq = ("\x1b[?1049h\x1b[?25l\x1b[?2004h"
                   "\x1b[?1000h\x1b[?1002h\x1b[?1006h\x1b[?1007h"
                   "\x1b[2J\x1b[H")
        self._write(seq)

    def _restore_terminal(self):
        try:
            self._write("\x1b[?1007l\x1b[?1006l\x1b[?1002l\x1b[?1000l"
                        "\x1b[?2004l\x1b[?25h\x1b[0m\x1b[?1049l")
        except Exception:
            pass
        if self._old_term is not None:
            try:
                import termios
                termios.tcsetattr(0, termios.TCSAFLUSH, self._old_term)
            except Exception:
                pass
            self._old_term = None
        for sig, handler in self._old_sigs.items():
            try:
                signal.signal(sig, handler)
            except Exception:
                pass
        self._old_sigs = {}

    def run(self) -> int:
        if not (os.isatty(0) and os.isatty(1)):
            return self._run_non_tty()
        self._install_terminal()
        try:
            self.draw()
            if self.initial:
                task, self.initial = self.initial, ""
                self.submit(task)
                self.draw()
            while not self.quit:
                self.drain()
                if self.sig_stop:
                    if not self.running:
                        self.quit = True
                        break
                    self.request_stop()
                    self.sig_stop = False
                if self._resize:
                    self._resize = False
                    self._cache_key = None
                    self._write("\x1b[2J")
                    self.draw()
                if self.running:
                    timeout = 0.08
                else:
                    timeout = 0.15
                data = b""
                ready = []
                try:
                    ready, _, _ = select.select([0], [], [], timeout)
                    if ready:
                        data = os.read(0, 4096)
                except InterruptedError:
                    data = b""
                except Exception:
                    data = b""
                if data:
                    self.feed_bytes(data)
                elif data == b"" and ready:
                    # stdin closed
                    self.quit = True
                    break
                now = time.monotonic()
                if self._flash_until and now >= self._flash_until:
                    self._flash_until = 0.0
                    self._force = True
                if self.running:
                    if now - self._last_draw >= 0.09:
                        self.draw()
                elif self._force or (self._flash_until > now
                                     and now - self._last_draw >= 0.1):
                    self.draw()
            return self.return_code
        finally:
            if self.running:
                try:
                    self.request_stop()
                except Exception:
                    pass
                th = self.thread
                if th is not None:
                    th.join(timeout=2.0)
            try:
                if self.rt is not None:
                    self.rt.close()
            except Exception:
                pass
            self._restore_terminal()
            self._print_exit()

    def _run_non_tty(self) -> int:
        # A pipe / CI: don't try raw mode; read lines and submit them.
        self.add_notice("not a terminal — interactive UI disabled")
        self._print_exit()
        return 2

    def _print_exit(self):
        if not self.run_id:
            return
        rep = self.last_report or {}
        status = rep.get("status") or ("interrupted" if self.running else "done")
        print()
        print(f"{status} · {self.run_id} · {self.turns} turns · "
              f"{self.agent_count} agents · {U.fmt_tokens(self.tokens_in)} in / "
              f"{U.fmt_tokens(self.tokens_out)} out · {U.fmt_money(self.cost)}")
        summary = str(rep.get("summary") or "").strip()
        if summary:
            print()
            try:
                width = max(30, shutil.get_terminal_size((80, 24)).columns)
                print("\n".join(r for r, _ in U.markdown_rows(summary, width)))
            except Exception:
                print(U.strip_inline(summary))
        for p in rep.get("artifacts") or []:
            print(f"\nartifact: {p}")
        print(f"\nrun saved: {C.run_dir(self.run_id)}")
        print(f"resume:    hydra resume {self.run_id}")
        print(f"events:    hydra tail {self.run_id}")


def run_tui(cfg: dict, opts=None, initial: str = "") -> int:
    app = App(cfg, opts, initial=initial)
    return app.run()
