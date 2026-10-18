"""Meta tools: todo, note/recall (blackboard + memory), finish, reload_tools."""
from __future__ import annotations

from . import Tool


def _render_todos(agent) -> str:
    todos = agent.data.get("todos") or []
    if not todos:
        return "(no todos)"
    lines = []
    done = 0
    for t in todos:
        s = str(t)
        if s.strip().lower().startswith("x "):
            done += 1
            lines.append(f"x {s[2:]}")
        else:
            lines.append(f"  {s}")
    return f"todo {done}/{len(todos)}\n" + "\n".join(lines)


def _tool_todo(args, ctx):
    items = args.get("items")
    if items is None:
        return _render_todos(ctx.agent)
    if not isinstance(items, list):
        return "ERROR: items must be a list of strings ('x ...' marks a done item)"
    ctx.agent.data["todos"] = [str(i) for i in items]
    return _render_todos(ctx.agent)


def _tool_note(args, ctx):
    key = str(args.get("key") or "").strip()
    value = str(args.get("value") or "")
    if not key:
        return "ERROR: key is required"
    persist = bool(args.get("persist"))
    if ctx.journal:
        ctx.journal.blackboard_append(key, value, agent=ctx.agent.id, persist=persist)
    return f"noted {key}" + (" (persisted to ~/.hydra/memory)" if persist else "")


def _tool_recall(args, ctx):
    key = args.get("key")
    entries = ctx.journal.blackboard_read(str(key) if key else None) if ctx.journal else []
    if not entries:
        return "(nothing)"
    return "\n".join(f"{e.get('key')} = {e.get('value')}" for e in entries[-25:])


def _tool_finish(args, ctx):
    status = str(args.get("status") or "done")
    summary = str(args.get("summary") or "")
    ctx.agent.set_report(
        status, summary,
        files_changed=args.get("files_changed"),
        commands_run=args.get("commands_run"),
        findings=args.get("findings"),
        open_issues=args.get("open_issues"),
        artifacts=args.get("artifacts"))
    return f"finish recorded (status={ctx.agent.report['status']}). The run stops after this turn."


def _tool_reload_tools(args, ctx):
    loaded = ctx.rt.tools.reload()
    return f"tools reloaded; plugins loaded: {', '.join(loaded) if loaded else '(none)'}"


# ------------------------------------------------------ sub-agents (§9.2)

def _tool_spawn_agent(args, ctx):
    swarm = getattr(ctx.rt, "swarm", None)
    if swarm is None:
        return "ERROR: swarm is not available in this runtime"
    task = args.get("task")
    if not task:
        return "ERROR: task is required"
    try:
        agent = swarm.spawn(ctx.agent, task,
                            role=str(args.get("role") or "worker"),
                            model=args.get("model"),
                            tools=args.get("tools") or "role",
                            context_files=args.get("context_files"))
    except ValueError as exc:
        return f"ERROR: {exc}"
    if args.get("wait", True):
        swarm.wait([agent.id])
        return swarm.format_report(agent.id)
    return (f"spawned {agent.id} ({agent.role}, depth {agent.depth}) in background; "
            f"use agent_status / agent_wait / agent_result")


def _tool_spawn_swarm(args, ctx):
    swarm = getattr(ctx.rt, "swarm", None)
    if swarm is None:
        return "ERROR: swarm is not available in this runtime"
    tasks = args.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        return "ERROR: tasks must be a non-empty list of strings or task objects"
    try:
        return swarm.spawn_many(ctx.agent, tasks,
                                role=str(args.get("role") or "worker"),
                                tools=args.get("tools") or "role",
                                max_parallel=args.get("max_parallel"),
                                join=args.get("join") or "all",
                                timeout=args.get("timeout"))
    except ValueError as exc:
        return f"ERROR: {exc}"


def _tool_agent_status(args, ctx):
    swarm = getattr(ctx.rt, "swarm", None)
    if swarm is None:
        return "ERROR: swarm is not available in this runtime"
    ids = args.get("ids")
    return swarm.status_lines(list(ids) if ids else None)


def _tool_agent_wait(args, ctx):
    swarm = getattr(ctx.rt, "swarm", None)
    if swarm is None:
        return "ERROR: swarm is not available in this runtime"
    ids = args.get("ids")
    if not ids:
        return "ERROR: ids is required"
    lines = swarm.wait(list(ids), timeout=args.get("timeout"))
    return lines + "\n\n" + swarm.status_lines(list(ids))


