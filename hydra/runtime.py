"""Runtime: wires config, client, router, journal, tools, agents and the TUI."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config as C
from . import caps as Caps
from . import httpdebug
from . import hygiene
from . import journal as J
from . import limits as Limits
from .agent import Agent, build_env_block, load_system_prompt
from .client import AuthError, OllamaCloud
from .gate import LLMGate
from .loop import run_agent
from .models import ModelRegistry
from .router import AllModelsFailed, Router
from .swarm import Swarm
from .tools import Executor, Registry


class _DetachedJob:
    """Popen look-alike for a job re-attached after a crash/resume.

    The process is no longer our child, so .poll() uses pid liveness and we
    cannot recover its exit code (0 is reported once it is gone).
    """

    def __init__(self, pid: int, log: Path, cmd: str, started: float,
                 agent=None):
        self.pid = int(pid)
        self.log = Path(log)
        self.cmd = cmd
        self.started = started
        self.agent = agent
        self.returncode = None

    def poll(self):
        if self.returncode is not None:
            return self.returncode
        state = None
        try:
            with open(f"/proc/{self.pid}/stat", "rb") as fh:
                state = fh.read().rsplit(b") ", 1)[1].split(b" ", 1)[0]
        except Exception:
            state = None
        if state in (b"Z", b"X", b"x"):
            self.returncode = 0
            return self.returncode
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            self.returncode = 0
        except PermissionError:
            return None
        except OSError:
            self.returncode = 0
        return self.returncode

    def kill(self):
        try:
            os.kill(self.pid, 9)
        except OSError:
            pass


class Runtime:
    def __init__(self, cfg: dict, *, cwd=None, quiet=False, json_out=False,
                 verbose=False, ui=None):
        self.cfg = cfg
        self.cwd = Path(cwd or os.getcwd())
        self.quiet = quiet
        self.json_out = json_out
        self.prices = C.load_prices(cfg)
        try:
            self.budget_usd = max(0.0, float(C.get(cfg, "budget.usd", 0) or 0))
        except (TypeError, ValueError):
            self.budget_usd = 0.0
        self.budget_hit = False
        self.redact = hygiene.make_redactor(cfg)
        self.debug_http = httpdebug.enabled(cfg)   # F4 --debug-http / env var
        key = C.load_key(cfg) or ""
        self.client = OllamaCloud(
            C.get(cfg, "api.host", "https://ollama.com"), key,
            C.get(cfg, "api.connect_timeout", 15) or 15,
            C.get(cfg, "api.idle_timeout", 90) or 90)
        self.sigint = threading.Event()   # first Ctrl-C: cancel, keep the run alive
        self.client.cancel_event = self.sigint
        self.client.redact = self.redact
        self.client.reject_status = Limits.rejection_status()  # F5
        self._child_env: dict = {}
        self.gate = LLMGate(Limits.gate_capacity(cfg),
                            semantics=Limits.semantics())      # F5
        self.caps = Caps.get_caps(cfg, client=self.client)     # F6 (24 h cache)
        self.registry = ModelRegistry(cfg)
        self.router = Router(cfg, self.client, self.prices, gate=self.gate,
                             registry=self.registry)

        if ui is None:
            from .tui import Tui
            ui = Tui(quiet=quiet, json_out=json_out, verbose=verbose)
        self.ui = ui
        self.web_cache: dict = {}          # F14: per-run web tool memo
        self.web_cache_lock = threading.RLock()
        self.journal = None
        self._pending_niceties: list = []
        self.run_id = None
        self.run_meta: dict = {}
        self.env_block = None
        self.tools = Registry(self)
        self.executor = Executor(self)
        self.agents: dict = {}
        self.jobs: dict = {}
        self.server_nudged: set = set()
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

    def _reattach_jobs(self) -> None:
        """Recover background jobs journaled before a crash.

        The child was started in its own session, so it usually survives the
        harness being killed. Without this the resumed agent is told "no
        background jobs" and repeats a command whose effects are still coming.
        """
        mx = 0
        for ev in J.read_events(self.run_id):
            if ev.get("type") != "job_start":
                continue
            jid = str(ev.get("jid") or "")
            try:
                pid = int(ev.get("pid"))
            except (TypeError, ValueError):
                continue
            if not jid:
                continue
            self.jobs[jid] = {
                "proc": _DetachedJob(pid, Path(ev.get("log") or ""),
                                     str(ev.get("cmd") or ""),
                                     float(ev.get("started") or 0),
                                     ev.get("agent")),
                "log": Path(ev.get("log") or ""),
                "cmd": str(ev.get("cmd") or ""),
                "started": float(ev.get("started") or 0),
                "agent": ev.get("agent"),
            }
            try:
                mx = max(mx, int(jid.lstrip("j")))
            except ValueError:
                pass
        with self._lock:
            self._job_seq = max(self._job_seq, mx)

    # -------------------------------------------------- termux niceties
    def _record_nicety(self, kind: str, ok: bool, returncode: int,
                       title: str = None) -> None:
        """Append one evidence line to ~/.hydra/niceties.jsonl and journal it.

        Called before a run journal exists (wake_lock), so events are buffered
        and flushed into the journal as soon as the run starts.
        """
        rec = {"ts": round(time.time(), 3), "kind": kind,
               "ok": bool(ok), "returncode": int(returncode)}
        if title is not None:
            rec["title"] = str(title)[:60]
        try:
            p = C.home() / "niceties.jsonl"
            p.parent.mkdir(parents=True, exist_ok=True)
            with open(p, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        except Exception:
            pass
        fields = {"ok": rec["ok"], "returncode": rec["returncode"]}
        if "title" in rec:
            fields["title"] = rec["title"]
        if self.journal:
            try:
                self.journal.event(kind, None, **fields)
            except Exception:
                pass
        else:
            self._pending_niceties.append((kind, fields))

    def _flush_pending_niceties(self) -> None:
        pending, self._pending_niceties = self._pending_niceties, []
        if not self.journal:
            return
        for kind, fields in pending:
            try:
                self.journal.event(kind, None, **fields)
            except Exception:
                pass

    def wake_lock(self) -> None:
        if not (C.get(self.cfg, "termux.wake_lock", True)
                and shutil.which("termux-wake-lock")):
            return
        rc = -1
        try:
            rc = int(subprocess.run(["termux-wake-lock"], timeout=10).returncode)
        except Exception:
            pass
        self._record_nicety("wake_lock", rc == 0, rc)

    def wake_unlock(self) -> None:
        if shutil.which("termux-wake-unlock"):
            try:
                subprocess.run(["termux-wake-unlock"], timeout=10)
            except Exception:
                pass

    def notify(self, title: str, content: str) -> None:
        title = str(title)[:60]
        if not shutil.which("termux-notification"):
            return
        rc = -1
        try:
            rc = int(subprocess.run(
                ["termux-notification", "-t", title,
                 "-c", str(content)[:200]], timeout=10).returncode)
        except Exception:
            pass
        self._record_nicety("notify", rc == 0, rc, title=title)

    # -------------------------------------------------- run lifecycle
    def _attach_http_log(self) -> None:
        """F4: start appending runs/<id>/http.jsonl for this run (if enabled)."""
        if not self.debug_http or not self.run_id:
            return
        try:
            self.client.http_log = httpdebug.HttpLogger(
                C.run_dir(self.run_id) / "http.jsonl", redact=self.redact)
        except Exception:
            self.client.http_log = None

    def _setup_agent(self, agent: Agent) -> Agent:
        agent.journal = self.journal
        agent.runtime = self
        return agent

    def _files_marker(self, since: float) -> Path:
        """A run-start marker for `find -newer`; never reset on resume."""
        marker = Path(self.journal.dir) / "files.marker"
        if not marker.exists():
            try:
                marker.write_text("", encoding="utf-8")
                os.utime(marker, (float(since) - 1.0, float(since) - 1.0))
            except Exception:
                pass
        return marker

    def start_run(self, task: str, name: str = None, lead_model: str = None) -> Agent:
        with self.web_cache_lock:
            self.web_cache.clear()
        self.run_id = J.new_run_id()
        self.journal = J.Journal(self.run_id, self.cfg, sink=self.ui.event,
                                 redact=self.redact)
        self._flush_pending_niceties()
        self._attach_http_log()
        self.router.journal = self.journal
        # C2: refresh stale registry entries once per process/hour, never per
        # turn.  A fresh/empty registry is left to explicit `models probe`.
        if not getattr(self, "_reprobe_checked", False):
            self._reprobe_checked = True
            try:
                from . import models as M
                self.reprobed = M.maybe_reprobe(
                    self.cfg, self.client, self.gate, self.registry,
                    journal=self.journal)
            except Exception:
                self.reprobed = []
        self.env_block = build_env_block(self.cwd, self.cfg, role="lead",
                                         caps=self.caps)
        marker = self._files_marker(time.time())
        if lead_model:
            chain = list(C.get(self.cfg, "roles.lead.chain") or [])
            self.cfg.setdefault("roles", {}).setdefault("lead", {})["chain"] = \
                [lead_model] + [m for m in chain if m != lead_model]
        lead = Agent("a0", "lead", task, self.cfg, workdir=self.cwd,
                     system_prompt=load_system_prompt("lead"),
                     env_block=self.env_block)
        lead.data["files_marker"] = str(marker)
        if lead_model:
            # Make --lead a real hint: router fallback ranking sorts unknown
            # models last, so without this a bad lead is never attempted (and
            # never marked missing). Paid leads are still pruned + failed over.
            lead.model_override = lead_model
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
        with self.web_cache_lock:
            self.web_cache.clear()
        self.run_id = rid
        self.journal = J.Journal(rid, self.cfg, sink=self.ui.event,
                                 redact=self.redact, seq0=len(events))
        self._flush_pending_niceties()
        self._attach_http_log()
        self.router.journal = self.journal
        self.cwd = Path(data.get("cwd") or self.cwd)
        self.env_block = build_env_block(self.cwd, self.cfg, role="lead",
                                         caps=self.caps)
        lead = Agent("a0", "lead", data.get("task", ""), self.cfg, workdir=self.cwd,
                     system_prompt=load_system_prompt("lead"),
                     env_block=self.env_block)
        lead.data["files_marker"] = str(
            self._files_marker(float(data.get("started") or time.time())))
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
        self._reattach_jobs()
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
        except Limits.BudgetExceeded as exc:
            self.budget_hit = True
            self.run_meta.update({"status": "parked", "exit_code": 75})
            self.ui.info(f"\nParked: {exc}")
            self.ui.info("Continue with a higher cap:  hydra resume --budget-usd M")
            if C.get(self.cfg, "termux.notify_on_park", True):
                self.notify("HYDRA parked", "Budget limit reached. hydra resume")
            self._hook("park", lead, str(exc))
            return 75, (lead.report or {})
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
                                                    project_cap=cap,
                                                    caps=self.caps)
        return self._child_env[role]

    def run(self, task: str, name: str = None, lead_model: str = None):
        lead = self.start_run(task, name=name, lead_model=lead_model)
        return self._execute(lead)

    def resume_run(self, which: str = None):
        lead = self.resume(which)
        return self._execute(lead)

    # -------------------------------------------------- usage / close
    def budget_exceeded(self) -> bool:
        """True once this run's ledger reaches the opt-in cap (fail open)."""
        if self.budget_usd <= 0:
            return False
        try:
            spent = sum(float(r.get("est_usd", 0.0))
                        for r in J.read_usage(self.run_id))
        except Exception:
            return False
        return spent >= self.budget_usd

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
        try:
            if getattr(self.tools, "mcp", None) is not None:
                self.tools.mcp.close()
        except Exception:
            pass
        for pool in (self.executor.pool, self.executor.agent_pool):
            try:
                pool.shutdown(wait=False)
            except Exception:
                pass
        try:
            self.client.close()          # F7: close pooled keep-alive sockets
        except Exception:
            pass
