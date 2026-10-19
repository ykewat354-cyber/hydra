"""F3 ``hydra report``: paste-friendly, redacted summaries of a run.

    hydra report [RUN_ID|--last] [--full] [--json] [--since 2h] [--timeline]

One run in Markdown (≤ 120 lines unless ``--full``), one JSON object with
``--json``, or a table over every run started inside the ``--since`` window.
Everything printed goes through the existing hygiene redactor, so the output
can be pasted into a bug report without leaking the API key.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import config as C
from . import journal as J

REPORT_CAP = 120      # Markdown lines unless --full
EVENT_ROWS = 8        # "last events" rows
TIMELINE_ROWS = 40    # timeline rows unless --full
TIMELINE_MAX = 200    # never build more than this many timeline rows

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


# ---------------------------------------------------------------- helpers

def parse_window(text) -> float:
    """Flexible window: ``30m``, ``2h``, ``3d`` (also ``s`` and ``w``)."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|m|h|d|w)\s*", str(text or ""))
    if not m:
        raise ValueError(f"bad --since value {text!r} (use e.g. 30m, 2h, 3d)")
    return float(m.group(1)) * _UNITS[m.group(2)]


def _window_text(seconds: float) -> str:
    for unit, size in (("d", 86400), ("h", 3600), ("m", 60)):
        if seconds >= size:
            return f"{seconds / size:g}{unit}"
    return f"{seconds:g}s"


def _short(text, n: int) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def _tok(n) -> str:
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1_000_000:
        return f"{n / 1000:.1f}k"
    return f"{n / 1_000_000:.1f}M"


def _hms(ts) -> str:
    return time.strftime("%H:%M:%S", time.localtime(float(ts or 0)))


def _duration_text(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins}m{secs:02d}s"
    hours, mins = divmod(mins, 60)
    return f"{hours}h{mins:02d}m"


def _md_cell(text, n: int) -> str:
    return _short(text, n).replace("|", "\\|")


def _event_brief(ev: dict) -> str:
    """One compact human line for an event (never the raw payload)."""
    t = str(ev.get("type") or "?")
    if t == "run_start":
        return _short(ev.get("task"), 70)
    if t == "assistant":
        return _short(ev.get("text"), 70)
    if t == "tool_call":
        try:
            args = json.dumps(ev.get("args") or {}, ensure_ascii=False,
                              sort_keys=True)
        except Exception:
            args = str(ev.get("args"))
        return _short(f"{ev.get('tool')} {args}", 70)
    if t == "tool_result":
        return _short(f"{ev.get('tool')} → {ev.get('preview')}", 70)
    if t == "model_response":
        return (f"{ev.get('model')} {_tok(ev.get('in_tokens'))} in / "
                f"{_tok(ev.get('out_tokens'))} out")
    if t == "model_request":
        return f"{ev.get('model')} attempt {ev.get('attempt') or 1}"
    if t == "report":
        rep = ev.get("report") or {}
        return _short(f"{(rep.get('status') or '?')}: {rep.get('summary')}", 70)
    if t == "compact":
        return f"retired {ev.get('retired') or 0} messages"
    if t in ("agent_start", "agent_spawn"):
        return _short(ev.get("role") or ev.get("task") or "", 70)
    if t == "error":
        return _short(ev.get("error"), 70)
    for key in ("note", "task", "preview", "text", "summary", "msg"):
        if ev.get(key):
            return _short(ev.get(key), 70)
    return ""


def read_http(run_id: str) -> list:
    """The run's runs/<id>/http.jsonl records (F4), skipping broken lines."""
    p = C.run_dir(run_id) / "http.jsonl"
    out = []
    if not p.is_file():
        return out
    try:
        with open(p, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict):
                    out.append(rec)
    except OSError:
        return out
    return out


def _event_status(ev: dict):
    """HTTP-looking status for fallback histograms (no http.jsonl)."""
    blob = " ".join(str(ev.get(k) or "") for k in ("error", "note"))
    m = re.search(r"\bHTTP[ -]?(\d{3})\b", blob)
    return int(m.group(1)) if m else None


