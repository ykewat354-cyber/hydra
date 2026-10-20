"""F2 — model benchmarking and role-chain tuning (§7, scorecard).

``hydra models bench`` runs a small, deterministic task suite against one or
more models and records a scorecard in ``~/.hydra/scorecard.json``.  Fixtures
are generated in code into a fresh temporary workdir for every cell, so no
data files are needed and runs never see each other's state.

``hydra models tune`` aggregates that scorecard and proposes role chains
(reliability first, then estimated cost); ``--apply`` writes
``~/.hydra/roles.auto.toml`` which ``config.load_config`` layers after the
built-in defaults and before ``config.toml`` / project config / flags.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import config as C
from . import journal as J

TASKS = ("B1", "B2", "B3")
# Roles written by `models tune --apply`. Producer roles share the
# reliability-first chain; critic/reviewer share the diverse-family chain.
# planner/debugger/tester/scribe inherit the producer chain so every
# tool-calling role gets fixed by one apply. vision is left alone: the
# scorecard does not test image input.
ROLES = ("lead", "coder", "worker", "researcher", "critic", "summarizer",
         "reviewer", "planner", "debugger", "tester", "scribe")

B1_TASK = ("Create sum.py that prints the sum of the integers 1 to 100, "
           "run it, then finish.")
B2_TASK = "The tests fail. Fix calc.py so they pass. Do not edit the tests."
B3_TASK = ("Find every line containing the exact marker TODO: under notes/ "
           'and write them to todos.md as a numbered list sorted by filename, '
           'formatted "1. <file>: <text>".')

B2_CALC = """def add(a, b):
    return a - b


def mul(a, b):
    return a * b
"""

B2_TEST = """import unittest

from calc import add, mul


class CalcTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_mul(self):
        self.assertEqual(mul(2, 3), 6)


if __name__ == "__main__":
    unittest.main()
"""

# 6 files, 9 exact "TODO:" lines total, one file with none, plus decoys.
B3_NOTES = {
    "a.txt": [
        "TODO: refactor the parser",
        "todo: lower-case decoy",
        "TODO: add unit tests",
        "TODOS are not the marker",
        "TO DO: spaced decoy",
        "TODO: handle empty input",
    ],
    "b.txt": [
        "intro line",
        "TODO: document the CLI",
        "TODO: check exit codes",
        "todo: another decoy",
    ],
    "c.txt": [
        "TODO: rotate the logs",
        "TODOS: decoy",
        "TODO: bound the queue",
    ],
    "d.txt": [
        "TO DO: decoy",
        "TODO: retry transient failures",
    ],
    "e.txt": [
        "TODO: escape the output",
        "todo: decoy",
    ],
    "f.txt": [
        "todo: nothing exact here",
        "TODOS: still a decoy",
        "TO DO: also a decoy",
    ],
}


# ---------------------------------------------------------------- fixtures

def make_fixture(task_id: str, workdir) -> dict:
    """Write the task's starting files into *workdir* and return its metadata."""
    wd = Path(workdir)
    wd.mkdir(parents=True, exist_ok=True)
    if task_id == "B1":
        return {"task": B1_TASK, "kind": "B1"}
    if task_id == "B2":
        (wd / "calc.py").write_text(B2_CALC, encoding="utf-8")
        raw = B2_TEST.encode("utf-8")
        (wd / "test_calc.py").write_bytes(raw)
        return {"task": B2_TASK, "kind": "B2",
                "test_sha": hashlib.sha256(raw).hexdigest()}
    if task_id == "B3":
        notes = wd / "notes"
        notes.mkdir(parents=True, exist_ok=True)
        for name, lines in B3_NOTES.items():
            (notes / name).write_text("\n".join(lines) + "\n", encoding="utf-8")
        return {"task": B3_TASK, "kind": "B3", "expected": b3_expected()}
    raise ValueError(f"unknown bench task: {task_id}")


def b3_expected() -> str:
    """The exact todos.md body implied by B3_NOTES (sorted by filename)."""
    out = []
    n = 0
    for name in sorted(B3_NOTES):
        for line in B3_NOTES[name]:
            if "TODO:" in line:
                n += 1
                out.append(f"{n}. notes/{name}: {line}")
    return "\n".join(out) + "\n"


def content_diff(want: str, got: str, limit: int = 5) -> str:
    """First *limit* differing lines, expected (-) then actual (+) each."""
    want_lines = str(want).splitlines()
    got_lines = str(got).splitlines()
    out = []
    for i in range(max(len(want_lines), len(got_lines))):
        w = want_lines[i] if i < len(want_lines) else None
        g = got_lines[i] if i < len(got_lines) else None
        if w == g:
            continue
        if w is not None:
            out.append("-" + w[:200])
        if g is not None:
            out.append("+" + g[:200])
        if len(out) >= limit * 2:
            break
    return "\n".join(out[:limit * 2])


