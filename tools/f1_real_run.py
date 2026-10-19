#!/usr/bin/env python3
"""F1 — one real planner + verifier run (tiny task, <= ~8 model calls).

Fresh temp work dir, `loop.verify=auto`, `loop.max_turns=6`,
`agents.max_agents=1`, `--debug-http` on so runs/<id>/http.jsonl proves the
`format` field was sent.  Prints and stores (in ~/hydra-real-test/wo1/F1/):

  * the run id and the chosen model per structured call (json_route events),
  * tokens / latency / est $ per call from usage.jsonl,
  * the same tokens priced at nemotron-3-ultra for the comparison.

Usage:  timeout 300 python tools/f1_real_run.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from hydra import config as C        # noqa: E402
from hydra import journal as J       # noqa: E402
from hydra import planner            # noqa: E402
from hydra.runtime import Runtime    # noqa: E402

OUT = Path("~/hydra-real-test/wo1/F1").expanduser()
TASK = ("plan and verify writing ok.txt with the word ok; when done, call the "
        "finish tool with status done, files_changed [\"ok.txt\"], and a summary")
LEAD = "gpt-oss:20b"
ULTRA = "nemotron-3-ultra"


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    work = OUT / time.strftime("work-%Y%m%d-%H%M%S")
    work.mkdir(parents=True, exist_ok=True)
    cfg = C.load_config(overrides=[
        "loop.verify=auto",
        "loop.max_turns=6",
        "agents.max_agents=1",
        "api.debug_http=true",
        "termux.wake_lock=false",
        "termux.notify_on_finish=false",
        "termux.notify_on_park=false",
    ], project_dir=str(work))
    prices = C.load_prices(cfg)
    rt = Runtime(cfg, cwd=str(work), quiet=True)
    started = time.time()
    try:
        lead = rt.start_run(TASK, name="f1-json-route", lead_model=LEAD)
        dag, err = planner.make_dag(rt, TASK)
        code, report = rt._execute(lead)
        run_id = rt.run_id
    finally:
        rt.close()
    wall = time.time() - started

    events = J.read_events(run_id)
    usage = J.read_usage(run_id)
    routes = [e for e in events if e.get("type") == "json_route"]
    model_responses = [e for e in events if e.get("type") == "model_response"]
    structured = [r for r in usage if r.get("role") in ("planner", "reviewer")]
    structured_ultra = [
        {"role": r.get("role"), "model_at_ultra": ULTRA,
         "in": r.get("in", 0), "out": r.get("out", 0),
         "est_usd": round(C.est_usd(prices, ULTRA, r.get("in") or 0,
                                    r.get("out") or 0), 6)}
        for r in structured]
    http_path = C.run_dir(run_id) / "http.jsonl"
    http_fmt = []
    if http_path.is_file():
        for line in http_path.read_text(encoding="utf-8").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            http_fmt.append({"path": rec.get("path"), "model": rec.get("model"),
                             "status": rec.get("status"),
                             "format_chars": len(json.dumps(rec.get("format") or {}))})

    total_est = round(sum(float(r.get("est_usd") or 0) for r in usage), 6)
    structured_est = round(sum(float(r.get("est_usd") or 0) for r in structured), 6)
    ultra_est = round(sum(s["est_usd"] for s in structured_ultra), 6)
    artifact = work / "ok.txt"
    summary = {
        "run_id": run_id, "task": TASK, "lead": LEAD, "workdir": str(work),
        "exit_code": code, "report_status": (report or {}).get("status"),
        "wall_s": round(wall, 1), "calls": len(usage),
        "total_est_usd": total_est,
        "structured_calls": structured,
        "structured_cost_chosen_usd": structured_est,
        "structured_cost_if_ultra_usd": ultra_est,
        "dag_ok": dag is not None, "dag_error": err,
        "ok_txt": artifact.read_text(encoding="utf-8", errors="replace")
                  if artifact.is_file() else None,
        "routes": routes, "model_responses": model_responses,
        "http_format": http_fmt,
    }
    (OUT / "run.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False),
                                  encoding="utf-8")
    lines = [
        "# F1 — real planner + verifier run",
        "",
        f"run `{run_id}` · exit {code} · report "
        f"{(report or {}).get('status')} · {summary['calls']} calls · "
        f"{summary['wall_s']}s · est ${total_est}",
        f"workdir `{work}` · dag_ok={summary['dag_ok']} ({err or 'no error'})",
        "",
        "| call | role | model | in | out | ms | est $ | est $ at ultra |",
        "|---|---|---|---|---|---|---|---|"]
    for r in structured:
        u = next((s for s in structured_ultra if s["role"] == r.get("role")), None)
        lines.append(f"| structured | {r.get('role')} | {r.get('model')} | "
                     f"{r.get('in')} | {r.get('out')} | {r.get('ms')} | "
                     f"{r.get('est_usd')} | {u['est_usd'] if u else '-'} |")
    lines += ["",
              f"Structured total: **${structured_est}** chosen vs "
              f"**${ultra_est}** if every structured call ran on {ULTRA}.",
              f"`format` reached the wire in {len(http_fmt)} HTTP calls "
              f"(see runs/{run_id}/http.jsonl).",
              "", "| all calls | role | model | in | out | est $ |",
              "|---|---|---|---|---|---|"]
    for r in usage:
        lines.append(f"| | {r.get('role')} | {r.get('model')} | {r.get('in')} | "
                     f"{r.get('out')} | {r.get('est_usd')} |")
    (OUT / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"run {run_id} · {len(usage)} calls · est ${total_est} · {wall:.0f}s")
    for r in structured:
        u = next((s for s in structured_ultra if s["role"] == r.get("role")), None)
        print(f"  structured {r.get('role')}: {r.get('model')} "
              f"in={r.get('in')} out={r.get('out')} ms={r.get('ms')} "
              f"est=${r.get('est_usd')} (ultra=${u['est_usd'] if u else '-'})")
    for r in routes:
        print(f"  json_route {r.get('schema')} ok={r.get('ok')} model={r.get('model')} "
              f"attempts={len(r.get('attempts') or [])}")
    for r in usage:
        print(f"  call {r.get('role'):<10} {r.get('model'):<20} "
              f"in={r.get('in'):>5} out={r.get('out'):>5} ${r.get('est_usd')}")
    print(f"  ok.txt: {summary['ok_txt']!r}")
    print(f"wrote {OUT / 'run.json'} and {OUT / 'summary.md'}")
    return 0 if code == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