def git_stat(cwd) -> list | None:
    """``git diff --stat`` for the run's cwd; None when it is not a repo."""
    if not cwd:
        return None
    try:
        probe = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10)
        if probe.returncode != 0 or probe.stdout.strip() != "true":
            return None
        stat = subprocess.run(["git", "-C", str(cwd), "diff", "--stat"],
                              capture_output=True, text=True, timeout=20)
    except Exception:
        return None
    if stat.returncode != 0:
        return None
    return [ln.rstrip() for ln in stat.stdout.splitlines() if ln.strip()]


# ---------------------------------------------------------------- collection

def collect(run_id: str, timeline: bool = False) -> dict:
    """Everything hydra report shows for one run. JSON-serializable."""
    meta = J.load_run(run_id) or {}
    events = J.read_events(run_id)
    usage = J.read_usage(run_id)
    http = read_http(run_id)

    agents, order = {}, []
    retries = failovers = compactions = 0
    tools: dict = {}
    last_model: dict = {}
    for ev in events:
        t = ev.get("type")
        a = ev.get("agent")
        if t == "agent_start" and a is not None and a not in agents:
            agents[a] = {"id": a, "role": ev.get("role") or "?",
                         "depth": int(ev.get("depth") or 0),
                         "parent": ev.get("parent"), "model": "", "models": [],
                         "turns": 0, "in": 0, "out": 0, "status": "running"}
            order.append(a)
        elif t == "model_response" and a in agents:
            d = agents[a]
            m = str(ev.get("model") or "")
            if m:
                d["model"] = m
                if m not in d["models"]:
                    d["models"].append(m)
            d["turns"] += 1
            d["in"] += int(ev.get("in_tokens") or 0)
            d["out"] += int(ev.get("out_tokens") or 0)
        elif t == "report" and a in agents:
            agents[a]["status"] = (ev.get("report") or {}).get("status") or \
                agents[a]["status"]
        elif t == "model_request":
            if int(ev.get("attempt") or 1) > 1:
                retries += 1
            if a in last_model and ev.get("model") != last_model[a]:
                failovers += 1
            last_model[a] = ev.get("model")
        elif t == "tool_call":
            name = str(ev.get("tool") or "?")
            tools[name] = tools.get(name, 0) + 1
        elif t == "compact":
            compactions += 1

    if not http:
        hist: dict = {}
        for ev in events:
            if ev.get("type") == "model_response":
                hist["200"] = hist.get("200", 0) + 1
            else:
                st = _event_status(ev)
                if st is not None:
                    hist[str(st)] = hist.get(str(st), 0) + 1
    else:
        hist = {}
        for rec in http:
            key = str(rec.get("status", "?"))
            hist[key] = hist.get(key, 0) + 1

    by_model: dict = {}
    for r in usage:
        m = str(r.get("model") or "?")
        d = by_model.setdefault(m, {"in": 0, "out": 0, "est_usd": 0.0, "calls": 0})
        d["in"] += int(r.get("in") or 0)
        d["out"] += int(r.get("out") or 0)
        d["est_usd"] += float(r.get("est_usd") or 0.0)
        d["calls"] += 1
    cost = {"by_model": by_model, "calls": sum(d["calls"] for d in by_model.values()),
            "in": sum(d["in"] for d in by_model.values()),
            "out": sum(d["out"] for d in by_model.values()),
            "est_usd": sum(d["est_usd"] for d in by_model.values())}

    started = float(meta.get("started") or 0.0)
    finished = meta.get("finished")
    if finished is None and events:
        finished = events[-1].get("ts")
    duration = max(0.0, float(finished or time.time()) - started) if started else 0.0

    rows = [{"ts": ev.get("ts"), "agent": ev.get("agent"),
             "type": ev.get("type"), "brief": _event_brief(ev)} for ev in events]

    data = {
        "run": run_id,
        "name": meta.get("name") or "",
        "task": _short(meta.get("task"), 200),
        "cwd": meta.get("cwd") or "",
        "status": meta.get("status") or "?",
        "started": started,
        "finished": finished,
        "duration_sec": round(duration, 1),
        "agents": [agents[a] for a in order],
        "tools": tools,
        "retries": retries,
        "failovers": failovers,
        "http_status": hist,
        "http_logged": bool(http),
        "compactions": compactions,
        "cost": cost,
        "files_changed": git_stat(meta.get("cwd")),
        "last_events": rows[-EVENT_ROWS:],
    }
    if timeline:
        data["timeline"] = rows[:TIMELINE_MAX]
    return data


