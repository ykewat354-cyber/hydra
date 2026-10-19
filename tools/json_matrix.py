#!/usr/bin/env python3
"""T07 — structured-output conformance matrix (real API, exactly 6 models x 9).

For every free model (``status == "ok"`` in ``~/.hydra/models.json``) this runs
3 trials x 3 schemas from Appendix B (verdict, plan, report) with:

  * ``format`` = the JSON schema, no tools
  * temperature 0.2, num_predict <= 1200
  * thinking at the model's lowest supported setting
    (think_style levels -> "low", bool -> False, none -> omit)

Every reply is validated **strictly** (``json.loads`` with no extraction, then
the schema: top-level type, required keys, enums, no extra keys, item types)
and **tolerantly** (after HYDRA's own normalization: ``parse_json_blob`` plus
the planner's bare-array wrap / steps->nodes shaping).  Results go to
``results.json`` + ``matrix.md``; per-model medians and a json_class
(strict >= 8/9 strict, tolerant >= 8/9 tolerant, else no) are stored in
models.json through the ModelRegistry API.

Budget: exactly 54 chat calls, no retries, no extra probes.  ``--resume`` only
skips calls already recorded in results.json; it never adds calls.

Usage:
    python tools/json_matrix.py [--resume]
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hydra import config as C            # noqa: E402
from hydra import hygiene                # noqa: E402
from hydra.agent import parse_json_blob  # noqa: E402
from hydra.client import OllamaCloud     # noqa: E402
from hydra.jsonschema import validate    # noqa: E402
from hydra.models import ModelRegistry   # noqa: E402

TRIALS = 3
NUM_PREDICT = 1200
TEMPERATURE = 0.2
DEFAULT_OUT = Path("~/hydra-real-test/wo1/T07").expanduser()

# ---------------------------------------------------------------- Appendix B
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"what": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["what", "fix"],
            "additionalProperties": False}},
    },
    "required": ["verdict", "issues"],
    "additionalProperties": False,
}
PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "acceptance": {"type": "array", "items": {"type": "string"}},
        "nodes": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "role": {"type": "string", "enum": [
                    "worker", "researcher", "coder", "debugger", "tester",
                    "reviewer", "scribe", "planner"]},
                "task": {"type": "string"},
                "deps": {"type": "array", "items": {"type": "string"}}},
            "required": ["id", "role", "task", "deps"],
            "additionalProperties": False}},
    },
    "required": ["goal", "acceptance", "nodes"],
    "additionalProperties": False,
}
REPORT_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["done", "partial", "failed"]},
        "summary": {"type": "string"},
        "files_changed": {"type": "array", "items": {"type": "string"}},
        "commands_run": {"type": "array", "items": {"type": "string"}},
        "findings": {"type": "array", "items": {"type": "string"}},
        "open_issues": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["status", "summary", "files_changed", "commands_run",
                 "findings", "open_issues"],
    "additionalProperties": False,
}

SCHEMAS = {
    "verdict": (VERDICT_SCHEMA,
                "Task: create hello.py printing 42. Evidence: ran it, output "
                "was 42. Judge the work."),
    "plan": (PLAN_SCHEMA,
             "Plan a 2-step task: write sum.py, then test it."),
    "report": (REPORT_SCHEMA,
               "Report on: you created x.txt."),
}

# The free models T06 found (status ok).  A mismatch is reported, not hidden.
EXPECTED_OK = {
    "gemma4:31b",
    "gpt-oss:120b",
    "gpt-oss:20b",
    "nemotron-3-nano:30b",
    "nemotron-3-super",
    "nemotron-3-ultra",
}


def lowest_think(style: str):
    """F1/T07 think policy: the lowest setting each dialect supports."""
    if style == "levels":
        return "low"
    if style == "bool":
        return False
    return None  # style "none": omit the parameter entirely


# ---------------------------------------------------------------- normalization

def normalize_obj(name: str, obj, task: str):
    """HYDRA's own tolerant normalization for each schema name."""
    if obj is None:
        return None
    if name == "plan":
        # planner.make_dag: a bare list of steps becomes {"goal","steps"};
        # then map its step shape onto Appendix B's nodes shape.
        if isinstance(obj, list):
            obj = {"goal": task, "steps": obj}
        if isinstance(obj, dict) and not isinstance(obj.get("nodes"), list):
            steps = obj.get("steps")
            if isinstance(steps, list):
                nodes = []
                for i, step in enumerate(steps, 1):
                    if not isinstance(step, dict):
                        return obj
                    title = step.get("task") or step.get("title") or step.get("step")
                    if title is None:
                        return obj
                    try:
                        sid = str(step.get("id") if step.get("id") is not None else i)
                    except (TypeError, ValueError):
                        sid = str(i)
                    deps = step.get("deps") or []
                    if not isinstance(deps, list):
                        deps = []
                    role = str(step.get("role") or "worker").strip()
                    if role not in PLAN_SCHEMA["properties"]["nodes"]["items"]["properties"]["role"]["enum"]:
                        role = "worker"
                    nodes.append({"id": sid, "role": role,
                                  "task": str(title), "deps": [str(d) for d in deps]})
                obj = {"goal": str(obj.get("goal") or task),
                       "acceptance": [str(a) for a in (obj.get("acceptance") or [])],
                       "nodes": nodes}
        return obj
    return obj