def _tool_agent_send(args, ctx):
    swarm = getattr(ctx.rt, "swarm", None)
    if swarm is None:
        return "ERROR: swarm is not available in this runtime"
    if not args.get("id") or args.get("message") is None:
        return "ERROR: id and message are required"
    return swarm.send(str(args["id"]), str(args["message"]))


def _tool_agent_kill(args, ctx):
    swarm = getattr(ctx.rt, "swarm", None)
    if swarm is None:
        return "ERROR: swarm is not available in this runtime"
    if not args.get("id"):
        return "ERROR: id is required"
    return swarm.kill(str(args["id"]))


def _tool_agent_result(args, ctx):
    swarm = getattr(ctx.rt, "swarm", None)
    if swarm is None:
        return "ERROR: swarm is not available in this runtime"
    if not args.get("id"):
        return "ERROR: id is required"
    return swarm.format_report(str(args["id"]))


# ------------------------------------------------------ skills (§19.2)

def _tool_skill(args, ctx):
    from ..project import find_skills
    skills = find_skills(ctx.agent.workdir)
    name = str(args.get("name") or "").strip()
    if not name:
        if not skills:
            return "no skills installed (~/.hydra/skills/<name>/SKILL.md)"
        return ("usage: skill(name). Available:\n"
                + "\n".join(f"- {n} — {s['description']}" if s["description"] else f"- {n}"
                            for n, s in sorted(skills.items())))
    s = skills.get(name)
    if s is None:
        avail = ", ".join(sorted(skills)) or "(none)"
        return f"ERROR: unknown skill '{name}'. Available: {avail}"
    body = s["body"] or "(empty skill)"
    return f"{body}\n\n[skill dir: {s['dir']}]"


# ------------------------------------------------------ multi-model (§7.5)

def _tool_ask_models(args, ctx):
    prompt = args.get("prompt") or args.get("question")
    if not prompt:
        return "ERROR: prompt is required"
    mode = str(args.get("mode") or "vote").lower()
    models = args.get("models")
    if models is not None and not isinstance(models, list):
        return "ERROR: models must be a list of model names"
    try:
        out = ctx.rt.router.multi(
            prompt=str(prompt), mode=mode,
            role=str(args.get("role") or "lead"),
            models=[str(m) for m in (models or [])],
            n=args.get("n"), accept=args.get("accept"),
            rounds=int(args.get("rounds") or 2), agent=ctx.agent)
    except Exception as exc:
        return f"ERROR: ask_models({mode}) failed: {type(exc).__name__}: {exc}"
    text = str(out.get("text") or "")
    return (f"{text}\n\n[ask_models: mode={out.get('mode')} "
            f"model={out.get('model')} "
            f"judge={out.get('judge') or out.get('aggregator') or '-'}]")


