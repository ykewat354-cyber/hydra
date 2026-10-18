"""Runtime: wires config, client, router, journal, tools, agents and the TUI."""
from __future__ import annotations

import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config as C
from . import hygiene
from . import journal as J
from .agent import Agent, build_env_block, load_system_prompt
from .client import AuthError, OllamaCloud
from .gate import LLMGate
from .loop import run_agent
from .models import ModelRegistry
from .router import AllModelsFailed, Router
from .swarm import Swarm
from .tools import Executor, Registry


class Runtime:
    def __init__(self, cfg: dict, *, cwd=None, quiet=False, json_out=False):
        self.cfg = cfg
        self.cwd = Path(cwd or os.getcwd())
        self.quiet = quiet
        self.json_out = json_out
        self.prices = C.load_prices(cfg)
        self.redact = hygiene.make_redactor(cfg)
        key = C.load_key(cfg) or ""
        self.client = OllamaCloud(
            C.get(cfg, "api.host", "https://ollama.com"), key,
            C.get(cfg, "api.connect_timeout", 15) or 15,
            C.get(cfg, "api.idle_timeout", 90) or 90)
        self.sigint = threading.Event()   # first Ctrl-C: cancel, keep the run alive
        self.client.cancel_event = self.sigint
        self._child_env: dict = {}
        self.gate = LLMGate(C.max_inflight(cfg))
        self.registry = ModelRegistry(cfg)
        self.router = Router(cfg, self.client, self.prices, gate=self.gate,
                             registry=self.registry)

        from .tui import Tui
        self.ui = Tui(quiet=quiet, json_out=json_out)
        self.journal = None
        self.run_id = None
        self.run_meta: dict = {}
        self.env_block = None
        self.tools = Registry(self)
        self.executor = Executor(self)
        self.agents: dict = {}
        self.jobs: dict = {}
        self.swarm = Swarm(self)
        self._agent_seq = 0
        self._job_seq = 0
        self._lock = threading.RLock()
        self.paused = threading.Event()  # REPL /pause — checked between turns

    # -------------------------------------------------- ids
    def next_agent_id(self) -> str:
        with self._lock:
            aid = f"a{self._agent_seq}"
            self._agent_seq += 1
            return aid

    def _sync_agent_seq(self) -> None:
        """Never reuse an id: children continue after the highest existing a<N>."""
        mx = 0
        jdir = getattr(self.journal, "dir", None)
        if jdir is not None:
            for p in (jdir / "agents").glob("a*"):
                try:
                    mx = max(mx, int(p.name[1:]))
                except ValueError:
                    pass
        with self._lock:
            self._agent_seq = max(self._agent_seq, mx + 1)

    def next_job_id(self) -> str:
        with self._lock:
            self._job_seq += 1
            return f"j{self._job_seq}"

    # -------------------------------------------------- termux niceties
    def wake_lock(self) -> None:
        if C.get(self.cfg, "termux.wake_lock", True) and shutil.which("termux-wake-lock"):
            try:
                subprocess.run(["termux-wake-lock"], timeout=10)
            except Exception:
                pass

    def wake_unlock(self) -> None:
        if shutil.which("termux-wake-unlock"):
            try:
                subprocess.run(["termux-wake-unlock"], timeout=10)
            except Exception:
                pass

    def notify(self, title: str, content: str) -> None:
        if shutil.which("termux-notification"):
            try:
                subprocess.run(["termux-notification", "-t", str(title)[:60],
                                "-c", str(content)[:200]], timeout=10)
            except Exception:
                pass

    # -------------------------------------------------- run lifecycle
    def _setup_agent(self, agent: Agent) -> Agent:
        agent.journal = self.journal
        agent.runtime = self
        return agent

    def start_run(self, task: str, name: str = None, lead_model: str = None) -> Agent:
        self.run_id = J.new_run_id()
        self.journal = J.Journal(self.run_id, self.cfg, sink=self.ui.event,
                                 redact=self.redact)
        self.router.journal = self.journal
        self.env_block = build_env_block(self.cwd, self.cfg, role="lead")
        if lead_model:
            chain = list(C.get(self.cfg, "roles.lead.chain") or [])
            self.cfg.setdefault("roles", {}).setdefault("lead", {})["chain"] = \
                [lead_model] + [m for m in chain if m != lead_model]
        lead = Agent("a0", "lead", task, self.cfg, workdir=self.cwd,
                     system_prompt=load_system_prompt("lead"),
                     env_block=self.env_block)
        self._setup_agent(lead)
        self.agents = {lead.id: lead}
        self.swarm = Swarm(self)
        self._sync_agent_seq()
        self.run_meta = {"run": self.run_id, "name": name or "", "task": task,
                         "cwd": str(self.cwd), "status": "running",
                         "started": time.time(), "pid": os.getpid(),
                         "finished": None, "exit_code": None}
        self.journal.event("run_start", None, task=task, cwd=str(self.cwd),
                           name=name or "")
        self.journal.checkpoint(self.run_meta)
        return lead

    def resume(self, which: str = None) -> Agent:
        rid = J.find_run(which) if which else J.last_run_in(str(self.cwd))
        if not rid:
            rid = (J.run_ids() or [None])[0]
        if not rid:
            raise SystemExit("hydra: no runs to resume")
        data = J.load_run(rid) or {}
        events = J.read_events(rid)
        self.run_id = rid
        self.journal = J.Journal(rid, self.cfg, sink=self.ui.event,
                                 redact=self.redact, seq0=len(events))
        self.router.journal = self.journal
        self.cwd = Path(data.get("cwd") or self.cwd)
        self.env_block = build_env_block(self.cwd, self.cfg, role="lead")
        lead = Agent("a0", "lead", data.get("task", ""), self.cfg, workdir=self.cwd,
                     system_prompt=load_system_prompt("lead"),
                     env_block=self.env_block)
        self._setup_agent(lead)
        msgs = [r for r in self.journal.read_transcript("a0")
                if isinstance(r, dict) and r.get("role")]
        lead.messages = msgs
        # synthesize INTERRUPTED results for a dangling tool-call turn
        if msgs and msgs[-1].get("role") == "assistant" and msgs[-1].get("tool_calls"):
            for tc in msgs[-1]["tool_calls"]:
                name = (tc.get("function") or {}).get("name") or tc.get("name") or "?"
                lead.append({"role": "tool", "tool_name": name,
                             "content": "INTERRUPTED: harness was killed while this ran; "
                                        "effects unknown. Inspect state before repeating."})
        self.agents = {lead.id: lead}
        self.swarm = Swarm(self)
        self._sync_agent_seq()
        self.run_meta = dict(data)
        self.run_meta["status"] = "running"
        self.journal.event("run_resume", None)
        # re-attach children that were alive when the process was killed
        try:
            n = self.swarm.rebuild()
            if n:
                self.journal.event("swarm_reattach", None, n=n)
        except Exception:
            pass
        return lead

    # -------------------------------------------------- execution
    def _execute(self, lead: Agent):
        try:
            report = run_agent(lead, self)
            ok = report.get("status") in ("done", "partial")
            self.run_meta.update({"status": report.get("status", "done"),
                                  "exit_code": 0 if ok else 1})
            return (0 if ok else 1), report
        except AuthError as exc:
            self.run_meta.update({"status": "auth-error", "exit_code": 1})
            self.ui.info("\n401/403 from the API: the Ollama key is missing or wrong.")
            self.ui.info("Fix: echo 'export OLLAMA_API_KEY=your_key' >> ~/.bashrc && source ~/.bashrc")
            self.ui.info("  or: mkdir -p ~/.hydra && echo your_key > ~/.hydra/key && chmod 600 ~/.hydra/key")
            self.ui.info(f"({exc})")
            self._hook("error", lead, f"{type(exc).__name__}: {exc}")
            return 1, (lead.report or {})
        except AllModelsFailed as exc:
            parked = bool(getattr(exc, "all_paid", False))
            self.run_meta.update({"status": "parked" if parked else "failed",
                                  "exit_code": 75 if parked else 1})
            if parked:
                self.ui.info("\nParked: free credits look exhausted or all models are paid.")
                self.ui.info("Re-run after your monthly reset:  hydra resume")
                if C.get(self.cfg, "termux.notify_on_park", True):
                    self.notify("HYDRA parked", "Credits likely exhausted. hydra resume")
                self._hook("park", lead, str(exc))
            else:
                self.ui.info(f"\nAll models failed: {exc}")
                self._hook("error", lead, f"{type(exc).__name__}: {exc}")
            return (75 if parked else 1), (lead.report or {})
        except KeyboardInterrupt:
            self.run_meta.update({"status": "interrupted", "exit_code": 130})
            return 130, (lead.report or {})
        except Exception as exc:
            self.run_meta.update({"status": "failed", "exit_code": 1})
            self.ui.info(f"\nrun failed: {type(exc).__name__}: {exc}")
            self._hook("error", lead, f"{type(exc).__name__}: {exc}")
            return 1, (lead.report or {})
        finally:
            self.run_meta["finished"] = time.time()
            if self.journal:
                self.journal.checkpoint(self.run_meta)
            if (self.run_meta.get("status") in ("done", "partial")
                    and C.get(self.cfg, "termux.notify_on_finish", True)):
                self.notify("HYDRA done", str((lead.report or {}).get("summary", ""))[:160])

    def _hook(self, event: str, agent, preview: str = "") -> None:
        try:
            from .hooks import run_hook
            run_hook(self, event, agent, result_preview=preview)
        except Exception:
            pass

    def child_env(self, role: str) -> str:
        """Env block for a sub-agent: same text, project part capped (§19.1)."""
        role = str(role or "worker")
        if role not in self._child_env:
            full = str(C.get(self.cfg, f"roles.{role}.project", "")) == "full"
            cap = None if full else int(
                C.get(self.cfg, "project.subagent_chars", 2000) or 2000)
            self._child_env[role] = build_env_block(self.cwd, self.cfg, role=role,
                                                    project_cap=cap)
        return self._child_env[role]

    def run(self, task: str, name: str = None, lead_model: str = None):
        lead = self.start_run(task, name=name, lead_model=lead_model)
        return self._execute(lead)

    def resume_run(self, which: str = None):
        lead = self.resume(which)
        return self._execute(lead)

    # -------------------------------------------------- usage / close
    def usage_totals(self, run_id: str = None) -> dict:
        recs = J.read_usage(run_id or self.run_id)
        tin = sum(int(r.get("in", 0)) for r in recs)
        tout = sum(int(r.get("out", 0)) for r in recs)
        est = sum(float(r.get("est_usd", 0.0)) for r in recs)
        by_model: dict = {}
        for r in recs:
            m = r.get("model", "?")
            d = by_model.setdefault(m, {"in": 0, "out": 0, "est_usd": 0.0, "calls": 0})
            d["in"] += int(r.get("in", 0))
            d["out"] += int(r.get("out", 0))
            d["est_usd"] += float(r.get("est_usd", 0.0))
            d["calls"] += 1
        return {"calls": len(recs), "in": tin, "out": tout, "est_usd": est,
                "by_model": by_model}

    def close(self) -> None:
        if self.journal:
            self.journal.close()
        for pool in (self.executor.pool, self.executor.agent_pool):
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass
