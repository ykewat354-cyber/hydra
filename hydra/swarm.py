"""Sub-agents (§9): spawn/wait/send/kill, capped reports, re-attach on resume.

Sub-agents are threads running the same loop as the lead. Model calls go
through the shared gate (deeper agents first); tool work inside them runs in
parallel. Each agent has its own message history — the parent only ever sees
the capped report, never the transcript.
"""
from __future__ import annotations

import threading
import time

from . import config as C
from . import journal as J
from .agent import Agent, load_system_prompt
from .loop import run_agent


class Swarm:
    def __init__(self, rt):
        self.rt = rt
        self.lock = threading.RLock()
        self._done: dict = {}      # aid -> threading.Event
        self._reports: dict = {}   # aid -> report dict (kept even after a reset)

    # -------------------------------------------------- limits / helpers
    def _limits(self):
        cfg = self.rt.cfg
        return (int(C.get(cfg, "agents.max_depth", 4) or 0),
                int(C.get(cfg, "agents.max_agents", 48) or 0))

    def report_cap(self) -> int:
        return int(C.get(self.rt.cfg, "agents.report_cap", 1600) or 1600)

    def count(self) -> int:
        with self.lock:
            return len(self.rt.agents)

    def _setup(self, agent: Agent) -> None:
        agent.journal = self.rt.journal
        agent.runtime = self.rt
        if self.rt.journal is not None:
            agent.data["files_marker"] = str(
                self.rt.journal.dir / "files.marker")

    def agent(self, aid: str):
        with self.lock:
            return self.rt.agents.get(aid)

    # -------------------------------------------------- spawn
    def spawn(self, parent: Agent, task: str, *, role: str = "worker",
              model: str = None, tools: str = "role", context_files=None,
              think=None, sem: threading.Semaphore = None) -> Agent:
        """Create + start a sub-agent; returns immediately (thread runs it)."""
        cfg = self.rt.cfg
        max_depth, max_agents = self._limits()
        depth = int(getattr(parent, "depth", 0) or 0) + 1
        if max_depth and depth > max_depth:
            raise ValueError(f"spawn refused: max_depth={max_depth} reached")
        max_agents_check = max_agents and self.count() >= max_agents
        if max_agents_check:
            raise ValueError(f"spawn refused: max_agents={max_agents} reached")
        aid = self.rt.next_agent_id()
        task_full = str(task or "").strip()
        files = [str(f) for f in (context_files or [])]
        if files:
            task_full += ("\n\n# Context files (read them yourself as needed)\n"
                          + "\n".join(files))
        agent = Agent(aid, str(role or "worker"), task_full, cfg, depth=depth,
                      parent_id=parent.id, workdir=parent.workdir,
                      system_prompt=load_system_prompt("subagent", role),
                      env_block=self.rt.child_env(role),
                      tool_names=(tools if tools is not None else "role"),
                      think=think, model=model)
        self._setup(agent)
        with self.lock:
            self.rt.agents[aid] = agent
            self._done[aid] = threading.Event()
        if self.rt.journal:
            try:
                self.rt.journal.event("agent_spawn", agent=aid, parent=parent.id,
                                      role=agent.role, depth=depth,
                                      task=task_full[:500],
                                      tools=str(tools), model=model or "")
            except Exception:
                pass
        self._launch(agent, sem=sem)
        return agent

    def _launch(self, agent: Agent, sem: threading.Semaphore = None):
        with self.lock:
            done = self._done.setdefault(agent.id, threading.Event())

        def work():
            if sem is not None:
                sem.acquire()
            try:
                run_agent(agent, self.rt)
            except Exception as exc:
                if not agent.report:
                    agent.status = "failed"
                    agent.set_report("failed",
                                     f"Error: {type(exc).__name__}: {exc}")
            finally:
                if sem is not None:
                    try:
                        sem.release()
                    except Exception:
                        pass
                with self.lock:
                    self._reports[agent.id] = agent.report_out()
                parent = self.rt.agents.get(getattr(agent, "parent_id", None))
                self.merge_child(parent, agent.id)
                done.set()
                if self.rt.journal:
                    try:
                        self.rt.journal.event("agent_end", agent.id,
                                              status=agent.status)
                    except Exception:
                        pass

        th = threading.Thread(target=work, name=f"hydra-{agent.id}", daemon=True)
        th.start()
        return th

    def merge_child(self, parent, child_id: str) -> None:
        """A5: union a child's files_changed into the parent; None is sticky."""
        if parent is None:
            return
        with self.lock:
            rep = self._reports.get(child_id)
            if not isinstance(rep, dict):
                return
            files = rep.get("files_changed")
            if files is None:
                parent.data["child_files_null"] = True
                return
            cur = parent.data.setdefault("child_files", [])
            for f in files:
                f = str(f)
                if f not in cur:
                    cur.append(f)

    # -------------------------------------------------- wait / reports
    def wait(self, ids, timeout=None) -> str:
        deadline = None if not timeout else time.time() + float(timeout)
        lines = []
        for aid in ids:
            with self.lock:
                ev = self._done.get(aid)
                agent = self.rt.agents.get(aid)
            if ev is None:
                lines.append(f"{aid}: unknown")
                continue
            remaining = None if deadline is None else max(0.0, deadline - time.time())
            ok = ev.wait(remaining)
            status = getattr(agent, "status", "?") if agent else "?"
            if ok:
                lines.append(f"{aid}: finished ({status})")
            else:
                lines.append(f"{aid}: still running (timeout) - {status}")
        return "\n".join(lines)

    def format_report(self, aid: str) -> str:
        with self.lock:
            rep = self._reports.get(aid)
        if rep is None:
            agent = self.agent(aid)
            rep = agent.report_out() if agent else None
        if not rep:
            return f"{aid}: no report yet"
        lines = [f"[{aid}] {rep.get('status')}: {rep.get('summary', '')}".strip()]
        for key in ("files_changed", "commands_run", "findings",
                    "open_issues", "artifacts"):
            vals = rep.get(key) or []
            if vals:
                lines.append(f"{key}: " + "; ".join(str(v) for v in vals))
        text = "\n".join(lines)
        cap = self.report_cap()
        if len(text) > cap:
            text = text[:cap] + (
                f"\n… [report truncated to {cap} chars; full transcript: "
                f"runs/{self.rt.run_id}/agents/{aid}/transcript.jsonl]")
        return text

    def status_lines(self, ids=None) -> str:
        with self.lock:
            agents = list(self.rt.agents.values())
        wanted = set(ids) if ids else None
        rows = []
        for a in agents:
            if wanted is not None and a.id not in wanted:
                continue
            task = " ".join(str(a.task or "").split())[:60]
            rows.append(f"{a.id} {a.status:<8} d{a.depth} {a.role:<10} "
                        f"msgs={len(a.messages)} in={a.tokens_in} "
                        f"out={a.tokens_out}"
                        + (f" | {task}" if task else ""))
        return "\n".join(rows) if rows else "no agents"

    # -------------------------------------------------- control
    def send(self, aid: str, message: str) -> str:
        agent = self.agent(aid)
        if not agent:
            return f"ERROR: unknown agent {aid}"
        agent.send(str(message))
        return f"sent to {aid} (delivered at its next turn boundary)"

    def kill(self, aid: str) -> str:
        agent = self.agent(aid)
        if not agent:
            return f"ERROR: unknown agent {aid}"
        agent.cancelled.set()
        # best-effort: stop this agent's background jobs (process groups)
        for jid, job in list(getattr(self.rt, "jobs", {}).items()):
            if job.get("agent") != aid:
                continue
            proc = job.get("proc")
            try:
                import os
                import signal
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except Exception:
                pass
        with self.lock:
            kids = [a for a in self.rt.agents.values() if a.parent_id == aid]
        for k in kids:
            self.kill(k.id)
        return f"kill requested for {aid}"

    # -------------------------------------------------- spawn_swarm
    def spawn_many(self, parent: Agent, tasks: list, *, role: str = "worker",
                   tools: str = "role", max_parallel: int = None,
                   join: str = "all", timeout=None) -> str:
        sem = None
        if max_parallel:
            sem = threading.Semaphore(max(1, int(max_parallel)))
        agents = []
        for t in tasks:
            if isinstance(t, dict):
                task = t.get("task") or ""
                a_role = t.get("role") or role
                a_tools = t.get("tools", tools)
                a_model = t.get("model")
            else:
                task = str(t)
                a_role, a_tools, a_model = role, tools, None
            try:
                agents.append(self.spawn(parent, task, role=a_role, model=a_model,
                                         tools=a_tools, sem=sem))
            except ValueError as exc:
                return f"ERROR: {exc} (spawned {len(agents)} so far)"
        ids = [a.id for a in agents]
        join = str(join or "all").lower()
        note = f"spawned {len(ids)}: {', '.join(ids)}"
        if join == "first":
            deadline = None if not timeout else time.time() + float(timeout)
            while True:
                for a in agents:
                    with self.lock:
                        ev = self._done.get(a.id)
                    if ev and ev.is_set():
                        return note + f"\nfirst finished: {a.id}\n" + self.format_report(a.id)
                if deadline is not None and time.time() >= deadline:
                    return note + "\nno agent finished before the timeout"
                time.sleep(0.05)
        if join == "quorum":
            quorum = max(1, (len(agents) + 1) // 2)
        else:
            quorum = len(agents)
        deadline = None if not timeout else time.time() + float(timeout)
        finished = 0
        while finished < quorum:
            finished = 0
            for a in agents:
                with self.lock:
                    ev = self._done.get(a.id)
                if ev and ev.is_set():
                    finished += 1
            if finished >= quorum:
                break
            if deadline is not None and time.time() >= deadline:
                break
            time.sleep(0.05)
        parts = [note, f"join={join} finished={finished}/{len(agents)}"]
        for a in agents:
            parts.append(self.format_report(a.id))
        return "\n".join(parts)

    # -------------------------------------------------- resume
    def rebuild(self) -> int:
        """Re-attach children that were running when the process was killed."""
        rt = self.rt
        events = J.read_events(rt.run_id)
        spawned, reported = {}, set()
        for e in events:
            t = e.get("type")
            if t == "agent_spawn" and e.get("agent") not in (None, "a0"):
                spawned[e["agent"]] = e
            elif t == "report" and e.get("agent"):
                reported.add(e["agent"])
            elif t == "agent_end" and e.get("agent"):
                reported.add(e["agent"])
        n = 0
        for aid, meta in spawned.items():
            if aid in reported or self.agent(aid):
                continue
            msgs = [r for r in rt.journal.read_transcript(aid)
                    if isinstance(r, dict) and r.get("role")]
            parent = self.agent(meta.get("parent") or "a0") or self.agent("a0")
            role = meta.get("role") or "worker"
            agent = Agent(aid, role, meta.get("task", ""), rt.cfg,
                          depth=int(meta.get("depth") or 1),
                          parent_id=meta.get("parent") or "a0",
                          workdir=parent.workdir if parent else rt.cwd,
                          system_prompt=load_system_prompt("subagent", role),
                          env_block=rt.child_env(role),
                          tool_names=meta.get("tools") or "role",
                          model=meta.get("model") or None)
            agent.messages = msgs
            self._setup(agent)
            if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
                for tc in msgs[-1]["tool_calls"]:
                    name = (tc.get("function") or {}).get("name") or "?"
                    agent.append({"role": "tool", "tool_name": name,
                                  "content": "INTERRUPTED: harness was killed while this "
                                             "ran; effects unknown. Inspect state before "
                                             "repeating."})
            with self.lock:
                rt.agents[aid] = agent
                self._done[aid] = threading.Event()
            if self.rt.journal:
                try:
                    self.rt.journal.event("agent_reattach", agent=aid,
                                          role=role, depth=agent.depth)
                except Exception:
                    pass
            self._launch(agent)
            n += 1
        return n
