"""The agent loop — identical for lead and sub-agents (§5 of the spec).

while True: absorb inbox -> maybe compact -> one model call (streamed through
the router) -> run the tool calls (parallel) -> post-edit hooks / auto-verify ->
handle finish -> stall check -> journal a turn boundary. The stall detector,
not a counter, stops runaway loops. A first Ctrl-C (CallCancelled) cancels the
in-flight call or tool and the run stays alive; only the CLI decides when a
second press should exit.
"""
from __future__ import annotations

import time

from . import config as C
from . import hooks
from . import limits as Limits
from . import verifier
from .agent import normalize_call, parse_text_calls
from .client import CallCancelled, ContextOverflow
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
            try:
                if agent.cancelled.is_set():
                    agent.status = "killed"
                    agent.set_report("killed", "Cancelled.")
                    break
                if rt.budget_exceeded():
                    rt.budget_hit = True
                    raise Limits.BudgetExceeded(
                        f"spend cap ${rt.budget_usd:.4f} reached")
                paused = getattr(rt, "paused", None)
                if paused is not None and paused.is_set():
                    while paused.is_set() and not agent.cancelled.is_set():
                        time.sleep(0.2)
                agent.absorb_inbox()
                agent.maybe_compact(rt)
                if max_turns and agent.turns >= max_turns:
                    agent.set_report("partial", f"Turn cap reached ({max_turns}).")
                    break
                if max_turns:
                    left = max_turns - agent.turns
                    if 0 < left <= 2 and not agent.data.get("cap_nudged"):
                        agent.data["cap_nudged"] = True
                        agent.steer(
                            f"Turn cap: {left} turn(s) left. Stop exploring. Call "
                            f"finish now with an honest status, summary, "
                            f"files_changed and any remaining todos/open issues.")
                agent.turns += 1
                if agent.turns == 1:
                    # §19.9: capture the pre-run tree as turn 0 so the whole
                    # run can be undone, not just its last turn. Root agent
                    # only; a no-op when journal.git_snapshots is off.
                    try:
                        from . import snapshots
                        snapshots.turn_snapshot(rt, agent, force=True, turn=0)
                    except Exception:
                        pass

                try:
                    cb = getattr(rt.ui, "delta", None)
                    on_delta = ((lambda kind, text: cb(agent.id, kind, text))
                                if callable(cb) else None)
                    reply = rt.router.chat(
                        messages=agent.api_messages(), role=agent.role,
                        tools=rt.tools.schemas_for(agent), think=agent.think,
                        agent=agent, exclude=tuple(agent.exclude_models),
                        on_delta=on_delta)
                except ContextOverflow:
                    if not agent.compact(rt):
                        raise
                    agent.turns -= 1  # a forced compaction retry is not a new turn
                    continue

                if rt.budget_exceeded():
                    rt.budget_hit = True
                    raise Limits.BudgetExceeded(
                        f"spend cap ${rt.budget_usd:.4f} reached")

                calls = [c for c in (normalize_call(tc) for tc in (reply.tool_calls or [])) if c]
                from_text = False
                if not calls:
                    parsed = parse_text_calls(reply.content)
                    if parsed:
                        calls = parsed
                        from_text = True
                agent.append(reply.as_message())
                agent.data["last_model"] = getattr(reply, "model", None)
                # the actual answer must be visible in the UI / journal; before
                # this event the CLI only ever showed tool noise and a summary.
                if rt.journal and (reply.content or "").strip():
                    rt.journal.event(
                        "assistant", agent.id, text=reply.content[:50000],
                        chars=len(reply.content),
                        model=getattr(reply, "model", None),
                        ms=getattr(reply, "ms", None))
                if from_text and rt.journal:
                    rt.journal.event("parsed_from_text", agent.id, n=len(calls))

                if not calls:
                    if (agent.data.get("server_finish_nudged")
                            and not agent.data.get("server_finish_partial")):
                        # Q2: after the one server nudge, a prose reply is the
                        # final answer.  Do not let finish_from_text turn a dead
                        # server into a silent "done".
                        verifier.finish_after_server_nudge(agent)
                        break
                    if agent.nudges < 1:
                        agent.nudge("No tool call detected. Continue the task with tools, "
                                    "or call finish(status, summary) if you are done.")
                        continue
                    agent.finish_from_text(reply.content)
                    break

                agent.nudges = 0
                results = rt.executor.run(calls, agent)
                # observers, never gates: post_edit hook + auto-verify land in
                # the tool results (before they are journaled to the transcript)
                try:
                    hooks.apply_turn_hooks(rt, agent, calls, results)
                except Exception:
                    pass
                if getattr(rt, "sigint", None) is not None and rt.sigint.is_set():
                    rt.sigint.clear()  # an interrupted tool was killed; back to normal
                agent.append_tool_results(results)

                # §19.9: snapshot after every turn with tree changes, including
                # the turn that finishes the run.
                try:
                    from . import snapshots
                    snapshots.turn_snapshot(rt, agent)  # root agent only
                except Exception:
                    pass
                if agent.finished:
                    # independent verification (§5.3): a failed review puts the
                    # agent back in the loop with the issues injected as steering
                    if verifier.run_once(rt, agent):
                        continue
                    break
                agent.stall_check(rt, calls, results)
                if rt.journal:
                    rt.journal.turn_boundary()
                if agent.finished:
                    # hard loop breaker set a partial report; do not call the
                    # model again just to discover that the run is over
                    break
            except CallCancelled as exc:
                # first Ctrl-C: the in-flight call/tool is gone, the run lives on
                time.sleep(0.5)  # give an in-flight tool a moment to notice and die
                if getattr(rt, "sigint", None) is not None:
                    rt.sigint.clear()
                if rt.journal:
                    rt.journal.event("sigint", agent.id, note=str(exc))
                last = agent.messages[-1] if agent.messages else None
                if (last or {}).get("role") == "assistant" and (last or {}).get("tool_calls"):
                    for tc in last.get("tool_calls") or []:
                        name = (tc.get("function") or {}).get("name") or "?"
                        msg = {"role": "tool", "tool_name": name,
                               "content": "[interrupted by Ctrl-C: this tool call was "
                                          "cancelled; background effects unknown]"}
                        if tc.get("id"):
                            msg["tool_call_id"] = str(tc["id"])
                        agent.append(msg)
                agent.turns = max(0, agent.turns - 1)
                agent.steer("[harness] Ctrl-C: the in-flight work was cancelled. "
                            "Continue the task; re-check state if a command may have run.")
                continue
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
    if int(getattr(agent, "depth", 0) or 0) > 0 and not agent.report \
            and getattr(agent, "turns", 0) > 0 and agent.status != "killed":
        # sub-agent report as a structured tool-less call (§19.10)
        agent.synthesize_report(rt)
    report = agent.report_out()
    if rt.journal:
        rt.journal.event("report", agent.id, report=report)
        rt.journal.turn_boundary()
    try:
        hooks.run_hook(rt, "agent_done", agent,
                       result_preview=str((report or {}).get("summary", "")))
        if int(getattr(agent, "depth", 0) or 0) == 0:
            hooks.run_hook(rt, "finish", agent,
                           result_preview=str((report or {}).get("summary", "")))
    except Exception:
        pass
    return report