def _run_py(workdir: Path, args: list, timeout: int = 120):
    return subprocess.run([sys.executable, *args], cwd=str(workdir),
                          capture_output=True, text=True, timeout=timeout)


def check_fixture(task_id: str, workdir, meta: dict) -> tuple:
    """Return ``(ok, detail)`` for the fixture's acceptance rule."""
    wd = Path(workdir)
    for cache in wd.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    try:
        if task_id == "B1":
            p = _run_py(wd, ["sum.py"])
            out = ((p.stdout or "") + (p.stderr or "")).strip()
            return (p.returncode == 0 and out == "5050",
                    f"python sum.py -> rc={p.returncode} out={out[-200:]!r}")
        if task_id == "B2":
            p = _run_py(wd, ["-m", "unittest", "-q", "test_calc"])
            detail = ((p.stdout or "") + (p.stderr or ""))[-300:]
            tests_ok = p.returncode == 0
            try:
                now = hashlib.sha256((wd / "test_calc.py").read_bytes()).hexdigest()
            except OSError:
                return False, "test_calc.py is missing"
            same = now == meta.get("test_sha")
            return (tests_ok and same,
                    f"tests_ok={tests_ok} tests_unchanged={same} {detail}")
        if task_id == "B3":
            p = wd / "todos.md"
            if not p.is_file():
                return False, "todos.md is missing"
            got = p.read_text(encoding="utf-8", errors="replace").strip()
            want = str(meta.get("expected") or "").strip()
            if got == want:
                return True, "todos.md matches the expected output"
            diff = content_diff(want, got)
            return False, (f"todos.md mismatch ({len(got)} vs {len(want)} chars)"
                           + ("\n" + diff if diff else ""))
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return False, f"unknown bench task: {task_id}"


# ---------------------------------------------------------------- one cell

def _bench_cfg(cfg: dict, model: str) -> dict:
    c = copy.deepcopy(cfg)
    for key, value in (("loop.max_turns", 12),
                       ("loop.verify", "never"),
                       ("agents.max_agents", 1),
                       ("termux.wake_lock", False),
                       ("termux.notify_on_finish", False),
                       ("termux.notify_on_park", False)):
        C.set_path(c, key, value)
    chain = list(C.get(c, "roles.lead.chain") or [])
    C.set_path(c, "roles.lead.chain", [model] + [m for m in chain if m != model])
    return c


def run_cell(model: str, task_id: str, cfg: dict, *, keep: bool = False) -> dict:
    """Run one (model, task) cell in a fresh temp cwd + isolated HYDRA_HOME."""
    started = time.time()
    wd = Path(tempfile.mkdtemp(prefix="hydra_bench_wd_"))
    home = Path(tempfile.mkdtemp(prefix="hydra_bench_home_"))
    old_home = os.environ.get("HYDRA_HOME")
    real_home = C.home()
    os.environ["HYDRA_HOME"] = str(home)
    for name in ("key", "prices.toml"):
        try:
            src = real_home / name
            if src.is_file():
                shutil.copy2(src, home / name)
        except Exception:
            pass
    rec = {"ts": round(time.time(), 3), "model": model, "task": task_id,
           "ok": False, "status": "error", "turns": 0, "in": 0, "out": 0,
           "est_usd": 0.0, "seconds": 0.0, "parsed_from_text": 0,
           "tool_errors": 0, "run_id": "", "error": "", "detail": ""}
    try:
        meta = make_fixture(task_id, wd)
        c = _bench_cfg(cfg, model)
        from .runtime import Runtime
        rt = Runtime(c, cwd=str(wd), quiet=True)
        try:
            code, report = rt.run(meta["task"], name=f"bench-{task_id}",
                                  lead_model=model)
            rid = rt.run_id or ""
        finally:
            rt.close()
        rec["run_id"] = rid
        rec["status"] = str((report or {}).get("status") or
                            ("done" if code == 0 else f"exit {code}"))
        events = J.read_events(rid)
        rec["turns"] = sum(1 for e in events if e.get("type") == "model_response")
        rec["parsed_from_text"] = sum(int(e.get("n") or 0) for e in events
                                      if e.get("type") == "parsed_from_text")
        rec["tool_errors"] = sum(
            1 for e in events if e.get("type") == "tool_result"
            and str(e.get("preview") or "").startswith("ERROR:"))
        usage = J.read_usage(rid)
        rec["in"] = sum(int(r.get("in") or 0) for r in usage)
        rec["out"] = sum(int(r.get("out") or 0) for r in usage)
        rec["est_usd"] = round(sum(float(r.get("est_usd") or 0.0)
                                   for r in usage), 6)
        ok, detail = check_fixture(task_id, wd, meta)
        rec["ok"] = bool(ok)
        rec["detail"] = detail[:400]
        if keep:
            rec["workdir"] = str(wd)
            rec["home"] = str(home)
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"[:400]
    finally:
        rec["seconds"] = round(time.time() - started, 2)
        if old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = old_home
        if not keep:
            shutil.rmtree(wd, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)
    return rec


