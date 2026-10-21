"""Tool registry, schemas, parallel executor, output cap + spill, plugins.

Every tool returns a string, never raises into the loop; the executor converts
exceptions to ``ERROR: <type>: <message>`` + a short traceback tail.
"""
from __future__ import annotations

import copy
import importlib.util
import re
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .. import config as C

_PATCH_FILE_RE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.M)


def _record_writes(agent, name: str, args: dict) -> None:
    """Remember which files a successful tool call wrote.

    `finish` uses this to auto-fill files_changed, and the verifier can trust
    the report instead of guessing from prose.
    """
    paths = []
    if name in ("write_file", "edit_file"):
        p = args.get("path")
        if p:
            paths.append(str(p))
    elif name == "apply_patch":
        for m in _PATCH_FILE_RE.finditer(str(args.get("patch") or "")):
            paths.append(m.group(1).strip())
    if not paths:
        return
    try:
        wrote = agent.data.setdefault("files_written", [])
        for p in paths:
            if p and p not in wrote:
                wrote.append(p)
    except Exception:
        pass


def _is_write(name: str, args: dict) -> bool:
    try:
        from ..artifacts import is_write_call
        return is_write_call(name, args)
    except Exception:
        return name in ("write_file", "edit_file", "apply_patch")


class Tool:
    def __init__(self, name: str, description: str, parameters: dict, fn,
                 group: str = "core", hide_schema: bool = False):
        self.name = name
        self.description = description
        self.parameters = parameters or {"type": "object", "properties": {}}
        self.fn = fn
        self.group = group
        # Hidden tools stay callable (and testable) but cost no prompt tokens;
        # the umbrella `termux` tool exposes their actions instead.
        self.hide_schema = bool(hide_schema)

    def schema(self) -> dict:
        # B2: keep the gist in every description; verbose tails belong in the
        # docs, not in the first prompt of every free-model turn. Optional
        # parameter help is dropped too (types stay).
        desc = " ".join(str(self.description or "").split())
        if len(desc) > 56:
            desc = desc[:55].rstrip(" ,;:.") + "…"
        params = copy.deepcopy(self.parameters or {})
        required = set(params.get("required") or [])
        for key, prop in (params.get("properties") or {}).items():
            if key not in required and isinstance(prop, dict):
                prop.pop("description", None)
        return {"type": "function",
                "function": {"name": self.name, "description": desc,
                             "parameters": params}}


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
               "skill", "view_image", "repo_map")


def tools_for(spec, role: str):
    """'all' | 'role' | comma-list -> set of names, or None for everything."""
    if spec is None or str(spec).strip().lower() == "all":
        return None
    if str(spec).strip().lower() == "role":
        names = list(ROLE_TOOLSETS.get(role, ROLE_TOOLSETS["worker"]))
        names += list(EXTRA_TOOLS)  # only registered ones end up in schemas
        return set(names)
    return {s.strip() for s in str(spec).split(",") if s.strip()}


def caps_allowed(name: str, caps: dict) -> bool:
    """F6: drop tools whose backing capability auto-detect found dead.

    Missing/unknown caps (None/{}) fail open — the tool stays available.
    """
    if not caps:
        return True
    if name == "web_search":
        return bool(caps.get("web_search", True))
    if name == "web_fetch":
        return bool(caps.get("web_fetch", True))
    if name == "termux":
        return bool(caps.get("termux_api", True))
    if name == "view_image":
        return bool(caps.get("vision", True))
    if name.startswith("termux_"):
        return bool(caps.get("termux_api", True))
    return True


