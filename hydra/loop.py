"""The agent loop — identical for lead and sub-agents (§5 of the spec).

while True: absorb inbox -> maybe compact -> one model call (streamed through
the router) -> run the tool calls (parallel) -> handle finish -> stall check ->
journal a turn boundary. The stall detector, not a counter, stops runaway loops.
"""
from __future__ import annotations

import time

from . import config as C
from . import verifier
from .agent import normalize_call, parse_text_calls
from .client import ContextOverflow
from .router import AllModelsFailed


def run_agent(agent, rt) -> dict:
    cfg = rt.cfg
    max_turns = int(C.get(cfg, "loop.max_turns", 0) or 0)
    agent.status = "running"
    if rt.journal:
        rt.journal.event("agent_start", agent.id, role=agent.role, depth=agent.depth,
                         parent=agent.parent_id)
    try:
        while True:
            if agent.cancelled.is_set():
                agent.status = "killed"
                agent.set_report("killed", "Cancelled.")
                break
            paused = getattr(rt, "paused", None)
            if paused is not None and paused.is_set():
                while paused.is_set() and not agent.cancelled.is_set():
                    time.sleep(0.2)
            agent.absorb_inbox()
            agent.maybe_compact(rt)
            if max_turns and agent.turns >= max_turns:
                agent.set_report("partial", f"Turn cap reached ({max_turns}).")
                break
            agent.turns += 1

            try:
                reply = rt.router.chat(
                    messages=agent.api_messages(), role=agent.role,
                    tools=rt.tools.schemas_for(agent), think=agent.think,
                    agent=agent, exclude=tuple(agent.exclude_models))
            except ContextOverflow:
                if not agent.compact(rt):
                    raise
                agent.turns -= 1  # a forced compaction retry is not a new turn
                continue

            calls = [c for c in (normalize_call(tc) for tc in (reply.tool_calls or [])) if c]
            from_text = False
            if not calls:
                parsed = parse_text_calls(reply.content)
                if parsed:
                    calls = parsed
                    from_text = True
            agent.append(reply.as_message())
            agent.data["last_model"] = getattr(reply, "model", None)
            if from_text and rt.journal:
                rt.journal.event("parsed_from_text", agent.id, n=len(calls))

            if not calls:
                if agent.nudges < 1:
                    agent.nudge("No tool call detected. Continue the task with tools, "
                                "or call finish(status, summary) if you are done.")
                    continue
                agent.finish_from_text(reply.content)
                break

            agent.nudges = 0
            results = rt.executor.run(calls, agent)
            agent.append_tool_results(results)

            if agent.finished:
                # independent verification (§5.3): a failed review puts the
                # agent back in the loop with the issues injected as steering
                if verifier.run_once(rt, agent):
                    continue
                break
            agent.stall_check(rt, calls, results)
            if rt.journal:
                rt.journal.turn_boundary()
    except AllModelsFailed as exc:
        agent.status = "failed"
        if not agent.report:
            if getattr(exc, "all_paid", False):
                agent.set_report("failed", "Parked: free credits look exhausted "
                                           "(or every model is paid for this key). "
                                           "Run `hydra resume` after the monthly reset.")
            else:
                agent.set_report("failed", f"All models failed: {exc}")
        raise
    except Exception as exc:
        agent.status = "failed"
        if rt.journal:
            rt.journal.event("error", agent.id, error=f"{type(exc).__name__}: {exc}")
        if not agent.report:
            agent.set_report("failed", f"Error: {type(exc).__name__}: {exc}")
        raise

    if agent.status == "running":
        st = (agent.report or {}).get("status")
        agent.status = "done" if st in ("done", "partial") else "failed"
    report = agent.report_out()
    if rt.journal:
        rt.journal.event("report", agent.id, report=report)
        rt.journal.turn_boundary()
    return report
