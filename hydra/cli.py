"""HYDRA CLI: run tasks, resume, inspect runs, doctor.

Command surface (grows by phase):
  hydra "task"            autonomous run
  hydra run task.md       (same; tasks may be free text)
  hydra resume [RUN]      continue a killed/parked run
  hydra ls | tail | tree | usage | kill | doctor
  hydra models ...        model discovery (models phase)
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sys
import time

from . import __version__
from . import config as C
from . import journal as J

COMMANDS = {"run", "resume", "ls", "tail", "tree", "usage", "doctor", "kill",
            "models", "undo", "init", "map", "eval", "daemon", "schedule",
            "help", "version"}

HELP = """HYDRA — autonomous multi-agent harness for Termux (Ollama Cloud).

usage:  hydra [flags] "task"        start an autonomous run
        hydra <command> [args]

commands:
  resume [RUN|NAME]     continue a killed / parked / interrupted run
  ls                    list runs
  tail [RUN] [-a AGENT]  recent events of a run
  tree [RUN]            agent tree with status and tokens
  usage [RUN]           token ledger + cost estimate (all runs if omitted)
  models probe|ls|pin   discover models your key can use
  doctor                environment checks
  kill RUN              stop a run

flags:
  --set key=value       config override (repeatable)
  --lead MODEL          pin the lead model
  --name NAME           name this run
  --max-inflight N      LLM gate slots
  --quiet  --json  --wait-reset
