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
