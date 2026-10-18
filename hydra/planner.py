"""Planner DAG (§19.10): one tool-less call with a JSON schema.

``format`` is only ever sent on calls without tools, per the spec. If the
model ignores or rejects the schema, the tolerant JSON extractor takes over;
if that also fails the caller just falls back to plain steering.
"""
from __future__ import annotations

from .agent import parse_json_blob

DAG_SCHEMA = {
    "type": "object",
    "properties": {
        "goal": {"type": "string"},
        "steps": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "id": {"type": "integer"},
                "title": {"type": "string"},
                "deps": {"type": "array", "items": {"type": "integer"}},
                "role": {"type": "string"},
            },
            "required": ["id", "title"],
        }},
    },
    "required": ["goal", "steps"],
}


def make_dag(rt, task: str, role: str = "planner"):
    """Return (dag_dict, "") or (None, error_text). Never raises."""
    prompt = (
        "Break this task into a small DAG of concrete steps. For each step give "
        "id (int), title (imperative, one line), deps (list of step ids that must "
        "finish first) and role (worker|coder|researcher|debugger|tester|reviewer). "
        "3-9 steps, no prose. Return JSON only.\n\nTASK:\n" + task[:4000])
    try:
        reply = rt.router.chat([{"role": "user", "content": prompt}],
                               role=role, think="low", format=DAG_SCHEMA)
    except Exception as exc:
        return None, f"planner unavailable: {type(exc).__name__}: {exc}"
    obj = parse_json_blob(reply.content or "")
    if not isinstance(obj, dict) or not isinstance(obj.get("steps"), list) \
            or not obj["steps"]:
        return None, "planner returned no DAG"
    return obj, ""


def render(dag: dict) -> str:
    lines = [f"goal: {dag.get('goal') or '?'}"]
    for s in dag.get("steps") or []:
        try:
            sid = s.get("id")
            deps = s.get("deps") or []
            role = s.get("role") or ""
            title = s.get("title") or ""
            dep = f" deps={','.join(str(d) for d in deps)}" if deps else ""
            r = f" [{role}]" if role else ""
            lines.append(f"  {sid}. {title}{r}{dep}")
        except Exception:
            lines.append(f"  {s}")
    return "\n".join(lines)
