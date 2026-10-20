#!/usr/bin/env python3
"""C3 — prompted JSON-obedience matrix (real API, 2 trials x 3 schemas x 2).

The WO#1 T07 matrix only used the ``format`` parameter.  This one tells the
model, in the prompt, to return **only** one JSON value and includes the full
schema text.  Every model is tested with and without ``format``:

    6 models x 3 schemas x 2 trials x 2 formats = 72 calls

Strict validation is ``json.loads`` with no extraction plus the schema.
Tolerant validation is HYDRA's own normalization (``parse_json_blob`` and the
planner bare-array/steps shaping) followed by the schema.  Results are stored
in ``models.json`` as ``json_prompt_class`` and F1 routing prefers that class
over the old T07 ``json_class``.

Usage:
    python tools/json_prompt_matrix.py [--models a,b] [--out DIR] [--resume]
                                       [--max-usd 0.03] [--recompute]
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
TOOLS = Path(__file__).resolve().parent
if str(TOOLS) not in sys.path:
    sys.path.insert(0, str(TOOLS))

from hydra import config as C                    # noqa: E402
from hydra import hygiene                        # noqa: E402
from hydra.client import OllamaCloud             # noqa: E402
from hydra.models import ModelRegistry           # noqa: E402
from json_matrix import (SCHEMAS, check_strict, check_tolerant,  # noqa: E402
                         lowest_think)

TRIALS = 2
FORMATS = (True, False)
DEFAULT_OUT = Path("~/hydra-real-test/wo3/C3").expanduser()


def _clean(text: str) -> str:
    return text.encode("utf-8", "replace").decode("utf-8")


def _prompt(name: str, task: str, schema: dict) -> str:
    return (
        "Return ONLY one JSON value that validates against the JSON Schema "
        "below. Do not add markdown fences, prose, explanations or comments.\n\n"
        "JSON SCHEMA:\n" + json.dumps(schema, ensure_ascii=False, indent=2) +
        "\n\nTASK:\n" + task + "\n\nJSON only:")


def _median(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return statistics.median(values) if values else 0


def _classify(strict_ok: int, tolerant_ok: int, trials: int) -> str:
    # T07 used 8/9 (~89%).  At 12 calls: strict >= 11, tolerant >= 10.
    if strict_ok >= max(1, trials - 1):
        return "strict"
    if tolerant_ok >= max(1, trials - 2):
        return "tolerant"
    return "no"


def aggregate(state: dict, registry, prices, models, out_dir: Path) -> dict:
    by_model = {}
    for model in models:
        recs = [r for r in state["calls"] if r.get("model") == model]
        got = [r for r in recs if not r.get("error")]
        strict_ok = sum(1 for r in recs if r.get("strict_ok"))
        tolerant_ok = sum(1 for r in recs if r.get("tolerant_ok"))
        trials = len(recs)
        cls = _classify(strict_ok, tolerant_ok, trials) if trials else "no"
        by_format = {}
        for fmt in FORMATS:
            frecs = [r for r in got if bool(r.get("format")) is fmt]
            by_format["format" if fmt else "prompt_only"] = {
                "strict_ok": sum(1 for r in frecs if r.get("strict_ok")),
                "tolerant_ok": sum(1 for r in frecs if r.get("tolerant_ok")),
                "trials": len(frecs),
                "med_in": int(_median([r.get("tin") for r in frecs])),
                "med_out": int(_median([r.get("tout") for r in frecs])),
                "p50_ms": int(_median([r.get("ms") for r in frecs])),
            }
        by_schema = {}
        for name in SCHEMAS:
            srecs = [r for r in got if r.get("schema") == name]
            by_schema[name] = {
                "strict_ok": sum(1 for r in srecs if r.get("strict_ok")),
                "tolerant_ok": sum(1 for r in srecs if r.get("tolerant_ok")),
                "med_in": int(_median([r.get("tin") for r in srecs])),
                "med_out": int(_median([r.get("tout") for r in srecs])),
            }
        med_in = int(_median([r.get("tin") for r in got]))
        med_out = int(_median([r.get("tout") for r in got]))
        p50 = int(_median([r.get("ms") for r in got]))
        est = sum(C.est_usd(prices, model, r.get("tin") or 0, r.get("tout") or 0)
                  for r in recs)
        by_model[model] = {
            "trials": trials, "strict_ok": strict_ok,
            "tolerant_ok": tolerant_ok, "json_prompt_class": cls,
            "by_format": by_format, "by_schema": by_schema,
            "med_in": med_in, "med_out": med_out, "p50_ms": p50,
            "est_usd": round(est, 6),
            "errors": sum(1 for r in recs if r.get("error")),
        }
        try:
            registry.set_json_prompt_class(
                model, cls, json_prompt_strict_ok=strict_ok,
                json_prompt_tolerant_ok=tolerant_ok,
                json_prompt_trials=trials,
                json_prompt_by_format=by_format,
                json_prompt_by_schema=by_schema,
                json_med_in=med_in, json_med_out=med_out, json_p50_ms=p50,
                json_med_by_schema=by_schema,
                json_prompt_checked=time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as exc:
            print(f"  warn: could not store json_prompt_class for {model}: {exc}")

    total = sum(v["est_usd"] for v in by_model.values())
    state["summary"] = {
        "by_model": by_model, "total_est_usd": round(total, 6),
        "calls": len(state["calls"]),
        "finished": time.strftime("%Y-%m-%d %H:%M:%S"),
        "prompt_mode": True, "trials_per_model": TRIALS * len(SCHEMAS) * len(FORMATS),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    (out_dir / "matrix.md").write_text(render(state), encoding="utf-8")
    return state


def render(state: dict) -> str:
    by_model = state.get("summary", {}).get("by_model") or {}
    import json_matrix
    lines = [
        "# C3 — prompted JSON obedience matrix",
        "",
        f"Run: {state.get('started', '?')} → "
        f"{state.get('summary', {}).get('finished', '?')} · "
        f"{state.get('summary', {}).get('calls', 0)} calls · est "
        f"${state.get('summary', {}).get('total_est_usd', 0):.4f}",
        "",
        "Prompts explicitly requested JSON only and included the schema text. "
        "Strict = `json.loads` with no extraction + schema. Tolerant = after "
        "HYDRA normalization + schema.",
        "",
        "| model | strict/12 | tolerant/12 | with format S/T | prompt-only S/T | med in | med out | p50 ms | class |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for m, v in by_model.items():
        wf = (v.get("by_format") or {}).get("format") or {}
        po = (v.get("by_format") or {}).get("prompt_only") or {}
        lines.append(
            f"| {m} | {v['strict_ok']}/{v['trials']} | "
            f"{v['tolerant_ok']}/{v['trials']} | "
            f"{wf.get('strict_ok', 0)}/{wf.get('tolerant_ok', 0)} | "
            f"{po.get('strict_ok', 0)}/{po.get('tolerant_ok', 0)} | "
            f"{v['med_in']} | {v['med_out']} | {v['p50_ms']} | "
            f"**{v['json_prompt_class']}** |")
    lines += ["", "Per-schema strict/tolerant counts (both formats):", "",
              "| model | schema | strict | tolerant | med in | med out |",
              "|---|---|---|---|---|---|"]
    for m, v in by_model.items():
        for name, s in (v.get("by_schema") or {}).items():
            lines.append(f"| {m} | {name} | {s['strict_ok']}/4 | "
                         f"{s['tolerant_ok']}/4 | {s['med_in']} | "
                         f"{s['med_out']} |")
    lines.append("")
    return "\n".join(lines)


def run(out_dir: Path, models: list, resume: bool, recompute: bool,
        max_usd: float) -> int:
    cfg = C.load_config()
    registry = ModelRegistry(cfg)
    prices = C.load_prices(cfg)
    results_path = out_dir / "results.json"

    if recompute:
        if not results_path.is_file():
            print(f"hydra: no results at {results_path}")
            return 1
        state = json.loads(results_path.read_text(encoding="utf-8"))
        ms = [m for m in (state.get("models") or []) if m]
        state = aggregate(state, registry, prices, ms, out_dir)
        print(f"recomputed {state['summary']['calls']} calls (no API calls)")
        return 0

    if not models:
        models = [e["model"] for e in registry.list_all()
                  if e.get("status_now") == "ok"]
    models = list(dict.fromkeys(str(m) for m in models if m))
    if not models:
        print("hydra: no ok models in models.json - run `hydra models probe`")
        return 1
    key = C.load_key(cfg)
    if not key:
        print("hydra: no API key (~/.hydra/key or OLLAMA_API_KEY)")
        return 1
    host = C.get(cfg, "api.host", "https://ollama.com")
    client = OllamaCloud(host, key,
                         C.get(cfg, "api.connect_timeout", 15) or 15,
                         C.get(cfg, "api.idle_timeout", 90) or 90)
    client.redact = hygiene.make_redactor(cfg)

    plan = [(m, name, trial, fmt)
            for m in models for name in SCHEMAS
            for trial in range(1, TRIALS + 1) for fmt in FORMATS]
    print(f"probing {len(models)} models x {len(SCHEMAS)} schemas x "
          f"{TRIALS} trials x {len(FORMATS)} formats = {len(plan)} calls "
          f"(budget ${max_usd:.3f})")

    out_dir.mkdir(parents=True, exist_ok=True)
    state = {"started": time.strftime("%Y-%m-%d %H:%M:%S"), "host": host,
             "models": models, "prompt_mode": True, "calls": []}
    done = set()
    if resume and results_path.is_file():
        try:
            prev = json.loads(results_path.read_text(encoding="utf-8"))
            state = prev
            for r in prev.get("calls") or []:
                done.add((r.get("model"), r.get("schema"), r.get("trial"),
                          bool(r.get("format"))))
            print(f"resume: {len(done)} calls already recorded")
        except Exception:
            pass

    total = 0.0
    made = 0
    for model, name, trial, fmt in plan:
        if (model, name, trial, fmt) in done:
            continue
        schema, task = SCHEMAS[name]
        style = registry.think_style(model)
        think = lowest_think(style)
        prompt = _prompt(name, task, schema)
        started = time.time()
        rec = {"model": model, "schema": name, "trial": trial,
               "format": fmt, "think": think,
               "at": time.strftime("%Y-%m-%d %H:%M:%S"),
               "strict_ok": False, "tolerant_ok": False, "error": "",
               "strict_detail": "", "tolerant_detail": "",
               "tin": 0, "tout": 0, "ms": 0, "thinking": False,
               "content": "", "content_chars": 0}
        try:
            reply = client.chat(
                model, [{"role": "user", "content": prompt}], tools=None,
                think=think,
                options={"temperature": 0.2, "num_predict": 1200},
                stream=True, format=schema if fmt else None)
            rec.update(tin=reply.prompt_eval_count, tout=reply.eval_count,
                       ms=reply.ms,
                       thinking=bool((reply.thinking or "").strip()),
                       content=(reply.content or "")[:2000],
                       content_chars=len(reply.content or ""))
            rec["strict_ok"], rec["strict_detail"] = check_strict(
                reply.content, schema)
            rec["tolerant_ok"], rec["tolerant_detail"] = check_tolerant(
                reply.content, schema, name, task)
            if rec["tolerant_ok"] and not rec["strict_ok"]:
                rec["tolerant_detail"] = "normalized"
        except Exception as exc:
            rec["error"] = f"{type(exc).__name__}: {exc}"[:300]
            rec["ms"] = int((time.time() - started) * 1000)
        cost = C.est_usd(prices, model, rec.get("tin") or 0, rec.get("tout") or 0)
        total += float(cost or 0)
        made += 1
        state["calls"].append(rec)
        results_path.write_text(json.dumps(state, indent=2, ensure_ascii=False),
                                encoding="utf-8")
        flag = "S" if rec["strict_ok"] else ("T" if rec["tolerant_ok"] else "x")
        err = f" err={rec['error'][:50]}" if rec["error"] else ""
        print(f"  [{made:2d}/{len(plan)}] {model:<20} {name:<7} t{trial} "
              f"{'fmt' if fmt else 'txt'} {flag} in={rec['tin']} "
              f"out={rec['tout']} {rec['ms']}ms est=${total:.4f}{err}",
              flush=True)
        time.sleep(0.4)
        if total > max_usd:
            print(f"stopping: estimated ${total:.4f} > budget ${max_usd:.3f}")
            break

    state = aggregate(state, registry, prices, models, out_dir)
    print(f"\n{state['summary']['calls']} calls · est "
          f"${state['summary']['total_est_usd']:.4f} · wrote {results_path} "
          f"and {out_dir / 'matrix.md'}")
    for m, v in state["summary"]["by_model"].items():
        print(f"  {m:<22} strict={v['strict_ok']}/{v['trials']} "
              f"tolerant={v['tolerant_ok']}/{v['trials']} -> "
              f"{v['json_prompt_class']}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--models", default="",
                    help="comma-separated subset (default: every ok model)")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--recompute", action="store_true")
    ap.add_argument("--max-usd", type=float, default=0.03)
    args = ap.parse_args()
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    return run(Path(args.out).expanduser(), models, args.resume,
               args.recompute, args.max_usd)


if __name__ == "__main__":
    raise SystemExit(main())
