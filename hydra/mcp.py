"""MCP stdio client (§19.5).

Config::

    [mcp.servers.example]
    command = ["npx", "-y", "some-mcp-server"]
    env = {}
    eager = false

Newline-delimited JSON-RPC 2.0 over subprocess pipes:
``initialize`` -> ``notifications/initialized`` -> ``tools/list`` / ``tools/call``.

On first use, or when ``command`` changes, the server is started once, its
tools are listed and the schemas cached in ``~/.hydra/mcp_cache/``. After that
servers start lazily on their first call. Each tool is exposed as
``mcp__<server>__<tool>``; schemas over 2000 chars are summarized or dropped.
Only roles with ``mcp = true`` see them (default: lead, coder, researcher).
"""
from __future__ import annotations

import json
import os
import re
import select
import shlex
import subprocess
import threading
import time
from pathlib import Path

from . import config as C

DEFAULT_MCP_ROLES = ("lead", "coder", "researcher")
SCHEMA_CHARS = 2000


def role_ok(cfg: dict, role: str) -> bool:
    v = C.get(cfg, f"roles.{role}.mcp", None)
    if v is None:
        return str(role) in DEFAULT_MCP_ROLES
    return bool(v)


def _cmd_list(raw):
    if isinstance(raw, (list, tuple)):
        return [str(x) for x in raw]
    if isinstance(raw, str) and raw.strip():
        try:
            return shlex.split(raw)
        except Exception:
            return raw.split()
    return []


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name))[:80]


def _summarize_schema(schema):
    """Keep schemas under SCHEMA_CHARS, else summarize, else drop (None)."""
    if not isinstance(schema, dict):
        return {"type": "object", "properties": {}}
    try:
        if len(json.dumps(schema)) <= SCHEMA_CHARS:
            return schema
    except Exception:
        return None
    props = schema.get("properties") or {}
    slim = {"type": "object", "properties": {},
            "required": list(schema.get("required") or [])}
    for k, v in props.items():
        if isinstance(v, dict):
            item = {"type": v.get("type") or "string"}
            if v.get("description"):
                item["description"] = str(v["description"])[:80]
        else:
            item = {"type": "string"}
        slim["properties"][k] = item
    try:
        if len(json.dumps(slim)) <= SCHEMA_CHARS:
            return slim
    except Exception:
        return None
    bare = {"type": "object",
            "properties": {k: {"type": (v.get("type") or "string")
                               if isinstance(v, dict) else "string"}
                           for k, v in props.items()}}
    try:
        if len(json.dumps(bare)) <= SCHEMA_CHARS:
            return bare
    except Exception:
        pass
    return None


class McpServer:
    def __init__(self, name: str, command: list, env=None):
        self.name = name
        self.command = [str(c) for c in command]
        self.env = dict(env or {})
        self.proc = None
        self.lock = threading.RLock()
        self._buf = b""
        self._seq = 10
        self._initialized = False
        self.tools: list = []

    # ---------------------------------------------------------- plumbing
    def _start(self) -> bool:
        if self.proc is not None and self.proc.poll() is None:
            return True
        env = dict(os.environ)
        env.update({str(k): str(v) for k, v in self.env.items()})
        try:
            self.proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, env=env, bufsize=0,
                start_new_session=True)
        except Exception:
            self.proc = None
            return False
        self._buf = b""
        self._initialized = False
        return True

    def _send(self, obj) -> None:
        self.proc.stdin.write((json.dumps(obj) + "\n").encode())
        self.proc.stdin.flush()

    def _readline(self, timeout: float):
        end = time.time() + float(timeout)
        fd = self.proc.stdout.fileno()
        while time.time() < end:
            if b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                return line.decode("utf-8", "replace")
            try:
                ready, _, _ = select.select([fd], [], [], max(0.05, end - time.time()))
            except Exception:
                return None
            if not ready:
                continue
            try:
                data = os.read(fd, 65536)
            except OSError:
                return None
            if not data:
                return None  # EOF
            self._buf += data
        return None

    def _request(self, method: str, params: dict, timeout: float):
        self._seq += 1
        want = self._seq
        self._send({"jsonrpc": "2.0", "id": want, "method": method,
                    "params": params})
        end = time.time() + float(timeout)
        while time.time() < end:
            line = self._readline(max(0.1, end - time.time()))
            if line is None:
                return None
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == want:
                return msg
        return None

    # ---------------------------------------------------------- protocol
    def initialize(self, timeout: float = 20) -> bool:
        with self.lock:
            if self._initialized:
                return True
            if not self._start():
                return False
            msg = self._request("initialize", {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "hydra", "version": "1.0"},
            }, timeout)
            if not isinstance(msg, dict) or "error" in msg or "result" not in msg:
                self.stop()
                return False
            try:
                self._send({"jsonrpc": "2.0", "method": "notifications/initialized"})
            except Exception:
                self.stop()
                return False
            self._initialized = True
            return True

    def list_tools(self, timeout: float = 30) -> list:
        with self.lock:
            if not self.initialize(timeout):
                return []
            msg = self._request("tools/list", {}, timeout)
            if not isinstance(msg, dict) or "error" in msg:
                return []
            tools = (msg.get("result") or {}).get("tools") or []
            self.tools = [t for t in tools if isinstance(t, dict) and t.get("name")]
            return self.tools

    def call(self, tool: str, args: dict, timeout: float = 120) -> str:
        """Run tools/call; always returns a string (ERROR: … on failure)."""
        with self.lock:
            if not self.initialize(min(20, float(timeout))):
                return f"ERROR: MCP server {self.name!r} did not start"
            msg = self._request("tools/call",
                                {"name": tool, "arguments": args or {}}, timeout)
            if not isinstance(msg, dict):
                return f"ERROR: MCP server {self.name!r} timed out"
            if "error" in msg:
                err = msg.get("error")
                return f"ERROR: mcp {self.name}/{tool}: {json.dumps(err)[:800]}"
            result = msg.get("result") or {}
            parts = []
            for item in result.get("content") or []:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text") or ""))
                else:
                    try:
                        parts.append(json.dumps(item)[:800])
                    except Exception:
                        parts.append(str(item)[:800])
            text = "\n".join(parts).strip()
            if not text:
                try:
                    text = json.dumps(result)[:4000]
                except Exception:
                    text = "(empty result)"
            if result.get("isError"):
                return "ERROR: " + text
            return text

    def stop(self) -> None:
        try:
            if self.proc is not None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except Exception:
                    self.proc.kill()
                for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
                    try:
                        if stream is not None:
                            stream.close()
                    except Exception:
                        pass
        except Exception:
            pass
        self.proc = None
        self._initialized = False


