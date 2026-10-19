#!/usr/bin/env python3
"""T12 swarm/sub-agent timeline analysis from a run's events.jsonl.

Computes, without the API:
  1. max concurrent model calls (must stay 1 on the free plan);
  2. seconds with >= 2 tools running at the same time (must be > 0 for a
     parallel swarm);
  3. wall time vs the sum of tool times;
  4. per-agent tokens;
  5. lead final context (last prompt tokens) vs the children's total.

Usage: python tools/swarm_analysis.py RUN_DIR [--json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def load_events(run_dir: Path) -> list[dict]:
    path = run_dir if run_dir.is_file() else run_dir / "events.jsonl"
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


def _overlap_peak(intervals: list[tuple[float, float]]) -> int:
    edges = []
    for start, end in intervals:
        edges.append((start, 1))
        edges.append((end, -1))
    # Ends sort before starts at the same timestamp: journals round to the
    # millisecond, so a call that hands over to the next one can share a ts.
    edges.sort(key=lambda x: (x[0], x[1]))
    cur = peak = 0
    for _, delta in edges:
        cur += delta
        peak = max(peak, cur)
    return peak


def _overlap_seconds(intervals: list[tuple[float, float]]) -> float:
    """Seconds where at least two intervals overlap each other."""
    edges = []
    for start, end in intervals:
        edges.append((start, 1))
        edges.append((end, -1))
    edges.sort(key=lambda x: (x[0], x[1]))
    cur = 0
    total = 0.0
    prev = None
    for ts, delta in edges:
        if prev is not None and cur >= 2 and ts > prev:
            total += ts - prev
        cur += delta
        prev = ts
    return round(total, 3)


def analyse(events: list[dict]) -> dict:
    model_open: dict[str, list[float]] = {}
    model_intervals: list[tuple[float, float]] = []
    tool_open: dict[tuple[str, str], list[float]] = {}
    tool_intervals: list[tuple[float, float]] = []
    tool_ms_sum = 0
    per_agent = {}
    lead_last_in = None
    started = ended = None

    for ev in events:
        ts = float(ev.get("ts") or 0)
        agent = str(ev.get("agent") or "?")
        kind = ev.get("type")
        if started is None:
            started = ts
        ended = ts
        if kind == "model_request":
            model_open.setdefault(agent, []).append(ts)
        elif kind == "model_response":
            row = per_agent.setdefault(agent, {"role": ev.get("role"), "in": 0,
                                               "out": 0, "calls": 0})
            row["in"] += int(ev.get("in_tokens") or 0)
            row["out"] += int(ev.get("out_tokens") or 0)
            row["calls"] += 1
            if str(ev.get("role")) == "lead":
                lead_last_in = int(ev.get("in_tokens") or 0)
            if model_open.get(agent):
                model_intervals.append((model_open[agent].pop(0), ts))
        elif kind == "tool_call":
            tool_open.setdefault((agent, str(ev.get("tool"))), []).append(ts)
        elif kind == "tool_result":
            ms = int(ev.get("ms") or 0)
            tool_ms_sum += ms
            key = (agent, str(ev.get("tool")))
            if tool_open.get(key):
                start = tool_open[key].pop(0)
                end = ts if ts >= start else start + ms / 1000.0
                tool_intervals.append((start, end))

    wall = round((ended or 0) - (started or 0), 3)
    children_in = sum(v["in"] for a, v in per_agent.items()
                      if str(v.get("role")) != "lead")
    return {
        "events": len(events),
        "wall_seconds": wall,
        "max_concurrent_model_calls": _overlap_peak(model_intervals),
        "tool_overlap_seconds": _overlap_seconds(tool_intervals),
        "tool_ms_sum": tool_ms_sum,
        "tool_seconds_sum": round(tool_ms_sum / 1000.0, 3),
        "per_agent": per_agent,
        "agents": len(per_agent),
        "lead_last_prompt_tokens": lead_last_in,
        "children_prompt_tokens_total": children_in,
    }


def main(argv: list[str]) -> int:
    if not argv[1:]:
        print(__doc__)
        return 2
    run_dir = Path(argv[1])
    if not run_dir.exists():
        print(f"no such run dir/file: {run_dir}")
        return 2
    result = analyse(load_events(run_dir))
    if "--json" in argv:
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"events: {result['events']} · wall {result['wall_seconds']}s · "
          f"agents {result['agents']}")
    print(f"max concurrent model calls : {result['max_concurrent_model_calls']}")
    print(f"tool overlap seconds       : {result['tool_overlap_seconds']}")
    print(f"tool time sum / wall       : {result['tool_seconds_sum']}s / "
          f"{result['wall_seconds']}s")
    print(f"lead last prompt tokens    : {result['lead_last_prompt_tokens']} · "
          f"children total {result['children_prompt_tokens_total']}")
    for agent, row in sorted(result["per_agent"].items()):
        print(f"  {agent} {row['role']}: {row['calls']} calls, "
              f"{row['in']} in / {row['out']} out")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