def aggregate(seconds: float, now: float = None) -> dict:
    """All runs started inside the window + totals (for --since)."""
    now = time.time() if now is None else float(now)
    cutoff = now - float(seconds)
    runs = []
    for rid in J.run_ids():
        meta = J.load_run(rid) or {}
        started = float(meta.get("started") or 0.0)
        if started < cutoff:
            continue
        events = J.read_events(rid)
        usage = J.read_usage(rid)
        runs.append({
            "run": rid,
            "task": _short(meta.get("task"), 200),
            "status": meta.get("status") or "?",
            "started": started,
            "turns": sum(1 for e in events if e.get("type") == "model_response"),
            "agents": len({e.get("agent") for e in events
                           if e.get("type") == "agent_start"}),
            "est_usd": round(sum(float(r.get("est_usd") or 0.0) for r in usage), 6),
        })
    totals = {
        "runs": len(runs),
        "turns": sum(r["turns"] for r in runs),
        "agents": sum(r["agents"] for r in runs),
        "est_usd": round(sum(r["est_usd"] for r in runs), 6),
    }
    return {"since": _window_text(float(seconds)),
            "window_seconds": float(seconds), "runs": runs, "totals": totals}


# ---------------------------------------------------------------- rendering

def render_markdown(d: dict, full: bool = False, timeline: bool = False) -> str:
    lines = [f"# hydra report · {d['run']}" + (f" ({d['name']})" if d.get("name") else ""),
             f"status: {d['status']} · duration: {_duration_text(d['duration_sec'])}"
             + (f" · started: {_hms(d['started'])}" if d.get("started") else "")]
    lines.append(f"task: {_short(d.get('task'), 200)}")
    if d.get("cwd"):
        lines.append(f"cwd: {d['cwd']}")

    agents = d.get("agents") or []
    lines += ["", "## agents"]
    if not agents:
        lines.append("(no agent events)")
    for a in agents:
        pad = "  " * int(a.get("depth") or 0)
        model = ", ".join(a.get("models") or []) or "-"
        lines.append(f"{pad}- {a['id']} {a['role']} · {model} · "
                     f"{a['turns']} turns · {_tok(a['in'])} in / {_tok(a['out'])} out"
                     f" · {a['status']}")

    tools = d.get("tools") or {}
    if tools:
        shown = sorted(tools.items(), key=lambda kv: (-kv[1], kv[0]))
        text = " · ".join(f"{name} ×{n}" for name, n in shown[:12])
        if len(shown) > 12:
            text += f" · +{len(shown) - 12} more"
        lines += ["", f"tools: {text}"]

    hist = d.get("http_status") or {}
    hist_txt = " ".join(f"{k}×{v}" for k, v in
                        sorted(hist.items(), key=lambda kv: (-kv[1], kv[0])))
    source = "http.jsonl" if d.get("http_logged") else "events (no http.jsonl)"
    lines += ["", "## health",
              f"retries: {d.get('retries', 0)} · failovers: {d.get('failovers', 0)}"
              f" · compactions: {d.get('compactions', 0)}",
              f"http: {hist_txt or '(none)'}  ·  source: {source}"]

    cost = d.get("cost") or {}
    lines += ["", "## cost (est)"]
    by_model = sorted((cost.get("by_model") or {}).items(),
                      key=lambda kv: (-kv[1]["est_usd"], kv[0]))
    for m, c in by_model[:10]:
        lines.append(f"- {m}: {c['calls']} calls · {_tok(c['in'])} in / "
                     f"{_tok(c['out'])} out · ~${c['est_usd']:.4f}")
    lines.append(f"total: ~${cost.get('est_usd', 0.0):.4f} · {_tok(cost.get('in'))} in / "
                 f"{_tok(cost.get('out'))} out · {cost.get('calls', 0)} calls")

    files = d.get("files_changed")
    lines += ["", "## files changed"]
    if files is None:
        lines.append("(cwd is not a git repo — skipped)")
    elif not files:
        lines.append("(clean — no tracked changes)")
    else:
        lines += ["    " + f for f in files[:20]]
        if len(files) > 20:
            lines.append(f"    … {len(files) - 20} more")

    lines += ["", f"## last {EVENT_ROWS} events"]
    for e in d.get("last_events") or []:
        line = f"{_hms(e.get('ts'))} [{e.get('agent') or '-'}] {e.get('type')}"
        if e.get("brief"):
            line += f" {e['brief']}"
        lines.append(line)
    if not d.get("last_events"):
        lines.append("(none)")

    if timeline:
        rows = d.get("timeline") or []
        cap = TIMELINE_MAX if full else TIMELINE_ROWS
        lines += ["", "## timeline"]
        for e in rows[:cap]:
            line = f"{_hms(e.get('ts'))} [{e.get('agent') or '-'}] {e.get('type')}"
            if e.get("brief"):
                line += f" {e['brief']}"
            lines.append(line)
        if len(rows) > cap:
            lines.append(f"… {len(rows) - cap} more events (--full)")
        if not rows:
            lines.append("(none)")
    return "\n".join(lines) + "\n"