# ---------------------------------------------------------------- scorecard

def scorecard_path() -> Path:
    return C.home() / "scorecard.json"


def load_scorecard(path=None) -> dict:
    p = Path(path) if path else scorecard_path()
    data = {"version": 1, "updated": 0.0, "cells": []}
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data["version"] = int(raw.get("version") or 1)
            data["updated"] = float(raw.get("updated") or 0.0)
            cells = raw.get("cells")
            if isinstance(cells, list):
                data["cells"] = [c for c in cells if isinstance(c, dict)]
    except Exception:
        pass
    return data


def merge_scorecard(data: dict, new_cells: list, *, cap: int = 5000) -> dict:
    cells = list(data.get("cells") or [])
    cells.extend(c for c in new_cells if isinstance(c, dict))
    if cap and len(cells) > cap:
        cells = cells[-cap:]
    data["version"] = int(data.get("version") or 1)
    data["updated"] = round(time.time(), 3)
    data["cells"] = cells
    return data


def save_scorecard(data: dict, path=None) -> Path:
    p = Path(path) if path else scorecard_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    os.replace(tmp, p)
    return p


# ---------------------------------------------------------------- bench run

def default_models(cfg: dict) -> list:
    try:
        from .models import ModelRegistry
        reg = ModelRegistry(cfg)
        ok = [e["model"] for e in reg.list_all()
              if e.get("status_now") in ("ok", "paid")]
        if ok:
            return ok
    except Exception:
        pass
    return list(C.get(cfg, "roles.lead.chain") or [])


def run(cfg: dict, models: list, tasks: list, *, runs: int = 1,
        max_usd: float = None, progress=None) -> dict:
    """Run every cell, honouring the budget cap, and merge into the scorecard."""
    models = [str(m) for m in models if str(m).strip()]
    tasks = [str(t).upper() for t in tasks if str(t).upper() in TASKS]
    runs = max(1, int(runs or 1))
    cells = []
    spent = 0.0
    aborted = False
    total = len(models) * len(tasks) * runs
    for model in models:
        for task_id in tasks:
            for n in range(runs):
                if max_usd is not None and spent >= float(max_usd):
                    aborted = True
                    break
                rec = run_cell(model, task_id, cfg)
                rec["run"] = n + 1
                cells.append(rec)
                spent += float(rec.get("est_usd") or 0.0)
                if progress:
                    try:
                        progress(rec)
                    except Exception:
                        pass
            if aborted:
                break
        if aborted:
            break
    data = merge_scorecard(load_scorecard(), cells)
    path = save_scorecard(data)
    skipped = max(0, total - len(cells))
    return {"cells": cells, "spent": round(spent, 6), "aborted": aborted,
            "skipped": skipped, "scorecard": str(path)}


def _fmt_cell(rec: dict) -> str:
    mark = "ok " if rec.get("ok") else "FAIL"
    return (f"  {mark} {rec.get('model', '?'):<22} {rec.get('task', '?'):<3} "
            f"turns={rec.get('turns', 0):<2} "
            f"tok={rec.get('in', 0)}/{rec.get('out', 0)} "
            f"${float(rec.get('est_usd') or 0.0):.4f} "
            f"{float(rec.get('seconds') or 0.0):.1f}s "
            f"text={rec.get('parsed_from_text', 0)} "
            f"terr={rec.get('tool_errors', 0)}")


def _flag(args: list, name: str, default=None):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            return args[i + 1]
    return default


