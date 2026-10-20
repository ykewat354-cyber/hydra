"""P6 — prompt footprint table that adds up.

The pinned prefix has several parts.  WO#3's table listed system + env +
tools but the printed total was larger, because message framing (JSON keys,
role wrappers, task line, escaping) was missing.  This module reports every
part and makes the last row the exact remainder, so the rows always sum to
``Agent.pinned_tokens``.

    hydra footprint          # ANSI-free table
    hydra footprint --json   # machine-readable
"""
from __future__ import annotations

import json
import os
import re
import sys

from . import config as C

_HEADINGS = (
    ("# Workspace", "workspace summary"),
    ("# Recent runs", "recent runs"),
    ("# Project instructions", "project instructions"),
    ("# Skills", "skills list"),
)
_ORDER = ("system prompt", "env block", "workspace summary", "recent runs",
          "project instructions", "skills list", "tool schemas",
          "message framing")


def _tok(text: str) -> int:
    """The same characters/3.5 estimate the runtime uses."""
    return int(len(str(text or "")) / 3.5)


def _split_env(env: str) -> dict:
    """Split the env block into its labelled sections (heading-based)."""
    out = {label: "" for _, label in _HEADINGS}
    if not env:
        return {"env block": "", **out}
    chunks = re.split(r"\n\n(?=# )", str(env))
    fixed = []
    for chunk in chunks:
        for prefix, label in _HEADINGS:
            if chunk.startswith(prefix):
                out[label] = (out[label] + "\n\n" + chunk).strip("\n")
                break
        else:
            fixed.append(chunk)
    return {"env block": "\n\n".join(fixed), **out}


def measure(agent, rt) -> dict:
    """Return ``{"parts": {label: tokens}, "total": int}``."""
    total = int(agent.pinned_tokens(rt))
    parts = {}
    parts["system prompt"] = _tok(agent.system_prompt)
    env_parts = _split_env(agent.env_block or "")
    parts["env block"] = _tok(env_parts.get("env block", ""))
    parts["workspace summary"] = _tok(env_parts.get("workspace summary", ""))
    parts["recent runs"] = _tok(env_parts.get("recent runs", ""))
    parts["project instructions"] = _tok(
        env_parts.get("project instructions", ""))
    parts["skills list"] = _tok(env_parts.get("skills list", ""))
    parts["tool schemas"] = _tok(agent._tools_json(rt))
    known = sum(parts.get(k, 0) for k in _ORDER if k != "message framing")
    # Exact remainder: JSON role wrappers, commas, the task line and escaping.
    parts["message framing"] = max(0, total - known)
    return {"parts": {k: int(parts.get(k, 0)) for k in _ORDER},
            "total": total}


def render(measured: dict) -> str:
    lines = [f"{'part':<22} {'tokens':>7}"]
    parts = measured.get("parts") or {}
    for label in _ORDER:
        lines.append(f"{label:<22} {int(parts.get(label, 0)):>7}")
    total = int(measured.get("total") or 0)
    lines.append(f"{'total':<22} {total:>7}")
    return "\n".join(lines)


def cli(cfg, args=None) -> int:
    args = list(args or [])
    if any(a not in ("--json",) for a in args):
        print("usage: hydra footprint [--json]")
        return 2
    cwd = os.getcwd()
    try:
        from .agent import Agent, build_env_block, load_system_prompt
        from .runtime import Runtime
        rt = Runtime(cfg, cwd=cwd, quiet=True)
        env = build_env_block(cwd, cfg, role="lead", caps=rt.caps)
        agent = Agent("a0", "lead", "", cfg, workdir=cwd,
                      system_prompt=load_system_prompt("lead"),
                      env_block=env)
        measured = measure(agent, rt)
    except Exception as exc:
        print(f"hydra footprint: {type(exc).__name__}: {exc}")
        return 1
    if "--json" in args:
        print(json.dumps(measured, ensure_ascii=False, indent=2))
    else:
        sys.stdout.write(render(measured) + "\n")
    return 0
