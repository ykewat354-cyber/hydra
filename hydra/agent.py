"""Agent state, context building, tolerant tool-call parsing, prompt loading."""
from __future__ import annotations

import collections
import json
import os
import platform
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config as C

PROMPTS_DIR = Path(__file__).parent / "prompts"


# ---------------------------------------------------------------- prompts

def load_prompt(name: str) -> str:
    """Load a built-in prompt file (e.g. 'lead', 'subagent', 'roles/worker')."""
    p = PROMPTS_DIR / f"{name}.md"
    if p.is_file():
        return p.read_text(encoding="utf-8")
    return ""


def load_role_prompt(role: str) -> str:
    override = C.home() / "roles" / f"{role}.md"
    if override.is_file():
        return override.read_text(encoding="utf-8")
    return load_prompt(f"roles/{role}")


def load_system_prompt(kind: str, role: str = None) -> str:
    base = load_prompt(kind)
    if kind == "subagent" and role:
        extra = load_role_prompt(role)
        if extra:
            base = base.replace("{role}", role) + "\n\n" + extra
    return base.replace("{role}", role or "")


# ---------------------------------------------------------------- env block

def build_env_block(cwd) -> str:
    lines = ["# Environment (fixed for this run)"]
    lines.append(f"date: {time.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"machine: {platform.machine()} / {platform.system()}")
    lines.append(f"python: {platform.python_version()}")
    lines.append(f"cwd: {cwd}")
    lines.append(f"PREFIX: {os.environ.get('PREFIX', '')}")
    clis = [c for c in ("rg", "git", "jq", "node", "npm", "curl", "bash", "patch",
                        "tmux", "fd", "python") if shutil.which(c)]
    lines.append("clis: " + ", ".join(clis))
    try:
        du = shutil.disk_usage(str(cwd))
        lines.append(f"disk_free_mb: {du.free // (1 << 20)}")
    except Exception:
        pass
    try:
        out = subprocess.run(["termux-battery-status"], capture_output=True,
                             text=True, timeout=3)
        if out.returncode == 0:
            bat = json.loads(out.stdout or "{}")
            if "percentage" in bat:
                lines.append(f"battery: {bat['percentage']}%")
    except Exception:
        pass
    return "\n".join(lines)


# ---------------------------------------------------------------- call parsing

def normalize_call(tc) -> dict | None:
    """Accept {'function':{name,arguments}} | {name,arguments} | JSON-string args."""
    if not isinstance(tc, dict):
        return None
    fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
    name = fn.get("name") or tc.get("name")
    if not name:
        return None
    args = fn.get("arguments", tc.get("arguments"))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except Exception:
            args = {"_raw": args}
    if args is None:
        args = {}
    if not isinstance(args, dict):
        args = {"value": args}
    return {"function": {"name": str(name), "arguments": args}}


def _balanced_objects(text: str, limit: int = 20):
    """Yield every balanced {...} substring (ignores braces inside strings)."""
    depth = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    yield text[start:i + 1]
                    start = None
                    limit -= 1
                    if limit <= 0:
                        return


def parse_text_calls(text: str) -> list:
    """Recover tool calls from plain text: JSON, <tool_call>, fenced json, ReAct."""
    if not text:
        return []
    found = []

    def _try(obj_text):
        try:
            obj = json.loads(obj_text)
        except Exception:
            return None
        if isinstance(obj, dict):
            return normalize_call(obj)
        return None

    for m in re.finditer(r"<tool_call>\s*(.*?)\s*</tool_call>", text, re.S):
        c = _try(m.group(1))
        if c:
            found.append(c)

    for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S):
        blob = m.group(1).strip()
        c = _try(blob)
        if c:
            found.append(c)
        else:
            for obj in _balanced_objects(blob):
                c = _try(obj)
                if c:
                    found.append(c)

    for obj in _balanced_objects(text):
        c = _try(obj)
        if c:
            found.append(c)

    for m in re.finditer(
            r"Action\s*:\s*([A-Za-z0-9_\-]+)\s*\n\s*Action\s*Input\s*:\s*(\{.*?\})\s*(?:\n|$)",
            text, re.S):
        try:
            args = json.loads(m.group(2))
        except Exception:
            args = {"_raw": m.group(2)}
        found.append({"function": {"name": m.group(1), "arguments": args}})

    out, seen = [], set()
    for c in found:
        key = (c["function"]["name"], json.dumps(c["function"]["arguments"], sort_keys=True))
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ---------------------------------------------------------------- agent