def check_strict(content: str, schema: dict):
    """(ok, detail): json.loads with no extraction + full schema validation."""
    text = (content or "").strip()
    try:
        obj = json.loads(text)
    except Exception as exc:
        return False, f"not JSON ({type(exc).__name__})"
    errs = validate(obj, schema)
    return (not errs), "; ".join(errs[:3])


def check_tolerant(content: str, schema: dict, name: str, task: str):
    """(ok, detail): accepted after HYDRA's own normalization, then validated."""
    ok, detail = check_strict(content, schema)
    if ok:
        return True, ""
    obj = normalize_obj(name, parse_json_blob(content or ""), task)
    if obj is None:
        return False, detail
    errs = validate(obj, schema)
    return (not errs), "; ".join(errs[:3])


# ---------------------------------------------------------------- main

def _median(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return statistics.median(values) if values else 0


def _aggregate(state: dict, registry, prices, ok_models, out_dir: Path) -> dict:
    """Per-model + per-schema stats, registry updates, results.json + matrix.md."""
    by_model = {}
    for m in ok_models:
        recs = [r for r in state["calls"] if r.get("model") == m]
        strict_ok = sum(1 for r in recs if r.get("strict_ok"))
        tolerant_ok = sum(1 for r in recs if r.get("tolerant_ok"))
        got = [r for r in recs if not r.get("error")]
        med_in = int(_median([r.get("tin") for r in got]))
        med_out = int(_median([r.get("tout") for r in got]))
        p50_ms = int(_median([r.get("ms") for r in got]))
        thinking = any(r.get("thinking") for r in recs)
        if strict_ok >= 8:
            cls = "strict"
        elif tolerant_ok >= 8:
            cls = "tolerant"
        else:
            cls = "no"
        cost_total = sum(C.est_usd(prices, m, r.get("tin") or 0, r.get("tout") or 0)
                         for r in recs)
        schemas = {}
        for name in SCHEMAS:
            srecs = [r for r in got if r.get("schema") == name]
            schemas[name] = {
                "strict_ok": sum(1 for r in srecs if r.get("strict_ok")),
                "tolerant_ok": sum(1 for r in srecs if r.get("tolerant_ok")),
                "med_in": int(_median([r.get("tin") for r in srecs])),
                "med_out": int(_median([r.get("tout") for r in srecs])),
                "p50_ms": int(_median([r.get("ms") for r in srecs])),
            }
        by_model[m] = {
            "status": "ok", "strict_ok": strict_ok, "tolerant_ok": tolerant_ok,
            "trials": len(recs), "errors": sum(1 for r in recs if r.get("error")),
            "med_in": med_in, "med_out": med_out, "p50_ms": p50_ms,
            "thinking": thinking, "json_class": cls, "by_schema": schemas,
            "est_usd": round(cost_total, 6),
        }
        try:
            registry.set_json_class(
                m, cls, json_strict_ok=strict_ok, json_tolerant_ok=tolerant_ok,
                json_trials=len(recs), json_med_in=med_in, json_med_out=med_out,
                json_p50_ms=p50_ms, json_thinking=thinking,
                json_med_by_schema=schemas,
                json_checked=time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as exc:
            print(f"  warn: could not store json_class for {m}: {exc}")

    total_usd = sum(v["est_usd"] for v in by_model.values())
    state["summary"] = {"by_model": by_model, "total_est_usd": round(total_usd, 6),
                        "calls": len(state["calls"]),
                        "finished": time.strftime("%Y-%m-%d %H:%M:%S")}
    (out_dir / "results.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "matrix.md").write_text(render_markdown(state), encoding="utf-8")
    return state


def run(out_dir: Path, resume: bool, recompute: bool = False) -> int:
    cfg = C.load_config()
    registry = ModelRegistry(cfg)
    prices = C.load_prices(cfg)
    results_path = out_dir / "results.json"

    if recompute:
        # No API calls: re-aggregate an existing results.json and refresh
        # models.json / matrix.md (useful after a tools change).
        if not results_path.is_file():
            print(f"hydra: no results at {results_path}")
            return 1
        state = json.loads(results_path.read_text(encoding="utf-8"))
        ok_models = [m for m in (state.get("models") or []) if m]
        state = _aggregate(state, registry, prices, ok_models, out_dir)
        print(f"recomputed {state['summary']['calls']} calls from {results_path}")
        return 0

    key = C.load_key(cfg)
    if not key:
        print("hydra: no API key (~/.hydra/key or OLLAMA_API_KEY)")
        return 1
    host = C.get(cfg, "api.host", "https://ollama.com")
    client = OllamaCloud(host, key,
                         C.get(cfg, "api.connect_timeout", 15) or 15,
                         C.get(cfg, "api.idle_timeout", 90) or 90)
    client.redact = hygiene.make_redactor(cfg)

    ok_models = [e["model"] for e in registry.list_all()
                 if e.get("status_now") == "ok"]
    missing = sorted(EXPECTED_OK - set(ok_models))
    extra = sorted(set(ok_models) - EXPECTED_OK)
    if missing or extra:
        print(f"NOTE: free-model set differs from T06 "
              f"(missing={missing} extra={extra})")
    plan_calls = [(m, name) for m in ok_models for name in SCHEMAS for _ in range(TRIALS)]
    print(f"probing {len(ok_models)} ok models x {len(SCHEMAS)} schemas x "
          f"{TRIALS} trials = {len(plan_calls)} calls (cap 54)")
    if len(plan_calls) > 54:
        print("refusing: more than 54 probe calls")
        return 2

    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / "results.json"

    state = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "host": host,
             "models": ok_models, "calls": []}
    done_keys = set()
    if resume and results_path.is_file():
        try:
            prev = json.loads(results_path.read_text(encoding="utf-8"))
            state = prev
            for r in prev.get("calls") or []:
                done_keys.add((r.get("model"), r.get("schema"), r.get("trial")))
            print(f"resume: {len(done_keys)} calls already recorded")
        except Exception:
            pass

    trial_no = {}
    if done_keys:
        for r in state.get("calls") or []:
            key = (r.get("model"), r.get("schema"))
            if isinstance(r.get("trial"), int):
                trial_no[key] = max(trial_no.get(key, 0), r["trial"])

    calls_made = 0
    for model, name in plan_calls:
        schema, task = SCHEMAS[name]
        key = (model, name)
        trial_no[key] = trial_no.get(key, 0) + 1
        trial = trial_no[key]
        if (model, name, trial) in done_keys:
            continue
        style = registry.think_style(model)
        think = lowest_think(style)
        payload_options = {"temperature": TEMPERATURE, "num_predict": NUM_PREDICT}
        started = time.time()
        rec = {"model": model, "schema": name, "trial": trial,
               "think": think, "at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "strict_ok": False, "tolerant_ok": False, "error": "",
               "strict_detail": "", "tolerant_detail": "",
               "tin": 0, "tout": 0, "ms": 0, "thinking": False,
               "done_reason": "", "content": "", "content_chars": 0}
        try:
            reply = client.chat(model, [{"role": "user", "content": task}],
                                tools=None, think=think, options=payload_options,
                                stream=True, format=schema)
            rec.update(tin=reply.prompt_eval_count, tout=reply.eval_count,
                       ms=reply.ms, thinking=bool((reply.thinking or "").strip()),
                       done_reason=reply.done_reason,
                       content=(reply.content or "")[:4000],
                       content_chars=len(reply.content or ""))
            rec["strict_ok"], rec["strict_detail"] = check_strict(reply.content, schema)
            rec["tolerant_ok"], rec["tolerant_detail"] = check_tolerant(
                reply.content, schema, name, task)
            if rec["tolerant_ok"] and not rec["strict_ok"]:
                rec["tolerant_detail"] = "normalized"
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
            rec["ms"] = int((time.time() - started) * 1000)
        calls_made += 1
        state["calls"].append(rec)
        results_path.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        flag = "S" if rec["strict_ok"] else ("T" if rec["tolerant_ok"] else "x")
        extra_err = f" err={rec['error'][:60]}" if rec["error"] else ""
        print(f"  [{calls_made:2d}/{len(plan_calls)}] {model:<20} {name:<7} "
              f"t{trial} {flag} in={rec['tin']} out={rec['tout']} "
              f"{rec['ms']}ms think={rec['thinking']}{extra_err}", flush=True)
        time.sleep(0.4)

    # ------------------------------------------------------------ aggregate
    plan_calls_done = len(state["calls"])
    state = _aggregate(state, registry, prices, ok_models, out_dir)
    by_model = state["summary"]["by_model"]
    total_usd = state["summary"]["total_est_usd"]
    print(f"\n{plan_calls_done} calls · est ${total_usd:.4f} (imputed at list "
          f"prices) · wrote {results_path} and {out_dir / 'matrix.md'}")
    for m in ok_models:
        v = by_model[m]
        print(f"  {m:<20} strict={v['strict_ok']}/9 tolerant={v['tolerant_ok']}/9 "
              f"in/out={v['med_in']}/{v['med_out']} p50={v['p50_ms']}ms "
              f"think={v['thinking']} -> {v['json_class']}")
    return 0


def render_markdown(state: dict) -> str:
    by_model = state.get("summary", {}).get("by_model") or {}
    lines = ["# T07 — structured-output conformance matrix",
             "",
             f"Run: {state.get('started', '?')} → "
             f"{state.get('summary', {}).get('finished', '?')} · "
             f"{state.get('summary', {}).get('calls', 0)} calls (6 models × 3 "
             f"schemas × 3 trials) · est ${state.get('summary', {}).get('total_est_usd', 0):.4f}",
             "",
             "| model | strict/9 | tolerant/9 | med in | med out | p50 ms | thinking | json_class |",
             "|---|---|---|---|---|---|---|---|"]
    for m, v in by_model.items():
        lines.append(f"| {m} | {v['strict_ok']}/9 | {v['tolerant_ok']}/9 | "
                     f"{v['med_in']} | {v['med_out']} | {v['p50_ms']} | "
                     f"{'yes' if v['thinking'] else 'no'} | {v['json_class']} |")
    lines += ["", "Per-schema strict/tolerant counts:", "",
              "| model | schema | strict | tolerant | med in | med out | p50 ms |",
              "|---|---|---|---|---|---|---|"]
    for m, v in by_model.items():
        for name, s in (v.get("by_schema") or {}).items():
            lines.append(f"| {m} | {name} | {s['strict_ok']}/3 | "
                         f"{s['tolerant_ok']}/3 | {s['med_in']} | "
                         f"{s['med_out']} | {s['p50_ms']} |")
    lines += ["", "Strict = `json.loads` with no extraction + full Appendix B "
                  "schema check. Tolerant = accepted after HYDRA's own "
                  "normalization (`parse_json_blob`, planner bare-array wrap, "
                  "steps→nodes shaping), then validated.", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--resume", action="store_true",
                    help="skip calls already present in results.json (never add calls)")
    ap.add_argument("--recompute", action="store_true",
                    help="re-aggregate an existing results.json with NO API calls")
    args = ap.parse_args()
    return run(Path(args.out).expanduser(), args.resume, args.recompute)


if __name__ == "__main__":
    raise SystemExit(main())
