"""Hooks & auto-verify (§19.3): shell observers, never gates.

A hook receives JSON on stdin (event, run, agent, tool, args, result_preview)
plus HYDRA_EVENT / HYDRA_RUN / HYDRA_AGENT in the environment. Its exit code or
output can never cancel or alter an action; output from post_tool / post_edit
is appended to the tool result as ``[hook] …`` so feedback reaches the model.
Auto-verify is the built-in post_edit hook (per-file commands, once per turn).
"""
from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path

from . import config as C

EDIT_TOOLS = ("write_file", "edit_file", "apply_patch")


def run_hook(rt, event: str, agent=None, tool=None, args=None,
             result_preview=None, timeout: float = None) -> str:
    """Run the configured hook for ``event``. Never raises; returns stdout tail."""
    cmd = C.get(rt.cfg, f"hooks.{event}", "") if rt is not None else ""
    if not cmd:
        return ""
    if timeout is None:
        timeout = float(C.get(rt.cfg, "hooks.timeout", 30) or 30)
    payload = {
        "event": event,
        "run": getattr(rt, "run_id", None),
        "agent": getattr(agent, "id", None),
        "tool": tool,
        "args": args,
        "result_preview": str(result_preview or "")[:500],
    }
    env = dict(os.environ)
    env["HYDRA_EVENT"] = str(event)
    env["HYDRA_RUN"] = str(getattr(rt, "run_id", "") or "")
    env["HYDRA_AGENT"] = str(getattr(agent, "id", "") or "")
    try:
        p = subprocess.run([C.shell_path(), "-lc", str(cmd)],
                           input=json.dumps(payload), capture_output=True,
                           text=True, timeout=float(timeout), env=env)
    except Exception as exc:
        _journal(rt, agent, event, error=f"{type(exc).__name__}: {exc}")
        return ""
    out = (p.stdout or "").strip()
    err = (p.stderr or "").strip()
    if p.returncode != 0 or err:
        _journal(rt, agent, event,
                 error=(err or f"exit {p.returncode}")[-300:])
    if out:
        _journal(rt, agent, event, output=out[:400])
    return out[-1500:]


def _journal(rt, agent, event, **kw) -> None:
    j = getattr(rt, "journal", None)
    if j is not None:
        try:
            j.event("hook", getattr(agent, "id", None), hook=event, **kw)
        except Exception:
            pass


def _append_hook_note(result: dict, note: str) -> None:
    if not note:
        return
    result["content"] = str(result.get("content") or "") + "\n[hook] " + note


# ---------------------------------------------------------------- turn pass

def _edited_paths(call: dict, result: dict) -> list:
    """Files a single edit-tool call touched (from its args)."""
    args = (call.get("function") or {}).get("arguments") or {}
    name = (call.get("function") or {}).get("name")
    paths = []
    if name in ("write_file", "edit_file"):
        if args.get("path"):
            paths.append(str(args["path"]))
    elif name == "apply_patch":
        text = str(args.get("patch") or args.get("input") or "")
        for line in text.splitlines():
            for pre in ("*** Update File: ", "*** Add File: ", "*** Delete File: ",
                        "+++ b/", "--- a/"):
                if line.startswith(pre):
                    paths.append(line[len(pre):].strip())
                    break
    return paths


def apply_turn_hooks(rt, agent, calls: list, results: list) -> None:
    """After a tool batch: post_edit hook per edit call + auto-verify per file
    (once per turn), appending ``[hook] …`` notes to the tool results in place."""
    # custom post_edit hook — observer only
    for call, res in zip(calls, results):
        name = (call.get("function") or {}).get("name")
        if name in EDIT_TOOLS:
            note = run_hook(rt, "post_edit", agent, tool=name,
                            args=(call.get("function") or {}).get("arguments"),
                            result_preview=res.get("content"))
            _append_hook_note(res, note)

    if not C.get(rt.cfg, "autoverify.enabled", True):
        return
    globs = C.get(rt.cfg, "autoverify.globs") or {}
    timeout = float(C.get(rt.cfg, "autoverify.timeout", 60) or 60)
    if not globs:
        return

    seen = set()
    for call, res in zip(calls, results):
        name = (call.get("function") or {}).get("name")
        if name not in EDIT_TOOLS:
            continue
        for path in _edited_paths(call, res):
            if path in seen:
                continue
            seen.add(path)
            cmd_tpl = None
            for pattern, command in globs.items():
                if fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(
                        Path(path).name, pattern):
                    cmd_tpl = str(command)
                    break
            if not cmd_tpl:
                continue
            cmd = cmd_tpl.replace("{file}", _q(path))
            try:
                p = subprocess.run([C.shell_path(), "-lc", cmd_cd(rt, cmd)],
                                   capture_output=True, text=True,
                                   timeout=timeout)
                tail = ((p.stdout or "") + (p.stderr or "")).strip()
                if p.returncode == 0:
                    _append_hook_note(res, f"autoverify {path}: ✓")
                else:
                    _append_hook_note(res, f"autoverify {path} (failed, exit "
                                           f"{p.returncode}):\n{tail[-1500:]}")
            except Exception as exc:
                _append_hook_note(res, f"autoverify {path} (error: "
                                       f"{type(exc).__name__}: {exc})")


def _q(path: str) -> str:
    return "'" + str(path).replace("'", "'\\''") + "'"


def cmd_cd(rt, cmd: str) -> str:
    cwd = getattr(rt, "cwd", None)
    if cwd is None:
        return cmd
    return f"cd {_q(str(cwd))} && {cmd}"