class Agent:
    def __init__(self, aid: str, role: str, task: str, cfg: dict, *,
                 depth: int = 0, parent_id: str = None, workdir=None,
                 think=None, model=None, tool_names: str = "all",
                 system_prompt: str = None, env_block: str = ""):
        self.id = aid
        self.role = role
        self.task = str(task or "")
        self.cfg = cfg
        self.depth = int(depth)
        self.parent_id = parent_id
        self.workdir = Path(workdir or os.getcwd())
        self.think = think
        self.model_override = model
        self.tool_names = tool_names
        self.system_prompt = system_prompt if system_prompt is not None else load_system_prompt("lead")
        self.env_block = env_block
        self.messages: list = []
        self.inbox = collections.deque()
        self._inbox_lock = threading.Lock()
        self.status = "queued"
        self.report = None
        self.finished = False
        self.cancelled = threading.Event()
        self.tokens_in = 0
        self.tokens_out = 0
        self.turns = 0
        self.nudges = 0
        self.exclude_models: list = []
        self.data: dict = {}
        self.journal = None
        self.runtime = None
        self._stall_window: list = []

    # -------------------------------------------------- messaging
    def append(self, message: dict) -> None:
        self.messages.append(message)
        if self.journal is not None:
            self.journal.transcript(self.id, message)

    def append_tool_results(self, results: list) -> None:
        for r in results:
            self.append(r)

    def send(self, text: str) -> None:
        with self._inbox_lock:
            self.inbox.append(str(text))

    def absorb_inbox(self) -> int:
        n = 0
        while True:
            with self._inbox_lock:
                if not self.inbox:
                    break
                text = self.inbox.popleft()
            self.append({"role": "user", "content": text})
            if self.journal is not None:
                self.journal.event("user_msg", self.id, text=str(text)[:500])
            n += 1
        return n

    def nudge(self, text: str) -> None:
        self.nudges += 1
        self.append({"role": "user", "content": text})

    # -------------------------------------------------- context
    def api_messages(self) -> list:
        first = self.env_block or ""
        if self.task:
            first = (first + "\n\n# Task\n" + self.task).strip()
        return [{"role": "system", "content": self.system_prompt},
                {"role": "user", "content": first}] + self.messages

    def estimate_tokens(self) -> int:
        try:
            blob = json.dumps(self.api_messages(), ensure_ascii=False)
        except Exception:
            blob = str(self.messages)
        return int(len(blob) / 3.5)

    def maybe_compact(self, rt) -> bool:
        return False  # real compaction lands with the context phase

    def compact(self, rt) -> bool:
        return False

    # -------------------------------------------------- report
    def finish_from_text(self, text: str) -> None:
        self.set_report("done", str(text or "").strip()[:2000])

    def set_report(self, status: str, summary: str, *, files_changed=None,
                   commands_run=None, findings=None, open_issues=None, artifacts=None) -> None:
        self.report = {
            "status": status if status in ("done", "partial", "failed", "killed") else "done",
            "summary": str(summary or ""),
            "files_changed": [str(x) for x in (files_changed or [])],
            "commands_run": [str(x) for x in (commands_run or [])],
            "findings": [str(x) for x in (findings or [])],
            "open_issues": [str(x) for x in (open_issues or [])],
            "artifacts": [str(x) for x in (artifacts or [])],
        }
        self.finished = True

    def report_out(self) -> dict:
        return self.report or {"status": "failed", "summary": "agent ended without finish",
                               "files_changed": [], "commands_run": [], "findings": [],
                               "open_issues": [], "artifacts": []}

    # -------------------------------------------------- stall detection
    def stall_check(self, rt, calls: list, results: list) -> None:
        repeat = int(C.get(rt.cfg, "loop.stall_repeat", 3) or 3)
        sigs = [c["function"]["name"] + ":" +
                json.dumps(c["function"]["arguments"], sort_keys=True) for c in calls]
        self._stall_window.append(sigs)
        self._stall_window = self._stall_window[-6:]
        counts: dict = {}
        for batch in self._stall_window:
            for s in batch:
                counts[s] = counts.get(s, 0) + 1
        worst = max(counts.values()) if counts else 0
        if worst >= repeat:
            self._stall_window.clear()
            if self.journal is not None:
                self.journal.event("stall", self.id, repeated=worst)
            self.send("Change approach: the same action keeps repeating without new "
                      "progress. Re-read the last output, use a different method, or "
                      "call finish with what you have.")
