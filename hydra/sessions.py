"""Human-friendly session discovery for resume, the CLI and the TUI.

A run is a directory under ``~/.hydra/runs``.  ``run.json`` says ``running``
even after Android/Termux kills the process, so the status written there is not
enough on its own: this module checks the recorded pid and presents a stable
``state`` (``running``, ``interrupted``, ``done``, ``parked``, ...).

The functions here are deliberately side-effect free.  Runtime owns execution;
this module only answers "which session should we offer, and what was it doing?"
"""
from __future__ import annotations

import os
import re
import time
from collections import deque
from pathlib import Path

from . import config as C
from . import journal as J

_PREFIXES = ("master prompt:", "prompt:", "task:", "user request:", "user:")
_WS = re.compile(r"\s+")


# ---------------------------------------------------------------- process state

def pid_alive(pid) -> bool:
    """True when *pid* exists and still looks like a Python/HYDRA process.

    A bare ``os.kill(pid, 0)`` is not enough across a reboot: Android may have
    reused the pid for an unrelated app.  When /proc is readable we require a
    familiar marker, but we fail open if it is not readable.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip().lower()
        if cmd:
            return any(word in cmd for word in ("hydra", "python", "pytest"))
    except Exception:
        pass
    return True


def run_state(meta: dict) -> str:
    """The state a user should see, correcting a stale ``running`` marker."""
    if not isinstance(meta, dict):
        return "?"
    status = str(meta.get("status") or "?")
    if status == "running":
        if meta.get("finished"):
            return "interrupted"
        if pid_alive(meta.get("pid")):
            return "running"
        return "interrupted"
    return status


def is_resumable(meta: dict) -> bool:
    """A run worth offering in the resume picker.

    Finished runs can still be resumed by explicitly naming them; they are just
    not what a bare ``hydra resume`` should choose.
    """
    state = run_state(meta)
    return state in ("interrupted", "parked", "paused")


# ---------------------------------------------------------------- titles

def _clean_line(text: str) -> str:
    line = str(text or "").strip()
    if not line:
        return ""
    line = re.sub(r"^[#>*\-\s]+", "", line)
    low = line.lower()
    for prefix in _PREFIXES:
        if low.startswith(prefix):
            line = line[len(prefix):].strip()
            low = line.lower()
            break
    line = line.replace("`", "").replace("**", "")
    return _WS.sub(" ", line).strip()


def title(meta: dict, limit: int = 64) -> str:
    """A short, stable session name (explicit ``name`` wins over the task)."""
    if not isinstance(meta, dict):
        return "(untitled run)"
    name = _clean_line(meta.get("name") or "")
    text = name or _clean_line(meta.get("task") or "")
    if not text:
        text = "(untitled run)"
    if limit > 1 and len(text) > limit:
        text = text[:limit - 1].rstrip() + "…"
    return text


def auto_name(task: str, limit: int = 64) -> str:
    return title({"task": task}, limit=limit)


def is_placeholder_task(task: str) -> bool:
    """Old TUI paste-chip bugs created junk sessions named after the chip."""
    text = _clean_line(task).casefold()
    return (not text
            or text.startswith("task with the paste")
            or text in {"hi", "hello", "test", "kya hua"})


# ---------------------------------------------------------------- activity

def _event_tail(run_id: str, count: int = 400) -> list:
    p = C.run_dir(run_id) / "events.jsonl"
    if not p.is_file():
        return []
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            return list(deque(fh, maxlen=count))
    except Exception:
        return []


def _tool_activity(ev: dict) -> str:
    tool = str(ev.get("tool") or "tool")
    args = ev.get("args") if isinstance(ev.get("args"), dict) else {}
    for key in ("cmd", "query", "url", "path", "file", "name"):
        val = args.get(key)
        if val:
            text = _WS.sub(" ", str(val)).strip()
            if len(text) > 72:
                text = text[:71].rstrip() + "…"
            return f"{tool}: {text}"
    return tool


def activity(run_id: str) -> str:
    """What the session was doing last (tool/answer/report), for the picker."""
    import json
    for raw in reversed(_event_tail(run_id)):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        kind = ev.get("type")
        if kind == "report":
            rep = ev.get("report") if isinstance(ev.get("report"), dict) else {}
            summary = _clean_line(rep.get("summary") or "")
            if summary:
                return "report: " + summary
        elif kind == "tool_call":
            return _tool_activity(ev)
        elif kind == "assistant":
            text = _clean_line(ev.get("text") or "")
            if text:
                if len(text) > 72:
                    text = text[:71].rstrip() + "…"
                return "answer: " + text
        elif kind in ("user_msg", "steer"):
            text = _clean_line(ev.get("text") or ev.get("preview") or "")
            if text:
                return "you: " + text
    return ""


def last_failure(run_id: str) -> str:
    """Last failed tool result or error, for an interrupted-session hint."""
    import json
    for raw in reversed(_event_tail(run_id)):
        try:
            ev = json.loads(raw)
        except Exception:
            continue
        if ev.get("type") == "tool_result" and ev.get("ok") is False:
            text = _clean_line(ev.get("preview") or "")
            if text:
                return text[:110]
        if ev.get("type") in ("error", "run_error"):
            text = _clean_line(ev.get("preview") or ev.get("message") or "")
            if text:
                return text[:110]
    return ""


# ---------------------------------------------------------------- records

def run_cost(run_id: str) -> float:
    """Best-effort estimated USD already spent by a run."""
    import json
    p = C.run_dir(run_id) / "usage.jsonl"
    if not p.is_file():
        return 0.0
    total = 0.0
    try:
        with open(p, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    row = json.loads(line)
                    total += float(row.get("est_usd") or 0.0)
                except Exception:
                    continue
    except Exception:
        return 0.0
    return total


def record(run_id: str, with_activity: bool = True,
           with_cost: bool = True) -> dict:
    meta = J.load_run(run_id) or {}
    if not meta:
        return {}
    state = run_state(meta)
    started = float(meta.get("started") or 0.0)
    return {
        "run": run_id,
        "name": str(meta.get("name") or ""),
        "title": title(meta),
        "task": str(meta.get("task") or ""),
        "cwd": str(meta.get("cwd") or ""),
        "status": str(meta.get("status") or "?"),
        "state": state,
        "started": started,
        "age": max(0.0, time.time() - started) if started else 0.0,
        "pid": meta.get("pid"),
        "resumable": is_resumable(meta),
        "activity": activity(run_id) if with_activity else "",
        "cost": run_cost(run_id) if with_cost else 0.0,
        "attempts": int(meta.get("auto_resume_attempts") or 0),
        "meta": meta,
    }


def session_rows(cwd: str = None, limit: int = 30,
                 with_activity: bool = True,
                 with_cost: bool = True) -> list:
    """Newest sessions first; sessions from *cwd* and resumable ones first."""
    rows = []
    for rid in J.run_ids():
        row = record(rid, with_activity=with_activity, with_cost=with_cost)
        if row and not is_placeholder_task(row.get("task") or ""):
            rows.append(row)
    if cwd:
        here = _norm_path(cwd)
        for row in rows:
            row["same_cwd"] = bool(here and _norm_path(row["cwd"]) == here)
        rows.sort(key=lambda r: (not r.get("same_cwd"), not r["resumable"],
                                 -r["started"], r["run"]))
    else:
        rows.sort(key=lambda r: (not r["resumable"], -r["started"], r["run"]))
    return rows[:max(1, int(limit))]


def pick_resume(which: str = None, cwd: str = None) -> str | None:
    """Resolve an explicit id/name, or choose the newest interrupted session.

    Bare resume never chooses a finished run: that was the old bug where it
    replayed the latest ``done`` session and looked like nothing happened.
    """
    if which:
        return resolve_run(which)
    all_rows = session_rows(cwd=None, limit=200, with_activity=False,
                            with_cost=False)
    if cwd:
        want = _norm_path(cwd)
        local = [r for r in all_rows if _norm_path(r["cwd"]) == want]
        for row in local:
            if row["resumable"]:
                return row["run"]
    for row in all_rows:
        if row["resumable"]:
            return row["run"]
    return None


def pick_auto_resume(cwd: str = None, max_attempts: int = 3,
                     grace_secs: float = 5.0,
                     max_age_hours: float = 24,
                     max_run_usd: float = 0.08) -> str | None:
    """Newest run that was killed by a signal/process death.

    Deliberate stops (``stop_reason == user``), budget-parked runs, very old
    experiments, the old paste-chip junk and runs already above the spend cap
    are not auto-resumed.  A run that crashes repeatedly stops after
    *max_attempts*.
    """
    now = time.time()
    for row in session_rows(cwd=cwd, limit=200, with_activity=False,
                            with_cost=False):
        if row["state"] != "interrupted":
            continue
        meta = row["meta"]
        if str(meta.get("stop_reason") or "") == "user":
            continue
        if meta.get("auto_resume_disabled"):
            continue
        if row.get("attempts", 0) >= int(max_attempts):
            continue
        if not row["started"]:
            continue
        if now - row["started"] < float(grace_secs):
            continue
        if (max_age_hours
                and now - row["started"] > float(max_age_hours) * 3600):
            continue
        if is_placeholder_task(meta.get("task") or ""):
            continue
        if not str(meta.get("task") or "").strip():
            continue
        if max_run_usd and run_cost(row["run"]) >= float(max_run_usd):
            # An expensive run is a deliberate choice, not a free background
            # restart. The picker still offers it with its cost visible.
            continue
        trans = C.run_dir(row["run"]) / "agents" / "a0" / "transcript.jsonl"
        try:
            if not trans.is_file() or trans.stat().st_size <= 0:
                continue
        except OSError:
            continue
        return row["run"]
    return None


def _norm_path(path) -> str:
    try:
        return str(Path(path or "").expanduser().resolve())
    except Exception:
        return str(path or "")


def resolve_run(name_or_id: str) -> str | None:
    """Resolve an exact id/name, then a unique id/name prefix."""
    value = str(name_or_id or "").strip()
    if not value:
        return None
    if (C.run_dir(value) / "run.json").is_file():
        return value
    ids = J.run_ids()
    for rid in ids:
        data = J.load_run(rid) or {}
        if str(data.get("name") or "") == value:
            return rid
    pref = [rid for rid in ids if rid.startswith(value)]
    if len(pref) == 1:
        return pref[0]
    low = value.casefold()
    named = []
    titled = []
    for rid in ids:
        data = J.load_run(rid) or {}
        name = str(data.get("name") or "")
        if name and name.casefold().startswith(low):
            named.append(rid)
        label = title(data).casefold()
        if label and label.startswith(low):
            titled.append(rid)
    if len(named) == 1:
        return named[0]
    if len(titled) == 1:
        return titled[0]
    return None


def state_rank(state: str) -> int:
    return {"interrupted": 0, "parked": 1, "paused": 2,
            "running": 3, "failed": 4, "partial": 5, "done": 6}.get(state, 9)


def age_text(seconds: float) -> str:
    """Compact age for a phone-width picker ("2m", "3h05m", "27d")."""
    try:
        s = max(0, int(seconds or 0))
    except Exception:
        s = 0
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        return f"{s // 3600}h{(s % 3600) // 60:02d}m"
    return f"{s // 86400}d{(s % 86400) // 3600:02d}h"