def register(reg) -> None:
    reg.register(Tool(
        "todo", "Persisted plan tracker. Pass items to set ('x done item' marks done); "
                "call with no args to read. Shown on the status line.",
        {"type": "object", "properties": {"items": {"type": "array",
                                                    "items": {"type": "string"}}}},
        _tool_todo))
    reg.register(Tool(
        "note", "Write a key/value note to the blackboard (persist=true also saves it "
                "to durable memory).",
        {"type": "object", "properties": {"key": {"type": "string"},
                                          "value": {"type": "string"},
                                          "persist": {"type": "boolean"}},
         "required": ["key", "value"]},
        _tool_note))
    reg.register(Tool(
        "recall", "Read notes from the blackboard (all, or one key).",
        {"type": "object", "properties": {"key": {"type": "string"}}},
        _tool_recall))
    reg.register(Tool(
        "finish", "End your run with a report. status: done|partial|failed; summary "
                  "≤ 300 words; plus files_changed, commands_run, findings, open_issues.",
        {"type": "object", "properties": {
            "status": {"type": "string"}, "summary": {"type": "string"},
            "files_changed": {"type": "array", "items": {"type": "string"}},
            "commands_run": {"type": "array", "items": {"type": "string"}},
            "findings": {"type": "array", "items": {"type": "string"}},
            "open_issues": {"type": "array", "items": {"type": "string"}},
            "artifacts": {"type": "array", "items": {"type": "string"}}},
         "required": ["status", "summary"]},
        _tool_finish))
    reg.register(Tool(
        "reload_tools", "Rescan ~/.hydra/tools/*.py and reload plugin tools.",
        {"type": "object", "properties": {}},
        _tool_reload_tools))

    # ---------------------------------------------- sub-agents (§9.2)
    reg.register(Tool(
        "spawn_agent",
        "Delegate one self-contained task to a sub-agent with its own context. "
        "The sub-agent sees ONLY what you write here. Returns its report.",
        {"type": "object", "properties": {
            "task": {"type": "string", "description": "Complete instructions, "
                     "inputs, paths, and what 'done' looks like."},
            "role": {"type": "string",
                     "enum": ["worker", "researcher", "coder", "debugger",
                              "tester", "reviewer", "scribe", "planner"]},
            "model": {"type": "string", "description": "Optional override. "
                      "Default: the role's chain."},
            "tools": {"type": "string", "description": "'role' (preset, default) "
                      "| 'all' | comma-separated names"},
            "context_files": {"type": "array", "items": {"type": "string"}},
            "wait": {"type": "boolean", "default": True}},
         "required": ["task"]},
        _tool_spawn_agent, group="agents"))
    reg.register(Tool(
        "spawn_swarm",
        "Launch several sub-agents at once and join as specified. Each task is a "
        "string or {task, role, model, tools}.",
        {"type": "object", "properties": {
            "tasks": {"type": "array", "items": {}},
            "role": {"type": "string"},
            "tools": {"type": "string"},
            "max_parallel": {"type": "integer"},
            "join": {"type": "string", "enum": ["all", "first", "quorum"]},
            "timeout": {"type": "number"}},
         "required": ["tasks"]},
        _tool_spawn_swarm, group="agents"))
    reg.register(Tool(
        "agent_status", "One-line status per agent (id, status, depth, role, "
        "tokens, task).",
        {"type": "object", "properties": {"ids": {"type": "array",
                                                  "items": {"type": "string"}}}},
        _tool_agent_status, group="agents"))
    reg.register(Tool(
        "agent_wait", "Block until the listed agents finish (optional timeout).",
        {"type": "object", "properties": {
            "ids": {"type": "array", "items": {"type": "string"}},
            "timeout": {"type": "number"}},
         "required": ["ids"]},
        _tool_agent_wait, group="agents"))
    reg.register(Tool(
        "agent_send", "Steer a running agent; the message is delivered at its "
        "next turn boundary.",
        {"type": "object", "properties": {"id": {"type": "string"},
                                          "message": {"type": "string"}},
         "required": ["id", "message"]},
        _tool_agent_send, group="agents"))
    reg.register(Tool(
        "agent_kill", "Stop an agent (and its children) at its next boundary; "
        "its background jobs are killed.",
        {"type": "object", "properties": {"id": {"type": "string"}},
         "required": ["id"]},
        _tool_agent_kill, group="agents"))
    reg.register(Tool(
        "agent_result", "Fetch a (finished or running) agent's report again.",
        {"type": "object", "properties": {"id": {"type": "string"}},
         "required": ["id"]},
        _tool_agent_result, group="agents"))
    reg.register(Tool(
        "ask_models",
        "Ask several models the same question with a strategy and get one answer "
        "back. Degrades safely on a single free-plan slot.",
        {"type": "object", "properties": {
            "prompt": {"type": "string"},
            "mode": {"type": "string",
                     "enum": ["vote", "race", "moa", "critic", "cascade"]},
            "models": {"type": "array", "items": {"type": "string"},
                       "description": "Optional explicit models (default: the role chain)."},
            "role": {"type": "string"},
            "n": {"type": "integer", "description": "How many models to use."},
            "accept": {"type": "string",
                       "description": "cascade only: nonempty | json | regex:<pattern>"},
            "rounds": {"type": "integer", "description": "critic only (default 2)."}},
         "required": ["prompt"]},
        _tool_ask_models, group="core"))
    reg.register(Tool(
        "skill",
        "Load a skill's full SKILL.md instructions by name. Skills are listed "
        "with one-line descriptions in the env block.",
        {"type": "object", "properties": {
            "name": {"type": "string", "description": "Skill name (omit to list)."}}},
        _tool_skill))