"""


class Opts:
    def __init__(self):
        self.quiet = False
        self.json_out = False
        self.sets = []
        self.lead = None
        self.name = None
        self.max_inflight = None
        self.wait_reset = False
        self.detach = False
        self.continue_ = False
        self.interactive = False


def _parse(argv):
    o = Opts()
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--set":
            i += 1
            if i >= len(argv):
                raise SystemExit("hydra: --set needs key=value")
            o.sets.append(argv[i])
        elif a.startswith("--set="):
            o.sets.append(a[6:])
        elif a in ("--quiet", "-q"):
            o.quiet = True
        elif a == "--json":
            o.json_out = True
        elif a == "--lead":
            i += 1
            o.lead = argv[i]
        elif a == "--name":
            i += 1
            o.name = argv[i]
        elif a == "--max-inflight":
            i += 1
            o.max_inflight = argv[i]
        elif a == "--wait-reset":
            o.wait_reset = True
        elif a == "--detach":
            o.detach = True
        elif a == "--continue":
            o.continue_ = True
        elif a in ("-i", "--interactive"):
            o.interactive = True
        elif a in ("--version", "-V"):
            return o, ["version"]
        elif a in ("--help", "-h"):
            return o, ["help"]
        else:
            rest.append(a)
        i += 1
    return o, rest


def _shorten(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        o, rest = _parse(argv)
    except SystemExit as exc:
        print(exc)
        return 2
    C.ensure_home()
    overrides = list(o.sets)
    if o.max_inflight:
        overrides.append(f"api.max_inflight={o.max_inflight}")
    if o.wait_reset:
        overrides.append("park.wait_reset=true")
    cfg = C.load_config(overrides=overrides)

    if rest and rest[0] == "version":
        print(f"hydra {__version__}")
        return 0
    if not rest or rest[0] == "help":
        print(HELP)
        return 0
    cmd = rest[0] if rest[0] in COMMANDS else None

    if cmd == "doctor":
        return cmd_doctor(cfg)
    if cmd == "ls":
        return cmd_ls()
    if cmd == "usage":
        return cmd_usage(rest[1:])
    if cmd == "tail":
        return cmd_tail(rest[1:])
    if cmd == "tree":
        return cmd_tree(rest[1:])
    if cmd == "kill":
        return cmd_kill(rest[1:])
    if cmd == "models":
        try:
            from . import models as Models
        except ImportError:
            print("hydra: 'models' arrives with the next build phase.")
            return 2
        return Models.cli(cfg, rest[1:])
    if cmd == "resume":
        return cmd_resume(cfg, rest[1:], o)
    if cmd in ("undo", "init", "map", "eval", "daemon", "schedule"):
        print(f"hydra: '{cmd}' arrives with a later build phase.")
        return 2

    task = " ".join(rest)
    if not task.strip():
        print(HELP)
        return 0
    return cmd_run(cfg, task, o)


# ------------------------------------------------------------------ run

def cmd_run(cfg, task, o) -> int:
    from .runtime import Runtime
    rt = Runtime(cfg, quiet=o.quiet, json_out=o.json_out)
    rt.wake_lock()
    code, report = 1, {}
    try:
        code, report = rt.run(task, name=o.name, lead_model=o.lead)
    finally:
        rt.wake_unlock()
        _footer(rt, report, code)
        rt.close()
    return code


def cmd_resume(cfg, args, o) -> int:
    from .runtime import Runtime
    which = args[0] if args else None
    rt = Runtime(cfg, quiet=o.quiet, json_out=o.json_out)
    rt.wake_lock()
    code, report = 1, {}
    try:
        code, report = rt.resume_run(which)
    finally:
        rt.wake_unlock()
        _footer(rt, report, code)
        rt.close()
    return code


def _footer(rt, report, code) -> None:
    tot = rt.usage_totals()
    turns = sum(getattr(a, "turns", 0) for a in rt.agents.values())
    started = rt.run_meta.get("started") or time.time()
    dur = max(0, int(time.time() - started))
    mins, secs = divmod(dur, 60)
    status = (report or {}).get("status") or \
        {"75": "parked", "130": "interrupted"}.get(str(code), "failed" if code else "done")
    if rt.ui.json_out:
        print(json.dumps({"type": "run_end", "status": status, "exit_code": code,
                          "turns": turns, "agents": len(rt.agents),
                          "in": tot["in"], "out": tot["out"],
                          "est_usd": round(tot["est_usd"], 6)}, ensure_ascii=False))
        return
    summary = (report or {}).get("summary")
    if summary and not rt.ui.quiet:
        print("\n" + summary.strip())
    print(f"\n{status} · {turns} turns · {len(rt.agents)} agents · "
          f"{tot['in'] // 1000}k in / {tot['out'] // 1000}k out · "
          f"est ${tot['est_usd']:.4f} · {mins:02d}:{secs:02d}")


# ------------------------------------------------------------------ inspect

def _resolve_run(args):
    if args:
        rid = J.find_run(args[0])
        if not rid:
            print(f"hydra: run not found: {args[0]}")
        return rid
    rid = J.last_run_in(os.getcwd())
    if rid:
        return rid
    return (J.run_ids() or [None])[0]


def cmd_ls() -> int:
    runs = J.run_ids()
    if not runs:
        print("no runs yet — start one with:  hydra \"your task\"")
        return 0
    for rid in runs[:30]:
        d = J.load_run(rid) or {}
        when = time.strftime("%m-%d %H:%M", time.localtime(d.get("started") or 0))
        label = d.get("name") or _shorten(d.get("task"), 46)
        print(f"{rid:<16} {str(d.get('status', '?')):<10} {when}  {label}")
    return 0


def cmd_tail(args) -> int:
    agent = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "-a" and i + 1 < len(args):
            agent = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    rid = _resolve_run(rest)
    if not rid:
        return 2
    events = J.read_events(rid, agent=agent)
    for ev in events[-40:]:
        hh = time.strftime("%H:%M:%S", time.localtime(ev.get("ts") or 0))
        tag = str(ev.get("agent") or "-")
        body = {k: v for k, v in ev.items()
                if k not in ("ts", "run", "agent", "seq", "type")}
        print(f"{hh} [{tag:<4}] {ev.get('type'):<14} {_shorten(body, 90)}")
    return 0


def cmd_tree(args) -> int:
    rid = _resolve_run(args)
    if not rid:
        return 2
    infos = {}
    order = []
    for ev in J.read_events(rid):
        t = ev.get("type")
        a = ev.get("agent")
        if t == "agent_start" and a not in infos:
            infos[a] = {"role": ev.get("role"), "depth": int(ev.get("depth") or 0),
                        "parent": ev.get("parent"), "status": "running",
                        "in": 0, "out": 0}
            order.append(a)
        elif t == "model_response" and a in infos:
            infos[a]["in"] += int(ev.get("in_tokens") or 0)
            infos[a]["out"] += int(ev.get("out_tokens") or 0)
        elif t == "report" and a in infos:
            infos[a]["status"] = (ev.get("report") or {}).get("status", "?")
    if not infos:
        print(f"{rid}: no agent events")
        return 0
    print(f"{rid}")
    for a in order:
        d = infos[a]
        print("  " * d["depth"] +
              f"{a} {d['role']:<10} {d['status']:<8} {d['in'] // 1000}k in / {d['out'] // 1000}k out")
    return 0


def cmd_usage(args) -> int:
    rid = None
    if args:
        rid = J.find_run(args[0])
        if not rid:
            print(f"hydra: run not found: {args[0]}")
            return 2
    recs = J.read_usage(rid)
    if not recs:
        print("no usage recorded yet")
        return 0
    by_model = {}
    for r in recs:
        d = by_model.setdefault(r.get("model", "?"), {"in": 0, "out": 0, "est": 0.0, "n": 0})
        d["in"] += int(r.get("in", 0))
        d["out"] += int(r.get("out", 0))
        d["est"] += float(r.get("est_usd", 0.0))
        d["n"] += 1
    scope = rid or "all runs"
    print(f"usage ({scope}):")
    for m, d in sorted(by_model.items()):
        print(f"  {m:<24} {d['n']:>3} calls  {d['in'] // 1000:>5}k in  "
              f"{d['out'] // 1000:>4}k out  ~${d['est']:.4f}")
    tin = sum(d["in"] for d in by_model.values())
    tout = sum(d["out"] for d in by_model.values())
    est = sum(d["est"] for d in by_model.values())
    print(f"  total: {tin // 1000}k in / {tout // 1000}k out · est ${est:.4f}")
    return 0


def cmd_kill(args) -> int:
    rid = J.find_run(args[0]) if args else None
    if not rid:
        print("usage: hydra kill RUN_ID")
        return 2
    d = J.load_run(rid) or {}
    pid = d.get("pid")
    result = "marked killed (process not found)"
    if pid:
        try:
            os.killpg(int(pid), signal.SIGKILL)
            result = f"killed process group {pid}"
        except Exception:
            try:
                os.kill(int(pid), signal.SIGKILL)
                result = f"killed pid {pid}"
            except Exception:
                pass
    d["status"] = "killed"
    J.save_run_atomic(rid, d)
    print(f"{rid}: {result}")
    return 0


# ------------------------------------------------------------------ doctor

def _termux_ok() -> bool:
    import subprocess
    if not shutil.which("termux-battery-status"):
        return False
    try:
        p = subprocess.run(["termux-battery-status"], capture_output=True,
                           text=True, timeout=3)
        return p.returncode == 0
    except Exception:
        return False


def cmd_doctor(cfg) -> int:
    from .client import OllamaCloud
    ok = True

    def chk(label, cond, hint=""):
        nonlocal ok
        mark = "ok  " if cond else "FAIL"
        line = f"[{mark}] {label}"
        if not cond and hint:
            line += f"  -> {hint}"
        print(line)
        ok = ok and bool(cond)

    chk("python >= 3.11", sys.version_info >= (3, 11), "pkg install python")
    chk("git", shutil.which("git"), "pkg install git")
    chk("ripgrep (rg)", shutil.which("rg"), "pkg install ripgrep")
    chk("jq (optional)", shutil.which("jq"), "pkg install jq")
    key = C.load_key(cfg)
    chk("Ollama API key", bool(key),
        "echo your_key > ~/.hydra/key && chmod 600 ~/.hydra/key")
    if key:
        host = C.get(cfg, "api.host", "https://ollama.com")
        t0 = time.time()
        try:
            cli = OllamaCloud(host, key, 8, 15)
            tags = cli.tags()
            ms = int((time.time() - t0) * 1000)
            chk(f"api reachable ({host}, {ms} ms, {len(tags)} models)", True)
        except Exception as exc:
            chk(f"api reachable ({host})", False, f"{type(exc).__name__}: {exc}")
    chk("termux-api battery probe", _termux_ok(), "pkg install termux-api + Termux:API app")
    try:
        probe = C.home() / ".doctor"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        chk(f"~/.hydra writable ({C.home()})", True)
    except Exception as exc:
        chk("~/.hydra writable", False, str(exc))
    print("\ndoctor: " + ("all good" if ok else "fix the FAIL lines above"))
    return 0 if ok else 1