class McpManager:
    """Owns the configured servers and registers their tools."""

    def __init__(self, rt):
        self.rt = rt
        self.cfg = rt.cfg
        self.servers: dict = {}
        self.cache_dir = C.home() / "mcp_cache"
        raw = C.get(self.cfg, "mcp.servers", {}) or {}
        if not isinstance(raw, dict):
            raw = {}
        for name, spec in raw.items():
            if not isinstance(spec, dict):
                continue
            cmd = _cmd_list(spec.get("command"))
            if not cmd:
                continue
            self.servers[str(name)] = {
                "spec": spec, "command": cmd,
                "srv": McpServer(str(name), cmd, spec.get("env") or {}),
            }

    # ---------------------------------------------------------- cache
    def _cache_file(self, name: str) -> Path:
        return self.cache_dir / f"{_safe(name)}.json"

    def _cached(self, name: str, command: list):
        try:
            data = json.loads(self._cache_file(name).read_text(encoding="utf-8"))
            if isinstance(data, dict) and data.get("command") == command \
                    and isinstance(data.get("tools"), list):
                return data["tools"]
        except Exception:
            pass
        return None

    def _save_cache(self, name: str, command: list, tools: list) -> None:
        try:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
            p = self._cache_file(name)
            tmp = p.with_suffix(".tmp")
            tmp.write_text(json.dumps({"command": command, "tools": tools},
                                      ensure_ascii=False), encoding="utf-8")
            tmp.replace(p)
        except Exception:
            pass

    # ---------------------------------------------------------- tools
    def discover(self, name: str, info: dict) -> list:
        cached = self._cached(name, info["command"])
        if cached is not None:
            return cached
        srv = info["srv"]
        tools = srv.list_tools()
        if tools:
            self._save_cache(name, info["command"], tools)
        if not bool((info["spec"] or {}).get("eager")):
            srv.stop()  # keep it lazy: it restarts on first call
        return tools

    def register_all(self, registry) -> list:
        from .tools import Tool

        loaded = []
        for name, info in self.servers.items():
            try:
                tools = self.discover(name, info)
            except Exception:
                tools = []
            for t in tools:
                tool_name = str(t.get("name") or "")
                if not tool_name:
                    continue
                schema = _summarize_schema(
                    t.get("inputSchema") or t.get("parameters") or
                    {"type": "object", "properties": {}})
                if schema is None:
                    continue  # too big even summarized: dropped (token thrift)
                desc = f"[mcp:{name}] " + str(t.get("description") or "")[:300]
                full = f"mcp__{name}__{tool_name}"

                def _fn(args, ctx, _name=name, _tool=tool_name):
                    info2 = self.servers.get(_name)
                    if info2 is None:
                        return f"ERROR: unknown MCP server {_name!r}"
                    timeout = float(C.get(self.cfg, "loop.tool_timeout", 120) or 120)
                    return info2["srv"].call(_tool, args or {}, timeout)

                registry.register(Tool(full, desc, schema, _fn, group="mcp"))
                loaded.append(full)
            srv = info.get("srv")
            if srv is not None and bool((info["spec"] or {}).get("eager")):
                srv.initialize()  # eager: keep it warm from the start
        return loaded

    def close(self) -> None:
        for info in self.servers.values():
            try:
                info["srv"].stop()
            except Exception:
                pass
