"""Eval harness (§19.11).

``evals/<suite>/<task>.toml`` fields::

    task        = "…"                  # the prompt
    setup       = "…"                  # shell, runs in the temp workdir first
    check       = "…"                  # shell; exit 0 = pass
    max_turns   = 12
    fake_script = "replay.json"        # node scripts for tests/fake_ollama.py
    models      = ["gpt-oss:120b"]     # fake /api/tags entries
    chains      = { lead = ["m1", "m2"] }   # optional role chains (failover tests)
    kill_after  = 1.5                  # optional: SIGKILL then `hydra resume`
    expect_events = ["compact"]
    expect_tool_result = [{ tool = "echoer", contains = "PLUGIN-OK" }]

``hydra eval [suite]`` runs every task in a temp dir against the fake server
using the scripted replays (zero credits). ``--real`` uses the live API and
prints the estimated spend. ``--model role=model`` overrides a role chain.
Results are saved to ``evals/results/<timestamp>.json``.
"""
from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

from . import config as C

REPO_ROOT = Path(__file__).resolve().parents[1]
ROLES = ("lead", "worker", "researcher", "coder", "reviewer", "summarizer",
         "critic", "planner", "vision", "debugger", "tester", "scribe")
DEFAULT_MODEL = "gpt-oss:120b"


# ------------------------------------------------------------------ discovery

def eval_roots() -> list:
    roots = []
    for base in (Path.cwd() / "evals", C.home() / "evals"):
        try:
            if base.is_dir() and base.resolve() not in [r.resolve() for r in roots]:
                roots.append(base)
        except Exception:
            continue
    return roots


def load_tasks(suite=None) -> list:
    tasks = []
    seen = set()
    for root in eval_roots():
        for sdir in sorted(p for p in root.iterdir() if p.is_dir()):
            if sdir.name == "results":
                continue
            if suite and sdir.name != suite:
                continue
            for tf in sorted(sdir.glob("*.toml")):
                key = (sdir.name, tf.stem)
                if key in seen:
                    continue
                seen.add(key)
                try:
                    data = tomllib.loads(tf.read_text(encoding="utf-8"))
                except Exception as exc:
                    tasks.append({"name": tf.stem, "suite": sdir.name,
                                  "file": tf, "load_error": f"{type(exc).__name__}: {exc}"})
                    continue
                data["name"] = tf.stem
                data["suite"] = sdir.name
                data["file"] = tf
                data["root"] = root
                tasks.append(data)
    return tasks


# ------------------------------------------------------------------ helpers

def _bash() -> str:
    from .tools.shell import _bash as b
    return b()


def _apply(cfg: dict, pairs) -> dict:
    for key, value in C.parse_overrides(pairs):
        C.set_path(cfg, key, value)
    return cfg


def _overrides(task: dict, srv_url=None, model_pairs=()) -> list:
    ov = [
        "termux.wake_lock=false",
        "termux.notify_on_finish=false",
        "termux.notify_on_park=false",
        "loop.verify=never",
        "journal.fsync=turn",
        f"loop.max_turns={int(task.get('max_turns') or 12)}",
    ]
    if srv_url:
        ov.append(f"api.host={srv_url}")
    chains = {}
    for role in ROLES:
        chains[role] = [DEFAULT_MODEL]
    for role, chain in (task.get("chains") or {}).items():
        chains[str(role)] = list(chain)
    for role, model in model_pairs:
        chains[str(role)] = [str(model)]
    for role, chain in chains.items():
        ov.append(f"roles.{role}.chain={json.dumps(chain)}")
    for key in ("loop.working_ctx", "loop.compact_at", "loop.compact_to"):
        if task.get(key.split(".")[1]):
            ov.append(f"{key}={task[key.split('.')[1]]}")
    return ov


def _child_env(home: Path, extra=None) -> dict:
    env = dict(os.environ)
    env["HYDRA_HOME"] = str(home)
    pp = env.get("PYTHONPATH", "")
    parts = pp.split(os.pathsep) if pp else []
    if str(REPO_ROOT) not in parts:
        env["PYTHONPATH"] = os.pathsep.join([str(REPO_ROOT)] + parts) if parts else str(REPO_ROOT)
    if extra:
        env.update({str(k): str(v) for k, v in extra.items()})
    return env


def _replay(task: dict):
    path = Path(task["file"]).parent / str(task.get("fake_script") or "fake.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    tags = list(data.get("tags") or (data.get("models") or {}).keys())
    if not tags:
        tags = [DEFAULT_MODEL]
    return tags, data.get("models") or {}


def _subst(obj, mapping):
    """Replace {HOME} / {CWD} placeholders inside a replay structure."""
    if isinstance(obj, str):
        for key, value in mapping.items():
            obj = obj.replace(key, value)
        return obj
    if isinstance(obj, list):
        return [_subst(x, mapping) for x in obj]
    if isinstance(obj, dict):
        return {k: _subst(v, mapping) for k, v in obj.items()}
    return obj


def _events(run_dir: Path) -> list:
    out = []
    f = run_dir / "events.jsonl"
    try:
        for line in f.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    except Exception:
        pass
    return out


def _usage(run_dir: Path) -> dict:
    tin = tout = calls = 0
    est = 0.0
    try:
        for line in (run_dir / "usage.jsonl").read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            tin += int(rec.get("in") or 0)
            tout += int(rec.get("out") or 0)
            est += float(rec.get("est_usd") or 0.0)
            calls += 1
    except Exception:
        pass
    return {"in": tin, "out": tout, "calls": calls, "est_usd": round(est, 6)}


def _latest_run(home: Path):
    runs = home / "runs"
    dirs = [p for p in runs.iterdir() if (p / "run.json").is_file()] if runs.is_dir() else []
    if not dirs:
        return None
    d = max(dirs, key=lambda p: (p / "run.json").stat().st_mtime)
    try:
        meta = json.loads((d / "run.json").read_text(encoding="utf-8"))
    except Exception:
        meta = {}
    return d, meta


# ------------------------------------------------------------------ one task

def run_task(base_cfg: dict, task: dict, *, real=False, model_pairs=(),
             keep=False, verbose=True) -> dict:
    name = f"{task.get('suite', '?')}/{task.get('name', '?')}"
    started = time.time()
    rec = {"suite": task.get("suite"), "task": task.get("name"),
           "name": name, "pass": False, "status": "error", "turns": 0,
           "in": 0, "out": 0, "calls": 0, "est_usd": 0.0,
           "wall_s": 0.0, "error": "", "check": ""}
    if task.get("load_error"):
        rec["error"] = task["load_error"]
        return rec
    workdir = Path(tempfile.mkdtemp(prefix="hydra_eval_wd_"))
    home = Path(tempfile.mkdtemp(prefix="hydra_eval_home_"))
    srv = None
    run_dir = None
    old_home = os.environ.get("HYDRA_HOME")
    os.environ["HYDRA_HOME"] = str(home)  # isolate in-process runs too
    try:
        env = _child_env(home)
        if task.get("setup"):
            p = subprocess.run([_bash(), "-lc", str(task["setup"])], cwd=workdir,
                               env=env, capture_output=True, text=True, timeout=120)
            if p.returncode != 0:
                rec["error"] = "setup failed: " + ((p.stdout or "") + (p.stderr or ""))[-800:]
                return rec
        srv_url = None
        if not real:
            from tests.fake_ollama import FakeOllama
            tags, models = _replay(task)
            srv = FakeOllama(tags=tags).start()
            mapping = {"{HOME}": str(home), "{CWD}": str(workdir)}
            for model, nodes in models.items():
                srv.add(model, *_subst(nodes, mapping))
            srv_url = srv.url
        ov = _overrides(task, srv_url=srv_url, model_pairs=model_pairs)
        cfg = _apply(copy.deepcopy(base_cfg), ov)
        timeout = float(task.get("timeout") or 300)
        kill_after = task.get("kill_after")
        if kill_after and not real:
            sets = []
            for item in ov:
                sets += ["--set", item]
            child_env = _child_env(home)
            cmd = [sys.executable, "-m", "hydra"] + sets + [str(task.get("task") or "")]
            proc = subprocess.Popen(cmd, cwd=workdir, env=child_env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
            # Start the kill timer only after this run has actually created
            # its run directory.  A loaded phone can take longer than
            # kill_after just to import Python; killing before run_start
            # leaves nothing for `hydra resume` and makes the eval flaky.
            run_deadline = time.time() + float(kill_after)
            startup_deadline = time.time() + max(10.0, float(kill_after) * 10)
            saw_run = False
            runs_dir = home / "runs"
            while proc.poll() is None:
                if not saw_run:
                    try:
                        saw_run = runs_dir.is_dir() and any(runs_dir.iterdir())
                    except Exception:
                        saw_run = False
                    if saw_run:
                        run_deadline = time.time() + float(kill_after)
                if saw_run and time.time() >= run_deadline:
                    break
                if time.time() >= startup_deadline:
                    break
                time.sleep(0.1)
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=20)
            except Exception:
                pass
            try:
                proc.stdout.close()
            except Exception:
                pass
            resume = subprocess.run([sys.executable, "-m", "hydra"] + sets + ["resume"],
                                    cwd=workdir, env=child_env,
                                    capture_output=True, text=True, timeout=timeout)
            rec["status"] = "done" if resume.returncode == 0 else f"exit {resume.returncode}"
            rec["cli_output"] = ((resume.stdout or "") + (resume.stderr or ""))[-1500:]
            run_dir, meta = _latest_run(home)
            rec["status"] = (meta or {}).get("status") or rec["status"]
        else:
            from .runtime import Runtime
            rt = Runtime(cfg, cwd=workdir, quiet=True)
            try:
                code, report = rt.run(str(task.get("task") or ""),
                                      name=f"eval-{task.get('name')}")
                rec["status"] = (report or {}).get("status") or ("done" if code == 0 else f"exit {code}")
                rec["summary"] = (report or {}).get("summary") or ""
                rid = rt.run_id
            finally:
                rt.close()
            run_dir = home / "runs" / str(rid) if rid else None
        if run_dir is not None and run_dir.is_dir():
            evs = _events(run_dir)
            rec["turns"] = sum(1 for e in evs if e.get("type") == "model_response")
            u = _usage(run_dir)
            rec.update({k: u[k] for k in ("in", "out", "calls", "est_usd")})
            types = {e.get("type") for e in evs}
            missing = [t for t in (task.get("expect_events") or []) if t not in types]
            if missing:
                rec["error"] = f"missing events: {', '.join(missing)}"
                return rec
            for want in task.get("expect_tool_result") or []:
                tool = str(want.get("tool") or "")
                needle = str(want.get("contains") or "")
                hit = any(e.get("type") == "tool_result" and e.get("tool") == tool
                          and needle in str(e.get("preview") or "") for e in evs)
                if not hit:
                    rec["error"] = f"tool result {tool!r} missing {needle!r}"
                    return rec
        check = task.get("check")
        if check:
            p = subprocess.run([_bash(), "-lc", str(check)], cwd=workdir,
                               env=_child_env(home), capture_output=True,
                               text=True, timeout=120)
            rec["check"] = ((p.stdout or "") + (p.stderr or ""))[-800:]
            rec["pass"] = p.returncode == 0 and str(rec["status"]) in ("done", "partial")
        else:
            rec["pass"] = str(rec["status"]) in ("done", "partial")
    except Exception as exc:
        rec["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        rec["wall_s"] = round(time.time() - started, 2)
        if old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = old_home
        if srv is not None:
            try:
                srv.stop()
            except Exception:
                pass
        if not keep:
            import shutil
            shutil.rmtree(workdir, ignore_errors=True)
            shutil.rmtree(home, ignore_errors=True)
        else:
            rec["workdir"] = str(workdir)
            rec["home"] = str(home)
    return rec


# ------------------------------------------------------------------ suite

def run(cfg: dict, suite=None, *, real=False, model_pairs=(), keep=False,
        quiet=False) -> list:
    tasks = load_tasks(suite)
    if not tasks:
        print("hydra eval: no tasks found (looked in ./evals and ~/.hydra/evals)")
        return []
    if real:
        print("hydra eval: --real uses the live API and spends credits. "
              "Per-task usage is printed below; total estimated spend at the end.")
    results = []
    for task in tasks:
        rec = run_task(cfg, task, real=real, model_pairs=model_pairs,
                       keep=keep, verbose=not quiet)
        results.append(rec)
        if not quiet:
            mark = "PASS" if rec["pass"] else "FAIL"
            extra = rec.get("error") or (rec.get("check") or "").strip().splitlines()[-1:] or ""
            extra = extra[0] if isinstance(extra, list) and extra else (extra or "")
            print(f"{mark} {rec['name']:<28} {rec['wall_s']:>6.1f}s  "
                  f"turns={rec['turns']:<3} in={rec['in']:<6} out={rec['out']:<6} "
                  f"${rec['est_usd']:.4f}"
                  + (f"  [{extra[:70]}]" if extra and not rec["pass"] else ""))
    passed = sum(1 for r in results if r["pass"])
    total_est = sum(r["est_usd"] for r in results)
    print(f"\n{passed}/{len(results)} passed · est ${total_est:.4f}")
    if real:
        print(f"estimated spend: ${total_est:.4f} (actual billing may differ)")
    root = tasks[0].get("root") if tasks else None
    if root is not None:
        out_dir = Path(root) / "results"
        try:
            out_dir.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            payload = {
                "ts": stamp, "real": bool(real),
                "suite": suite, "model_overrides": [list(p) for p in model_pairs],
                "passed": passed, "total": len(results),
                "est_usd": total_est,
                "tasks": [{k: v for k, v in r.items()
                           if k not in ("workdir", "home")} for r in results],
            }
            (out_dir / f"{stamp}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
            print(f"results saved to {out_dir / (stamp + '.json')}")
        except Exception as exc:
            print(f"hydra eval: could not save results: {exc}")
    return results


def cli(cfg: dict, args: list) -> int:
    suite = None
    real = False
    keep = False
    model_pairs = []
    i = 0
    while i < len(args):
        a = str(args[i])
        if a == "--real":
            real = True
        elif a == "--keep":
            keep = True
        elif a in ("--model", "-m") and i + 1 < len(args):
            i += 1
            role, sep, model = str(args[i]).partition("=")
            if not sep:
                role, model = "lead", str(args[i])
            model_pairs.append((role.strip(), model.strip()))
        elif a.startswith("--model="):
            role, sep, model = a.split("=", 1)[1].partition("=")
            model_pairs.append((role.strip() or "lead", model.strip() or role.strip()))
        elif a.startswith("-"):
            print(f"hydra eval: unknown flag {a!r}")
            return 2
        else:
            suite = a
        i += 1
    results = run(cfg, suite, real=real, model_pairs=model_pairs, keep=keep)
    if not results:
        return 2
    return 0 if all(r["pass"] for r in results) else 1