def render_since_markdown(agg: dict) -> str:
    t = agg.get("totals") or {}
    lines = [f"# hydra report · window {agg.get('since')}",
             f"{t.get('runs', 0)} runs · {_tok(t.get('turns'))} turns · "
             f"{t.get('agents', 0)} agents · est ${t.get('est_usd', 0.0):.4f}",
             "",
             "| run | task | status | turns | agents | est $ |",
             "|---|---|---|---|---|---|"]
    for r in agg.get("runs") or []:
        lines.append(f"| {r['run']} | {_md_cell(r['task'], 60)} | {r['status']} "
                     f"| {r['turns']} | {r['agents']} | {r['est_usd']:.4f} |")
    if not agg.get("runs"):
        lines.append("| | | | | | |")
    lines += ["", f"totals: {t.get('runs', 0)} runs · {t.get('turns', 0)} turns · "
                  f"{t.get('agents', 0)} agents · est ${t.get('est_usd', 0.0):.4f}"]
    return "\n".join(lines) + "\n"


def cap_lines(text: str, limit: int = REPORT_CAP, full: bool = False) -> str:
    if full:
        return text
    lines = text.splitlines()
    if len(lines) <= limit:
        return text
    keep = lines[: limit - 1]
    keep.append(f"… truncated at {limit} lines — use --full for everything")
    return "\n".join(keep) + "\n"


# ---------------------------------------------------------------- CLI

def cli(cfg, args, json_out: bool = False) -> int:
    from . import hygiene
    redact = hygiene.make_redactor(cfg)

    run_id, last, full, as_json = None, False, False, bool(json_out)
    timeline, since = False, None
    i = 0
    while i < len(args):
        a = str(args[i])
        if a == "--last":
            last = True
        elif a == "--full":
            full = True
        elif a == "--json":
            as_json = True
        elif a == "--timeline":
            timeline = True
        elif a == "--since" and i + 1 < len(args):
            i += 1
            since = args[i]
        elif a.startswith("--since="):
            since = a.split("=", 1)[1]
        elif a.startswith("-"):
            print(f"hydra report: unknown flag {a!r}")
            return 2
        else:
            run_id = a
        i += 1

    def out(text: str) -> None:
        print(redact(text) if redact else text)

    if since is not None:
        try:
            window = parse_window(since)
        except ValueError as exc:
            print(f"hydra report: {exc}")
            return 2
        agg = aggregate(window)
        if as_json:
            out(json.dumps(agg, ensure_ascii=False, indent=2))
        else:
            out(cap_lines(render_since_markdown(agg), full=full).rstrip())
        return 0

    rid = J.find_run(run_id) if run_id else None
    if run_id and not rid:
        print(f"hydra report: run not found: {run_id}")
        return 2
    if not rid and not last:
        rid = J.last_run_in(os.getcwd())
    if not rid:
        rid = (J.run_ids() or [None])[0]
    if not rid:
        print("hydra report: no runs yet — start one with:  hydra \"your task\"")
        return 2

    data = collect(rid, timeline=timeline)
    if as_json:
        out(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        md = render_markdown(data, full=full, timeline=timeline)
        out(cap_lines(md, full=full).rstrip())
    return 0