def cli_bench(cfg: dict, args: list) -> int:
    raw_models = _flag(args, "--models")
    models = ([m.strip() for m in raw_models.split(",") if m.strip()]
              if raw_models else default_models(cfg))
    raw_tasks = _flag(args, "--tasks", ",".join(TASKS))
    tasks = [t.strip().upper() for t in raw_tasks.split(",") if t.strip()]
    bad = [t for t in tasks if t not in TASKS]
    if bad:
        print(f"hydra: unknown bench task(s): {', '.join(bad)} "
              f"(choose from {', '.join(TASKS)})")
        return 2
    if not models:
        print("hydra: no models to bench — pass --models a,b")
        return 2
    try:
        runs = max(1, int(_flag(args, "--runs", 1)))
    except ValueError:
        print("hydra: --runs needs an integer")
        return 2
    raw_cap = _flag(args, "--max-usd")
    try:
        max_usd = float(raw_cap) if raw_cap is not None else None
    except ValueError:
        print("hydra: --max-usd needs a number")
        return 2

    print(f"bench: {len(models)} model(s) x {len(tasks)} task(s) x {runs} run(s)")
    res = run(cfg, models, tasks, runs=runs, max_usd=max_usd,
              progress=lambda r: print(_fmt_cell(r), flush=True))
    for rec in res["cells"]:
        if rec.get("error"):
            print(f"    error: {rec['error']}")
        elif not rec.get("ok") and rec.get("detail"):
            print(f"    detail: {rec['detail'][:200]}")
    if res["aborted"]:
        print(f"bench: budget cap ${max_usd:.4f} reached "
              f"(spent ${res['spent']:.4f}); skipped {res['skipped']} cell(s)")
    print(f"bench: spent ~${res['spent']:.4f}; scorecard -> {res['scorecard']}")
    return 0 if res["cells"] and all(c.get("ok") for c in res["cells"]) else 1


# ---------------------------------------------------------------- tune

def aggregate(cells: list) -> dict:
    agg = {}
    for rec in cells:
        model = str(rec.get("model") or "").strip()
        if not model:
            continue
        d = agg.setdefault(model, {"runs": 0, "ok": 0, "est_usd": 0.0,
                                   "seconds": 0.0})
        d["runs"] += 1
        d["ok"] += 1 if rec.get("ok") else 0
        d["est_usd"] += float(rec.get("est_usd") or 0.0)
        d["seconds"] += float(rec.get("seconds") or 0.0)
    for d in agg.values():
        d["pass_rate"] = d["ok"] / d["runs"] if d["runs"] else 0.0
        d["avg_usd"] = d["est_usd"] / d["runs"] if d["runs"] else 0.0
        d["avg_seconds"] = d["seconds"] / d["runs"] if d["runs"] else 0.0
    return agg


def rank_models(agg: dict) -> list:
    """Reliability first, then average estimated cost, then name."""
    return sorted(agg, key=lambda m: (-agg[m]["pass_rate"],
                                      agg[m]["avg_usd"], m))


def family(model: str) -> str:
    return str(model).split(":", 1)[0].strip().lower()


def propose(cells: list) -> dict:
    agg = aggregate(cells)
    ranked = rank_models(agg)
    if not ranked:
        return {}
    chain = ranked[:3]
    coder_family = family(chain[0])
    diff = next((m for m in ranked if family(m) != coder_family), None)
    critic = (([diff] + [m for m in ranked if m != diff])[:3]
              if diff else list(chain))
    out = {}
    for role in ROLES:
        out[role] = list(critic) if role in ("critic", "reviewer") else list(chain)
    return out


def render_auto_toml(proposals: dict) -> str:
    lines = ["# generated by `hydra models tune --apply`; safe to delete",
             "# loads after built-in defaults, before config.toml / project / flags",
             ""]
    for role in ROLES:
        chain = proposals.get(role) or []
        if not chain:
            continue
        lines.append(f"[roles.{role}]")
        lines.append("chain = " + json.dumps(chain))
        lines.append("")
    return "\n".join(lines)


def apply_proposals(proposals: dict, path=None) -> Path:
    p = Path(path) if path else (C.home() / "roles.auto.toml")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_auto_toml(proposals), encoding="utf-8")
    return p


def cli_tune(cfg: dict, args: list) -> int:
    data = load_scorecard()
    cells = list(data.get("cells") or [])
    if not cells:
        print("scorecard is empty — run: hydra models bench")
        return 1
    agg = aggregate(cells)
    proposals = propose(cells)
    print("scorecard models (reliability first, then cost):")
    for model in rank_models(agg):
        d = agg[model]
        print(f"  {model:<22} runs={d['runs']:<3} "
              f"pass={d['pass_rate'] * 100:5.1f}%  "
              f"avg=${d['avg_usd']:.4f}  avg={d['avg_seconds']:.1f}s")
    print("\nproposed chains:")
    for role in ROLES:
        chain = proposals.get(role) or []
        if chain:
            print(f"  {role:<11} -> {' > '.join(chain)}")
    if "--apply" in args:
        p = apply_proposals(proposals)
        print(f"\nwrote {p}")
        print("(layered after defaults and before config.toml / project / flags)")
    else:
        print("\n(dry run — pass --apply to write ~/.hydra/roles.auto.toml)")
    return 0