class Registry:
    def __init__(self, rt):
        self.rt = rt
        self.lock = threading.RLock()
        self.tools: dict = {}
        self.plugins: list = []
        self.register_builtins()
        self.load_plugins()
        self.mcp = None
        try:
            from .. import mcp as mcp_mod
            if C.get(rt.cfg, "mcp.servers", {}) or {}:
                self.mcp = mcp_mod.McpManager(rt)
                self.mcp.register_all(self)
        except Exception:
            self.mcp = None

    # -------------------------------------------------- registration
    def register(self, tool: Tool) -> None:
        with self.lock:
            self.tools[tool.name] = tool

    def register_builtins(self) -> None:
        from . import files, meta, shell, termux, vision, web
        for mod in (shell, files, web, termux, meta, vision):
            mod.register(self)

    def get(self, name: str):
        with self.lock:
            return self.tools.get(name)

    def schemas_for(self, agent) -> list:
        names = tools_for(getattr(agent, "tool_names", "all"), agent.role)
        role = getattr(agent, "role", "")
        try:
            from .. import mcp as mcp_mod
            mcp_ok = mcp_mod.role_ok(self.rt.cfg, role)
        except Exception:
            mcp_ok = True
        with self.lock:
            items = sorted(self.tools.items())
        caps = getattr(self.rt, "caps", None) or {}
        out = []
        for n, t in items:
            is_mcp = n.startswith("mcp__")
            if is_mcp and not mcp_ok:
                continue
            if names is not None and n not in names and not is_mcp:
                continue
            if getattr(t, "hide_schema", False):
                continue
            if not caps_allowed(n, caps):
                continue
            out.append(t.schema())
        return out

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
                tools = []
                multi = getattr(mod, "TOOLS", None)
                if isinstance(multi, (list, tuple)) and callable(getattr(mod, "run", None)):
                    # multi-tool plugin contract (§19.12): run(name, args, ctx)
                    for ts in multi:
                        if not isinstance(ts, dict) or not str(ts.get("name") or "").strip():
                            continue

                        def _multi(args, ctx, _name=str(ts["name"]), _mod=mod):
                            return _mod.run(_name, args, ctx)

                        tools.append(Tool(str(ts["name"]), ts.get("description", ""),
                                          ts.get("parameters") or {}, _multi,
                                          group="plugin"))
                else:
                    ts = mod.SPEC
                    fn = mod.run
                    tools.append(Tool(ts["name"], ts.get("description", ""),
                                      ts.get("parameters") or {}, fn, group="plugin"))
                for tool in tools:
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
        # agent_* tools may block (agent_wait) — keep them off the main pool so
        # a blocked parent can never starve its children's tool calls.
        self.agent_pool = ThreadPoolExecutor(max_workers=16,
                                             thread_name_prefix="agenttool")

    def _pool_for(self, name: str):
        tool = self.rt.tools.get(name)
        if tool is not None and getattr(tool, "group", "") == "agents":
            return self.agent_pool
        return self.pool

    def run(self, calls: list, agent) -> list:
        """Run calls (possibly in parallel), return tool messages in call order."""
        results = [None] * len(calls)
        elapsed = [0] * len(calls)
        futures = {}
        for i, call in enumerate(calls):
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            if self.rt.journal:
                self.rt.journal.event("tool_call", agent.id, tool=name, args=args, idx=i)
            futures[self._pool_for(name).submit(self._timed, name, args, agent)] = i
        for fut, i in futures.items():
            try:
                content, ms = fut.result()
                results[i] = content
                elapsed[i] = int(ms)
            except Exception as exc:  # never reachable; _invoke swallows
                results[i] = f"ERROR: {type(exc).__name__}: {exc}"
        out = []
        for i, (call, content) in enumerate(zip(calls, results)):
            name = call["function"]["name"]
            args = call["function"]["arguments"]
            failed = str(content).startswith("ERROR:")
            if not failed:
                _record_writes(agent, name, args)
            content = self._cap(content, agent, name)
            if self.rt.redact is not None:
                content = self.rt.redact(content)  # secrets never reach the model
            if name.startswith("browser_") and self._untrusted(name):
                content = self._wrap_untrusted(content, name)
            ev_seq = None
            changed = bool(not failed and _is_write(name, args))
            if self.rt.journal:
                ev = self.rt.journal.event("tool_result", agent.id, tool=name,
                                           chars=len(content), ms=elapsed[i],
                                           lines=content.count("\n") + 1,
                                           changed=changed,
                                           preview=content[:400])
                ev_seq = (ev or {}).get("seq")
            message = {"role": "tool", "tool_name": name, "content": content,
                       "ev": ev_seq, "turn": int(getattr(agent, "turns", 0) or 0),
                       "changed": changed}
            if call.get("id"):
                message["tool_call_id"] = str(call["id"])
            out.append(message)
        return out

    def _timed(self, name: str, args: dict, agent):
        start = time.time()
        out = self._invoke(name, args, agent)
        return out, max(0, int((time.time() - start) * 1000))

    def _untrusted(self, name: str) -> bool:
        from .. import config as C
        from .. import hygiene
        if not C.get(self.rt.cfg, "hygiene.untrusted_tagging", True):
            return False
        tool = self.rt.tools.get(name)
        group = str(getattr(tool, "group", "") or "")
        return group == "browser" or name.startswith("browser_")

    def _wrap_untrusted(self, content: str, name: str) -> str:
        from .. import hygiene
        return hygiene.wrap_untrusted(self.rt.cfg, content, f"tool:{name}")

    def _invoke(self, name: str, args: dict, agent) -> str:
        tool = self.rt.tools.get(name)
        if tool is None:
            names = ", ".join(sorted(self.rt.tools.tools.keys()))
            return f"ERROR: unknown tool '{name}'. Available tools: {names}"
        ctx = ToolContext(self.rt, agent, {"name": name, "arguments": args})
        pre = ""
        try:
            from ..hooks import run_hook
            pre = run_hook(self.rt, "pre_tool", agent, tool=name, args=args)
        except Exception:
            pre = ""
        try:
            out = tool.fn(args or {}, ctx)
            out = out if isinstance(out, str) else str(out)
        except Exception as exc:
            tb = traceback.format_exc(limit=3)
            out = f"ERROR: {type(exc).__name__}: {exc}\n{tb[-600:]}"
        try:
            from ..hooks import run_hook
            note = run_hook(self.rt, "post_tool", agent, tool=name, args=args,
                            result_preview=out)
            if note:
                out = out + "\n[hook] " + note
        except Exception:
            pass
        return out

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
