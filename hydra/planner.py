"""Planner DAG (§19.10): one tool-less call with a JSON schema.

``format`` is only ever sent on calls without tools, per the spec. If the
model ignores or rejects the schema, the tolerant JSON extractor takes over;
if that also fails the caller just falls back to plain steering.
"""
from __future__ import annotations

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
    from . import jsonroute
    res = jsonroute.structured_chat(
        rt.router, [{"role": "user", "content": prompt}], role=role,
        schema=DAG_SCHEMA, schema_name="plan", think="low", task=task)
    if res.obj is None:
        return None, f"planner unavailable: {res.error}"
    obj = res.obj
    # Models frequently ignore the wrapper: accept a bare list of steps too.
    if isinstance(obj, list):
        obj = {"goal": task.strip().splitlines()[0][:160] if task.strip() else "", "steps": obj}
    if not isinstance(obj, dict):
        return None, "planner returned no DAG"
    steps = obj.get("steps")
    if isinstance(steps, dict):
        steps = [steps]
    if not isinstance(steps, list) or not steps:
        return None, "planner returned no DAG"
    clean = []
    for i, step in enumerate(steps, 1):
        if not isinstance(step, dict):
            continue
        title = str(step.get("title") or step.get("step") or "").strip()
        if not title:
            continue
        try:
            sid = int(step.get("id") if step.get("id") is not None else i)
        except (TypeError, ValueError):
            sid = i
        raw_deps = step.get("deps") or []
        if not isinstance(raw_deps, list):
            raw_deps = []
        deps = [int(x) for x in raw_deps if str(x).lstrip("-").isdigit()]
        role = str(step.get("role") or "").strip()
        if role not in ("worker", "coder", "researcher", "debugger", "tester", "reviewer"):
            role = "worker"
        clean.append({"id": sid, "title": title,
                      "deps": [d for d in deps if d != sid], "role": role})
    if not clean:
        return None, "planner returned no DAG"
    goal = str(obj.get("goal") or (task.strip().splitlines()[0] if task.strip() else ""))[:200]
    return {"goal": goal, "steps": clean}, ""


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
