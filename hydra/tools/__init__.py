"""Tool registry, schemas, parallel executor, output cap + spill, plugins.

Every tool returns a string, never raises into the loop; the executor converts
exceptions to ``ERROR: <type>: <message>`` + a short traceback tail.
"""
from __future__ import annotations

import importlib.util
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config as C


class Tool:
    def __init__(self, name: str, description: str, parameters: dict, fn,
                 group: str = "core"):
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.fn = fn
        self.group = group

    def schema(self) -> dict:
        return {"type": "function",
                "function": {"name": self.name, "description": self.description,
                             "parameters": self.parameters}}


class ToolContext:
    def __init__(self, rt, agent, call=None):
        self.rt = rt
        self.agent = agent
        self.call = call
        self.cfg = rt.cfg
        self.client = rt.client
        self.journal = rt.journal
        self.cwd = Path(agent.workdir)


# Focus presets (§9.6) — fewer tools = better selection by small models.
ROLE_TOOLSETS = {
    "planner": ["read_file", "ls", "glob", "grep", "web_search", "web_fetch",
                "note", "recall", "todo", "finish", "repo_map"],
    "researcher": ["web_search", "web_fetch", "http", "read_file", "grep", "ls",
                   "note", "recall", "finish"],
    "coder": ["bash", "python", "read_file", "write_file", "edit_file", "apply_patch",
              "ls", "glob", "grep", "job_status", "job_output", "job_kill", "todo",
              "note", "recall", "reload_tools", "finish"],
    "debugger": ["bash", "python", "read_file", "grep", "edit_file", "write_file",
                 "ls", "glob", "job_output", "job_kill", "todo", "note", "finish"],
    "tester": ["bash", "python", "read_file", "write_file", "edit_file", "glob",
               "grep", "finish"],
    "reviewer": ["bash", "python", "read_file", "grep", "ls", "glob", "finish"],
    "scribe": ["read_file", "write_file", "edit_file", "ls", "finish"],
    "worker": ["bash", "python", "read_file", "write_file", "edit_file", "ls",
               "grep", "glob", "todo", "note", "finish"],
}
EXTRA_TOOLS = ("spawn_agent", "spawn_swarm", "agent_status", "agent_wait",
               "agent_send", "agent_kill", "agent_result", "ask_models",
               "skill", "view_image")


def tools_for(spec, role: str):
    """'all' | 'role' | comma-list -> set of names, or None for everything."""
    if spec is None or str(spec).strip().lower() == "all":
        return None
    if str(spec).strip().lower() == "role":
        names = list(ROLE_TOOLSETS.get(role, ROLE_TOOLSETS["worker"]))
        names += list(EXTRA_TOOLS)  # only registered ones end up in schemas
        return set(names)
    return {s.strip() for s in str(spec).split(",") if s.strip()}


class Registry:
    def __init__(self, rt):
        self.rt = rt
        self.lock = threading.RLock()
        self.tools: dict = {}
        self.plugins: list = []
        self.register_builtins()
        self.load_plugins()

    # -------------------------------------------------- registration
    def register(self, tool: Tool) -> None:
        with self.lock:
            self.tools[tool.name] = tool

    def register_builtins(self) -> None:
        from . import files, meta, shell, termux, web
        for mod in (shell, files, web, termux, meta):
            mod.register(self)

    def get(self, name: str):
        with self.lock:
            return self.tools.get(name)

    def schemas_for(self, agent) -> list:
        names = tools_for(getattr(agent, "tool_names", "all"), agent.role)
        with self.lock:
            items = sorted(self.tools.items())
        return [t.schema() for n, t in items if names is None or n in names]

    # -------------------------------------------------- plugins
    def load_plugins(self) -> list:
        loaded = []
        pdir = C.home() / "tools"
        if not pdir.is_dir():
            return loaded
        for p in sorted(pdir.glob("*.py")):
            try:
                spec = importlib.util.spec_from_file_location(f"hydra_plugin_{p.stem}", p)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                ts = mod.SPEC
                fn = mod.run
                tool = Tool(ts["name"], ts.get("description", ""), ts.get("parameters") or {},
                            fn, group="plugin")
                self.register(tool)
                loaded.append(tool.name)
                with self.lock:
                    if p.name not in self.plugins:
                        self.plugins.append(p.name)
            except Exception:
                continue
        return loaded

    def reload(self) -> list:
        self.register_builtins()
        return self.load_plugins()


class Executor:
    def __init__(self, rt):
        self.rt = rt
        n = max(1, int(C.get(rt.cfg, "loop.parallel_tools", 4) or 4))
        self.pool = ThreadPoolExecutor(max_workers=n, thread_name_prefix="tool")

    def run(self, calls: list, agent) -> list:
        """Run calls (possibly in parallel), return tool messages in call order."""
        results = [None] * len(calls)
        futures = {}
        for i, call in enumerate(calls):
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            if self.rt.journal:
                self.rt.journal.event("tool_call", agent.id, tool=name, args=args, idx=i)
            futures[self.pool.submit(self._invoke, name, args, agent)] = i
        for fut, i in futures.items():
            try:
                results[i] = fut.result()
            except Exception as exc:  # never reachable; _invoke swallows
                results[i] = f"ERROR: {type(exc).__name__}: {exc}"
        out = []
        for call, content in zip(calls, results):
            name = call["function"]["name"]
            content = self._cap(content, agent, name)
            if self.rt.journal:
                self.rt.journal.event("tool_result", agent.id, tool=name,
                                      chars=len(content), preview=content[:400])
            out.append({"role": "tool", "tool_name": name, "content": content})
        return out

    def _invoke(self, name: str, args: dict, agent) -> str:
        tool = self.rt.tools.get(name)
        if tool is None:
            names = ", ".join(sorted(self.rt.tools.tools.keys()))
            return f"ERROR: unknown tool '{name}'. Available tools: {names}"
        ctx = ToolContext(self.rt, agent, {"name": name, "arguments": args})
        try:
            out = tool.fn(args or {}, ctx)
            return out if isinstance(out, str) else str(out)
        except Exception as exc:
            tb = traceback.format_exc(limit=3)
            return f"ERROR: {type(exc).__name__}: {exc}\n{tb[-600:]}"

    def _cap(self, text: str, agent, name: str) -> str:
        text = text if isinstance(text, str) else str(text)
        cap = int(C.get(self.rt.cfg, "loop.tool_cap", 6000) or 6000)
        if len(text) <= cap:
            return text
        spill_dir = None
        if self.rt.journal:
            spill_dir = self.rt.journal.dir / "spill"
            spill_dir.mkdir(parents=True, exist_ok=True)
        path = None
        if spill_dir is not None:
            path = spill_dir / f"{agent.id}-{self.rt.journal.seq:06d}-{name}.txt"
            try:
                path.write_text(text, encoding="utf-8")
            except Exception:
                path = None
        head = text[:2500]
        tail = text[-2500:]
        omitted = len(text) - len(head) - len(tail)
        if path is not None:
            return f"{head}\n[... {omitted} chars omitted; full output: {path}]\n{tail}"
        return f"{head}\n[... {omitted} chars omitted ...]\n{tail}"
