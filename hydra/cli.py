"""HYDRA CLI: run tasks, resume, inspect runs, doctor.

Command surface (grows by phase):
  hydra "task"            autonomous run
  hydra run task.md       (same; tasks may be free text)
  hydra resume [RUN]      continue a killed/parked run
  hydra ls | tail | tree | usage | kill | doctor
  hydra models ...        model discovery (models phase)
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import __version__
from . import config as C
from . import journal as J
from .client import CallCancelled

COMMANDS = {"run", "resume", "ls", "tail", "tree", "usage", "doctor", "kill",
            "models", "undo", "init", "map", "eval", "daemon", "schedule",
            "report", "help", "version"}

HELP = """HYDRA — autonomous multi-agent harness for Termux (Ollama Cloud).

usage:  hydra                       open the full-screen TUI (works from any folder)
        hydra "task"                open the TUI with a task already running
        hydra -i                    same TUI (interactive)
        hydra --plain "task"        compact stream in this terminal (scripts)
        hydra --detach "task"       tmux/new-session, survives closing Termux
        hydra <command> [args]

The TUI: assistant text streams live, tool calls collapse to one line
(Ctrl-T expands), the composer stays open for steering, and /help lists
commands. Ctrl-C cancels the in-flight call; press twice to stop the run.

commands:
  resume [RUN|NAME]     continue a killed / parked / interrupted run
  ls                    list runs
  tail [RUN] [-a AGENT]  recent events of a run
  tree [RUN]            agent tree with status and tokens
  report [RUN|--last]   paste-friendly run summary (--json --full --since 2h --timeline)
  usage [RUN]           token ledger + cost estimate (all runs if omitted)
  models probe|ls|pin   discover models your key can use
  models bench          benchmark models on B1/B2/B3 -> ~/.hydra/scorecard.json
  models tune [--apply] propose role chains from the scorecard
  daemon [--once]       run *.md task files dropped in ~/.hydra/queue/
  schedule add|ls|rm    periodic jobs fired by the daemon
  map [PATH] [BUDGET]   repo map: tree + top-level symbols (cached)
  undo [RUN] [--to N]   restore the workdir from a shadow snapshot
  eval [SUITE]          run scripted eval suites on the fake server
  init                  the lead explores the repo and writes HYDRA.md
  doctor                environment checks (--limits: calibrate concurrency,
                        --caps: force a capability re-check)
  kill RUN              stop a run

flags:
  --set key=value       config override (repeatable)
  --lead MODEL          pin the lead model
  --name NAME           name this run
  --max-inflight N      LLM gate slots
  --continue            resume the last run started in this cwd
  --plain / --no-tui    no full-screen UI; compact stream (auto when piped)
  --debug-http          append runs/<id>/http.jsonl (or HYDRA_DEBUG_HTTP=1)
  -v / --verbose        show full tool arguments/results in plain mode
  --quiet  --json  --wait-reset
"""

REPL_HELP = """repl commands (all non-blocking, none is an approval):
  /tree            agent tree + status
  /kill aN         stop an agent (subtree included)
  /model [role=]m  pin a model first for a role (default lead)
  /models          role chains and registry status
  /cost            tokens + cost estimate so far
  /compact         force context compaction now
  /plan            tell the lead to plan (todos) before acting
  /pause /resume   pause between turns
  /note text       write to the shared blackboard
  /quit            leave the REPL (run keeps its state)
  /<name> [args]   run a custom command from ~/.hydra/commands/<name>.md
  anything else    goes to the lead's inbox at its next turn
"""


class Opts:
    def __init__(self):
        self.quiet = False
        self.json_out = False
        self.sets = []
        self.lead = None
        self.name = None
        self.max_inflight = None
        self.wait_reset = False
        self.detach = False
        self.continue_ = False
        self.interactive = False
        self.plain = False
        self.verbose = False
        self.debug_http = False


def _parse(argv):
    o = Opts()
    rest = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--set":
            i += 1
            if i >= len(argv):
                raise SystemExit("hydra: --set needs key=value")
            o.sets.append(argv[i])
        elif a.startswith("--set="):
            o.sets.append(a[6:])
        elif a in ("--quiet", "-q"):
            o.quiet = True
        elif a == "--json":
            o.json_out = True
        elif a == "--lead":
            i += 1
            o.lead = argv[i]
        elif a == "--name":
            i += 1
            o.name = argv[i]
        elif a == "--max-inflight":
            i += 1
            o.max_inflight = argv[i]
        elif a == "--wait-reset":
            o.wait_reset = True
        elif a == "--debug-http":
            o.debug_http = True
        elif a == "--detach":
            o.detach = True
        elif a == "--continue":
            o.continue_ = True
        elif a in ("-i", "--interactive"):
            o.interactive = True
        elif a in ("--plain", "--no-tui"):
            o.plain = True
        elif a in ("-v", "--verbose"):
            o.verbose = True
        elif a in ("--version", "-V"):
            return o, ["version"]
        elif a in ("--help", "-h"):
            return o, ["help"]
        else:
            rest.append(a)
        i += 1
    return o, rest


def _shorten(text, n):
    text = " ".join(str(text or "").split())
    return text if len(text) <= n else text[: n - 1] + "…"


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        o, rest = _parse(argv)
    except SystemExit as exc:
        print(exc)
        return 2
    C.ensure_home()
    overrides = list(o.sets)
    if o.max_inflight:
        overrides.append(f"api.max_inflight={o.max_inflight}")
    if o.wait_reset:
        overrides.append("park.wait_reset=true")
    if o.debug_http:
        overrides.append("api.debug_http=true")
    cfg = C.load_config(overrides=overrides)

    if o.interactive:
        return cmd_tui(cfg, o, " ".join(rest))
    if o.continue_:
        return cmd_resume(cfg, [], o)
    if rest and rest[0] == "version":
        print(f"hydra {__version__}")
        return 0
    if rest and rest[0] == "help":
        print(HELP)
        return 0
    if not rest:
        return cmd_tui(cfg, o)
    cmd = rest[0] if rest[0] in COMMANDS else None

    if cmd == "doctor":
        return cmd_doctor(cfg, rest[1:])
    if cmd == "ls":
        return cmd_ls()
    if cmd == "usage":
        return cmd_usage(rest[1:])
    if cmd == "tail":
        return cmd_tail(rest[1:])
    if cmd == "tree":
        return cmd_tree(rest[1:])
    if cmd == "report":
        from . import report as R
        return R.cli(cfg, rest[1:], json_out=o.json_out)
    if cmd == "kill":
        return cmd_kill(rest[1:])
    if cmd == "models":
        try:
            from . import models as Models
        except ImportError:
            print("hydra: 'models' arrives with the next build phase.")
            return 2
        return Models.cli(cfg, rest[1:])
    if cmd == "resume":
        return cmd_resume(cfg, rest[1:], o)
    if cmd == "daemon":
        return cmd_daemon(cfg, rest[1:])
    if cmd == "schedule":
        return cmd_schedule(cfg, rest[1:])
    if cmd == "init":
        return cmd_init(cfg, o)
    if cmd == "map":
        return cmd_map(cfg, rest[1:])
    if cmd == "undo":
        return cmd_undo(cfg, rest[1:], o)
    if cmd == "eval":
        return cmd_eval(cfg, rest[1:], o)

    if cmd == "run":
        args = rest[1:]
        if not args:
            print("hydra: 'run' needs a task or a readable .md/.txt file")
            return 2
        candidate = Path(args[0]).expanduser()
        if len(args) == 1 and candidate.is_file():
            try:
                task = candidate.read_text(encoding="utf-8", errors="replace")
            except Exception as exc:
                print(f"hydra: cannot read {args[0]}: {exc}")
                return 2
        elif len(args) == 1 and candidate.suffix.lower() in (".md", ".txt"):
            print(f"hydra: file not found: {args[0]}")
            return 2
        else:
            task = " ".join(args)
        if not task.strip():
            print(f"hydra: {args[0]} is empty")
            return 2
    else:
        task = " ".join(rest)
    if not task.strip():
        return cmd_tui(cfg, o)
    if task.startswith("/"):
        from .project import find_commands, render_command
        head, _, args = task[1:].partition(" ")
        cmds = find_commands(os.getcwd())
        if head in cmds:
            task = render_command(cmds[head], args.strip())
        else:
            print(f"hydra: unknown command /{head} — "
                  f"available: {', '.join('/' + c for c in sorted(cmds)) or '(none)'}")
            return 2
    # Same one-shot run, but with the readable TUI when a real terminal is
    # attached. Pipes, scripts and --plain keep the line stream.
    if (_tui_ok(o) and not o.detach and not o.continue_):
        return cmd_tui(cfg, o, task)
    return cmd_run(cfg, task, o)


def _tui_ok(o) -> bool:
    if o.plain or o.quiet or o.json_out:
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stdout.isatty())
    except Exception:
        return False


def cmd_tui(cfg, o, initial: str = "") -> int:
    """Full-screen UI; falls back to the compact stream when unavailable."""
    if not _key_preflight(cfg):
        return 2
    if not _tui_ok(o):
        if initial:
            return cmd_run(cfg, initial, o)
        if o.interactive:
            return cmd_repl(cfg, o)  # pipes/scripts keep the old REPL
        print(HELP)
        return 0
    try:
        from .tui_app import run_tui
        return run_tui(cfg, o, initial=initial)
    except Exception as exc:
        print(f"\nhydra: TUI unavailable ({type(exc).__name__}: {exc}); "
              "falling back to the compact stream.\n")
        if initial:
            return cmd_run(cfg, initial, o)
        return cmd_repl(cfg, o)


# ------------------------------------------------------------------ run

def _key_preflight(cfg) -> bool:
    """Friendly stop when the real Ollama Cloud host has no API key.
    Local/fake hosts are exempt so tests never need one."""
    host = str(C.get(cfg, "api.host", "https://ollama.com") or "")
    if C.load_key(cfg) or "ollama.com" not in host:
        return True
    print("No Ollama API key found.\n"
          "\n"
          "  1. Create a key: https://ollama.com/settings/keys  (free plan works)\n"
          "  2. Save it:     echo 'YOUR_KEY' > ~/.hydra/key && chmod 600 ~/.hydra/key\n"
          "     (or export OLLAMA_API_KEY=your_key)\n"
          "  3. Check:       hydra models probe\n"
          "  4. Run:         hydra \"your task\"")
    return False


def cmd_run(cfg, task, o) -> int:
    if not _key_preflight(cfg):
        return 2
    if o.detach:
        return _spawn_detached(cfg, task, o)
    from .runtime import Runtime
    from . import mentions
    rt = Runtime(cfg, quiet=o.quiet, json_out=o.json_out,
                 verbose=getattr(o, "verbose", False))
    task = mentions.expand(cfg, task, os.getcwd(), rt.client)
    rt.wake_lock()
    restore = _install_signals(rt)
    code, report = 1, {}
    try:
        code, report = rt.run(task, name=o.name, lead_model=o.lead)
    except CallCancelled:
        # pressed outside the agent loop: there is nothing left to keep alive
        rt.run_meta.update({"status": "interrupted", "exit_code": 130})
        try:
            rt.journal.checkpoint(rt.run_meta)
        except Exception:
            pass
        code, report = 130, {"status": "interrupted",
                             "summary": "cancelled by user (Ctrl-C)"}
    except KeyboardInterrupt:
        rt.run_meta.update({"status": "interrupted", "exit_code": 130})
        try:
            rt.journal.checkpoint(rt.run_meta)
        except Exception:
            pass
        print("\ninterrupted — checkpoint saved; `hydra resume` continues")
        code, report = 130, {"status": "interrupted",
                             "summary": "cancelled by user (Ctrl-C)"}
    finally:
        restore()
        rt.wake_unlock()
        _footer(rt, report, code)
        rt.close()
    return code


def _install_signals(rt):
    """Ctrl-C (§19.8): first press cancels the in-flight call/tool and the run
    stays alive; a second press within 2 s checkpoints and exits (code 130).
    SIGTERM = checkpoint + exit. SIGHUP = checkpoint, keep running if detached."""
    prev = {}
    state = {"last": 0.0}

    def first_or_exit(signum, frame):
        now = time.monotonic()
        if now - state["last"] <= 2.0:
            raise KeyboardInterrupt
        state["last"] = now
        rt.sigint.set()
        print("\n[cancelling the in-flight work — press again within 2s to stop]",
              flush=True)
        raise CallCancelled("Ctrl-C")

    def on_term(signum, frame):
        rt.sigint.set()  # let in-flight tools kill their process groups
        raise KeyboardInterrupt

    def on_hup(signum, frame):
        try:
            rt.journal.checkpoint(rt.run_meta)
        except Exception:
            pass
        if os.environ.get("HYDRA_DETACHED") != "1":
            raise KeyboardInterrupt

    for sig, handler in ((signal.SIGINT, first_or_exit),
                         (signal.SIGTERM, on_term),
                         (signal.SIGHUP, on_hup)):
        try:
            prev[sig] = signal.getsignal(sig)
            signal.signal(sig, handler)
        except Exception:
            pass

    def restore():
        for sig, handler in prev.items():
            try:
                signal.signal(sig, handler)
            except Exception:
                pass

    return restore


INIT_TASK = """Explore this repository and write a HYDRA.md file for it at the \
repo root for future agents.

Include, briefly and concretely:
- what this project does and how it is laid out (key files/dirs)
- exact build / test / run commands
- conventions, gotchas and do-not-touch areas a fresh agent must know

Start an empty HYDRA.md, inspect the repo with ls/glob/grep/read_file, then \
write it (under ~150 lines). End with finish(status=\"done\", summary=...)."""


def cmd_init(cfg, o) -> int:
    """`hydra init` (§19.1): the lead explores the repo and writes HYDRA.md."""
    print("exploring this repo and writing HYDRA.md…")
    return cmd_run(cfg, INIT_TASK, o)


def cmd_map(cfg, args) -> int:
    """`hydra map` (§19.4): the repo map, printed."""
    from . import repo_map
    path = "."
    budget = 1500
    for a in args:
        if str(a).isdigit():
            budget = int(a)
        else:
            path = str(a)
    text, _st = repo_map.build(path, budget)
    print(text)
    return 0


def cmd_undo(cfg, args, o) -> int:
    """`hydra undo` (§19.9): restore the workdir from a shadow snapshot."""
    from . import snapshots
    run_id, to, dry, do_prune = None, None, False, False
    i = 0
    while i < len(args):
        a = str(args[i])
        if a == "--dry-run":
            dry = True
        elif a == "--prune":
            do_prune = True
        elif a == "--to" and i + 1 < len(args):
            i += 1
            try:
                to = int(args[i])
            except ValueError:
                print("hydra undo: --to needs a turn number")
                return 2
        elif a.startswith("--to="):
            try:
                to = int(a.split("=", 1)[1])
            except ValueError:
                print("hydra undo: --to needs a turn number")
                return 2
        elif a.startswith("-"):
            print(f"hydra undo: unknown flag {a!r}")
            return 2
        else:
            run_id = a
        i += 1
    root = os.getcwd()
    if do_prune:
        n = snapshots.prune(root)
        print(f"pruned {n} snapshot ref(s) older than 14 days")
        return 0
    info = snapshots.undo(root, run_id=run_id, to=to, dry_run=dry)
    if not info.get("ok"):
        print(f"hydra undo: {info.get('note')}")
        return 2
    verb = "would restore" if dry else "restored"
    print(f"run {info['run']} turn {info['turn']} ({str(info['sha'])[:12]}): "
          f"{verb} the working tree")
    changed = []
    if dry:
        try:
            p = snapshots._git(root, "diff", "--name-only", info["sha"], "--", ".")
            if p.returncode == 0:
                changed = [ln for ln in p.stdout.splitlines() if ln.strip()]
        except Exception:
            changed = []
        for rel in changed[:40]:
            print(f"  change  {rel}")
    else:
        for rel in info["restore"][:15]:
            print(f"  restore {rel}")
    for rel in info["remove"][:40]:
        print(("  remove  " if not dry else "  delete  ") + rel)
    extra = len(changed) + len(info["remove"]) - min(len(changed), 40) - min(len(info["remove"]), 40)
    if extra > 0:
        print(f"  … {extra} more")
    if info.get("fail"):
        print(f"hydra undo: restore reported: {info['fail']}")
        return 1
    if not dry:
        print("done — your branch, HEAD and index were not touched")
        print("(snapshots stay until `hydra undo --prune` removes refs older than 14 days)")
    return 0


def cmd_eval(cfg, args, o) -> int:
    """`hydra eval` (§19.11): scripted fake-server suites, zero credits."""
    if "--real" in args and not _key_preflight(cfg):
        return 2
    from . import evals
    return evals.cli(cfg, args)


def _spawn_detached(cfg, task, o) -> int:
    """Run the task away from this terminal: tmux when installed (so it
    survives closing the Termux UI and can be re-attached), else a plain
    new-session process. Returns immediately."""
    logdir = C.home() / "detached"
    logdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%m%d_%H%M%S")
    log = logdir / f"{stamp}.log"
    args = []
    for s in o.sets:
        args += ["--set", s]
    if o.max_inflight:
        args += ["--max-inflight", str(o.max_inflight)]
    if o.lead:
        args += ["--lead", o.lead]
    if o.name:
        args += ["--name", o.name]
    if o.quiet:
        args += ["--quiet"]
    if o.wait_reset:
        args += ["--wait-reset"]
    if o.debug_http:
        args += ["--debug-http"]
    cmd = [sys.executable, "-m", "hydra"] + args + [task]
    env = dict(os.environ)
    env["HYDRA_DETACHED"] = "1"
    root = str(Path(__file__).resolve().parents[1])
    have = env.get("PYTHONPATH", "").split(os.pathsep) if env.get("PYTHONPATH") else []
    if root not in have:
        env["PYTHONPATH"] = os.pathsep.join([root] + have) if have else root
    if shutil.which("tmux"):
        import shlex
        name = f"hydra_{stamp}"
        inner = ("termux-wake-lock 2>/dev/null; "
                 "HYDRA_DETACHED=1 "
                 + " ".join(shlex.quote(a) for a in cmd)
                 + f" 2>&1 | tee -a {shlex.quote(str(log))}; "
                   "termux-wake-unlock 2>/dev/null")
        try:
            subprocess.run(["tmux", "new-session", "-d", "-s", name, inner],
                           check=True, timeout=30)
            print(f"detached in tmux session {name}")
            print(f"  attach:  tmux attach -t {name}")
            print(f"  log:     {log}")
            print("  progress: hydra ls · hydra tail")
            return 0
        except Exception as exc:
            print(f"tmux failed ({exc}); using a plain detached process")
    with open(log, "ab") as fh:
        subprocess.Popen(cmd, stdout=fh, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, cwd=os.getcwd(),
                         env=env, start_new_session=True)
    print("detached (new session — survives closing Termux; tmux not installed)")
    print(f"  log:      {log}")
    print("  progress: hydra ls · hydra tail")
    return 0


def cmd_resume(cfg, args, o) -> int:
    if not _key_preflight(cfg):
        return 2
    from .runtime import Runtime
    which = args[0] if args else None
    rt = Runtime(cfg, quiet=o.quiet, json_out=o.json_out,
                 verbose=getattr(o, "verbose", False))
    rt.wake_lock()
    code, report = 1, {}
    try:
        code, report = rt.resume_run(which)
    finally:
        rt.wake_unlock()
        _footer(rt, report, code)
        rt.close()
    return code


def _footer(rt, report, code) -> None:
    tot = rt.usage_totals()
    turns = sum(getattr(a, "turns", 0) for a in rt.agents.values())
    started = rt.run_meta.get("started") or time.time()
    dur = max(0, int(time.time() - started))
    mins, secs = divmod(dur, 60)
    status = (report or {}).get("status") or \
        {"75": "parked", "130": "interrupted"}.get(str(code), "failed" if code else "done")
    if rt.ui.json_out:
        print(json.dumps({"type": "run_end", "status": status, "exit_code": code,
                          "turns": turns, "agents": len(rt.agents),
                          "in": tot["in"], "out": tot["out"],
                          "est_usd": round(tot["est_usd"], 6)}, ensure_ascii=False))
        return
    summary = (report or {}).get("summary")
    if (summary and not rt.ui.quiet and not getattr(rt.ui, "json_out", False)
            and not getattr(rt.ui, "printed_report", False)):
        print("\n" + summary.strip())
    print(f"\n{status} · {turns} turns · {len(rt.agents)} agents · "
          f"{tot['in'] // 1000}k in / {tot['out'] // 1000}k out · "
          f"est ${tot['est_usd']:.4f} · {mins:02d}:{secs:02d}")


# ------------------------------------------------------------------ repl

def cmd_repl(cfg, o) -> int:
    """Interactive mode: the run works in a background thread while stdin
    stays free — type to steer, or use /commands. Nothing here gates the
    agent; it only adds a way to talk to it."""
    if not _key_preflight(cfg):
        return 2
    from .runtime import Runtime
    rt = Runtime(cfg, quiet=o.quiet, json_out=o.json_out,
                 verbose=getattr(o, "verbose", False))
    rt.wake_lock()
    restore = _install_signals(rt)
    code, report = 1, {}
    try:
        if not o.quiet:
            print("hydra REPL · type the task, then steer anytime · /help for commands")
        try:
            task = input("task> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not task:
            return 0
        from . import mentions
        task = mentions.expand(cfg, task, rt.cwd, rt.client)
        box = {}

        def work():
            try:
                box["code"], box["report"] = rt.run(task, name=o.name,
                                                    lead_model=o.lead)
            except Exception as exc:  # never let the thread die silently
                box["code"] = 1
                box["report"] = {"status": "failed",
                                 "summary": f"{type(exc).__name__}: {exc}"}

        th = threading.Thread(target=work, daemon=True)
        th.start()
        # A blocking reader thread, not select() on sys.stdin: the first-line
        # read may have buffered lines ahead that select() would never see.
        import queue as _queue
        inbox: "_queue.Queue" = _queue.Queue()
        stop_reader = threading.Event()

        def _read_stdin():
            while not stop_reader.is_set():
                try:
                    line = sys.stdin.readline()
                except Exception:
                    line = ""
                if line == "":
                    inbox.put(None)
                    return
                inbox.put(line)

        threading.Thread(target=_read_stdin, daemon=True,
                         name="repl-stdin").start()
        pending = []  # steers typed before the lead agent exists
        notes_pending = []  # /note text typed before the journal exists
        try:
            while th.is_alive():
                lead = rt.agents.get("a0")
                if lead is not None and pending:  # deliver as soon as it shows up
                    for m in pending:
                        lead.send(m)
                    pending.clear()
                    print("→ steered")
                if notes_pending and rt.journal is not None:
                    try:
                        for text in notes_pending:
                            rt.journal.blackboard_append("repl", text, persist=False)
                        notes_pending.clear()
                        print("noted")
                    except Exception:
                        pass   # the run just ended and closed the journal
                try:
                    line = inbox.get(timeout=0.25)
                except _queue.Empty:
                    continue
                except KeyboardInterrupt:
                    raise
                if line is None:  # stdin closed — the run continues to the end
                    break
                line = line.strip()
                if not line:
                    continue
                if line.startswith("/"):
                    if not _repl_cmd(rt, line, pending, notes_pending):
                        break
                else:
                    from . import mentions
                    pending.append(mentions.expand(rt.cfg, line, rt.cwd, rt.client))
                    if rt.agents.get("a0") is None:
                        print("→ queued (the run is still starting)")
        except KeyboardInterrupt:
            for a in list(rt.agents.values()):
                a.cancelled.set()
            print("\ninterrupted — checkpoint saved; `hydra resume` continues")
            code = 130
            report = {"status": "interrupted", "summary": "cancelled by user (Ctrl-C)"}
        finally:
            stop_reader.set()
        th.join(timeout=15)
        if code != 130:
            code = box.get("code", 1)
            report = box.get("report", {})
    finally:
        restore()
        rt.wake_unlock()
        _footer(rt, report, code)
        rt.close()
    return code


def _repl_cmd(rt, line, pending=None, notes=None) -> bool:
    """Handle one /command. Returns False to leave the REPL loop."""
    parts = line.split(None, 1)
    cmd = parts[0]
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/quit", "/exit", "/q"):
        for a in list(rt.agents.values()):
            a.cancelled.set()
        print("stopping… (run state is journaled; hydra resume brings it back)")
        return False
    if cmd == "/tree":
        print(rt.swarm.status_lines())
    elif cmd == "/kill":
        if arg:
            print(rt.swarm.kill(arg))
        else:
            print("usage: /kill aN")
    elif cmd == "/model":
        role, _, model = arg.partition("=")
        if not model:
            model, role = role.strip(), "lead"
        role = role.strip()
        if not role or not model:
            print("usage: /model [role=]model")
        else:
            base = list(C.get(rt.cfg, f"roles.{role}.chain") or [])
            C.set_path(rt.cfg, f"roles.{role}.chain",
                       [model] + [m for m in base if m != model])
            print(f"{role} chain: {model} first")
    elif cmd == "/models":
        for role in ("lead", "worker", "researcher", "critic", "summarizer",
                     "reviewer", "vision"):
            chain = C.get(rt.cfg, f"roles.{role}.chain") or []
            print(f"{role:<10} " + " → ".join(str(m) for m in chain))
        reg = getattr(rt, "registry", None)
        if reg is not None:
            try:
                for m in reg.list_all():
                    print(f"  {m.get('name'):<24} {m.get('status', '?')}")
            except Exception:
                pass
    elif cmd == "/cost":
        tot = rt.usage_totals()
        print(f"{tot['in']} in / {tot['out']} out · {tot['calls']} calls · "
              f"est ${tot['est_usd']:.4f}")
    elif cmd == "/compact":
        lead = rt.agents.get("a0")
        if lead is None:
            print("no lead yet")
        else:
            ok = False
            try:
                ok = bool(lead.compact(rt))
            except Exception as exc:
                print(f"compact failed: {exc}")
            print("compacted" if ok else "nothing to compact yet")
    elif cmd == "/plan":
        lead = rt.agents.get("a0")
        if lead is not None:
            def _plan_async(lead=lead):
                try:
                    from . import planner
                    dag, err = planner.make_dag(rt, lead.task or "the current task")
                except Exception as exc:
                    dag, err = None, f"{type(exc).__name__}: {exc}"
                if dag is not None:
                    text = planner.render(dag)
                    print("\n" + text, flush=True)
                    lead.nudge("Here is the plan the user requested. Follow this "
                               "DAG (todo tool to track it) and execute step by "
                               "step:\n" + text)
                    print("plan sent to the lead", flush=True)
                else:
                    lead.nudge("PLAN FIRST: before any further tool action, emit "
                               "your plan as a todo list (todo tool), then execute "
                               "it step by step.")
                    print(f"plan requested from the lead ({err})", flush=True)

            threading.Thread(target=_plan_async, daemon=True).start()
            print("planning in the background…")
        else:
            msg = ("PLAN FIRST: before any further tool action, emit your plan as a "
                   "todo list (todo tool), then execute it step by step.")
            if pending is not None:
                pending.append(msg)
            print("plan requested (queued until the run starts)")
    elif cmd == "/pause":
        rt.paused.set()
        print("paused between turns — /resume to continue")
    elif cmd == "/resume":
        rt.paused.clear()
        print("resumed")
    elif cmd == "/note":
        if not arg:
            print("usage: /note text")
        elif rt.journal:
            rt.journal.blackboard_append("repl", arg, persist=False)
            print("noted")
        elif notes is not None:
            notes.append(arg)   # run still starting: write once the journal exists
        else:
            print("usage: /note text")
    elif cmd in ("/help", "/?"):
        print(REPL_HELP)
    else:
        from .project import find_commands, render_command
        cmds = find_commands(getattr(rt, "cwd", os.getcwd()))
        head = cmd.lstrip("/")
        if head in cmds:
            rendered = render_command(cmds[head], arg)
            lead = rt.agents.get("a0")
            if lead is not None:
                lead.send(rendered)
                print(f"→ /{head} sent")
            elif pending is not None:
                pending.append(rendered)
                print(f"→ /{head} queued (the run is still starting)")
            else:
                print(f"/{head}: no run to send it to")
        else:
            print(f"unknown command {cmd} — /help")
    return True


# ------------------------------------------------------------------ daemon

def _daemon_result(task, src, code, report, rid, usage) -> str:
    status = (report or {}).get("status") or ("done" if code == 0 else "failed")
    lines = [f"# {src}", "",
             f"status: {status}",
             f"exit_code: {code}",
             f"run: {rid or '-'}",
             f"cwd: {os.getcwd()}", "",
             "## task", task.strip(), "", "## summary",
             str((report or {}).get("summary") or "(none)").strip()]
    files = (report or {}).get("files_changed") or []
    if files:
        lines += ["", "## files changed", *[f"- {f}" for f in files]]
    if usage:
        lines += ["", "## usage",
                  f"{usage.get('in', 0)} in / {usage.get('out', 0)} out · "
                  f"est ${usage.get('est_usd', 0.0):.4f}"]
    return "\n".join(lines) + "\n"


def cmd_daemon(cfg, args) -> int:
    """Watch ~/.hydra/queue/*.md: run each as a task, write the result to
    queue/done/<name>.result.md, remove the source file. This is the
    integration seam for any external bridge (SPEC §13)."""
    if not _key_preflight(cfg):
        return 2
    from .runtime import Runtime
    once = "--once" in args
    qdir = C.home() / "queue"
    donedir = qdir / "done"
    qdir.mkdir(parents=True, exist_ok=True)
    donedir.mkdir(parents=True, exist_ok=True)
    poll = float(C.get(cfg, "daemon.poll_secs", 5) or 5)
    stop = threading.Event()

    def on_sig(signum, frame):
        stop.set()

    old = {}
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            old[sig] = signal.getsignal(sig)
            signal.signal(sig, on_sig)
        except Exception:
            pass
    print(f"hydra daemon: watching {qdir} (poll {poll}s)")
    print(f"  drop a *.md task file there; results land in {donedir}/")
    try:
        while not stop.is_set():
            for p in sorted(qdir.glob("*.md")):
                if stop.is_set():
                    break
                if not p.is_file() or p.name.endswith(".result.md"):
                    continue
                task = p.read_text(encoding="utf-8", errors="replace").strip()
                if not task:
                    continue
                print(f"[daemon] {p.name}: {_shorten(task, 64)}")
                code, report, rid, usage = 1, {}, None, {}
                rt = None
                try:
                    rt = Runtime(cfg, quiet=True)
                    rt.wake_lock()
                    code, report = rt.run(task, name=p.stem)
                    rid = rt.run_id
                    usage = rt.usage_totals()
                except Exception as exc:
                    report = {"status": "failed",
                              "summary": f"{type(exc).__name__}: {exc}"}
                finally:
                    if rt is not None:
                        rt.wake_unlock()
                        rt.close()
                out = donedir / f"{p.stem}.result.md"
                try:
                    out.write_text(_daemon_result(task, p.name, code, report,
                                                  rid, usage),
                                   encoding="utf-8")
                except Exception as exc:
                    print(f"[daemon] could not write {out}: {exc}")
                try:
                    p.unlink()
                except Exception:
                    pass
                print(f"[daemon] {p.name} -> {out.name} "
                      f"({(report or {}).get('status') or '?'}, exit {code})")
            # scheduled jobs (§19.8): fired by the same daemon timer
            try:
                from . import schedule as S
                for job in S.load_jobs():
                    if stop.is_set():
                        break
                    if not S.is_due(job):
                        continue
                    jid = str(job.get("id"))
                    ts = time.strftime("%m%d_%H%M%S")
                    task = str(job.get("task") or "")
                    print(f"[daemon] schedule {jid}: {_shorten(task, 50)}")
                    code, report = 1, {}
                    rt = None
                    try:
                        rt = Runtime(cfg, quiet=True)
                        rt.wake_lock()
                        code, report = rt.run(task, name=f"sched-{jid}-{ts}")
                    except Exception as exc:
                        report = {"status": "failed",
                                  "summary": f"{type(exc).__name__}: {exc}"}
                    finally:
                        if rt is not None:
                            rt.wake_unlock()
                            rt.close()
                    S.mark_fired(jid)
                    print(f"[daemon] schedule {jid} done "
                          f"({(report or {}).get('status') or '?'}, exit {code})")
            except Exception as exc:
                print(f"[daemon] schedule error: {exc}")
            if once:
                break
            deadline = time.time() + poll
            while time.time() < deadline and not stop.is_set():
                time.sleep(0.1)
    finally:
        for sig, handler in old.items():
            try:
                signal.signal(sig, handler)
            except Exception:
                pass
    print("hydra daemon: stopped")
    return 0


# ------------------------------------------------------------------ schedule
def cmd_schedule(cfg, args) -> int:
    from . import schedule as S
    sub = (args[0] if args else "ls").lower()
    if sub in ("ls", "list"):
        jobs = S.load_jobs()
        if not jobs:
            print("no schedule entries — hydra schedule add \"every 30m\" \"<task>\"")
            return 0
        now = time.time()
        for j in jobs:
            last = float(j.get("last") or 0.0)
            ago = f"{int((now - last) // 60)}m ago" if last else "never"
            print(f"{str(j.get('id')):<10} {str(j.get('when')):<14} "
                  f"last={ago:<10} {_shorten(j.get('task'), 60)}")
        print("fired by `hydra daemon`; `hydra schedule export` hands them to "
              "termux-job-scheduler (Android minimum 15 min)")
        return 0
    if sub == "add":
        if len(args) < 3:
            print("usage: hydra schedule add \"every 30m\" \"<task>\"")
            return 2
        try:
            jid = S.add(args[1], " ".join(args[2:]))
        except ValueError as exc:
            print(f"hydra: {exc}")
            return 2
        print(f"schedule {jid}: {args[1]} -> {' '.join(args[2:])}")
        return 0
    if sub in ("rm", "del", "remove"):
        if len(args) < 2:
            print("usage: hydra schedule rm <id>")
            return 2
        ok = S.remove(args[1])
        print("removed" if ok else f"no such job: {args[1]}")
        return 0 if ok else 2
    if sub == "export":
        print(S.export_termux())
        return 0
    print("usage: hydra schedule add|ls|rm|export")
    return 2


# ------------------------------------------------------------------ inspect

def _resolve_run(args):
    if args:
        rid = J.find_run(args[0])
        if not rid:
            print(f"hydra: run not found: {args[0]}")
        return rid
    rid = J.last_run_in(os.getcwd())
    if rid:
        return rid
    return (J.run_ids() or [None])[0]


def cmd_ls() -> int:
    runs = J.run_ids()
    if not runs:
        print("no runs yet — start one with:  hydra \"your task\"")
        return 0
    for rid in runs[:30]:
        d = J.load_run(rid) or {}
        when = time.strftime("%m-%d %H:%M", time.localtime(d.get("started") or 0))
        label = d.get("name") or _shorten(d.get("task"), 46)
        print(f"{rid:<16} {str(d.get('status', '?')):<10} {when}  {label}")
    return 0


def cmd_tail(args) -> int:
    agent = None
    rest = []
    i = 0
    while i < len(args):
        if args[i] == "-a" and i + 1 < len(args):
            agent = args[i + 1]
            i += 2
        else:
            rest.append(args[i])
            i += 1
    rid = _resolve_run(rest)
    if not rid:
        return 2
    events = J.read_events(rid, agent=agent)
    for ev in events[-40:]:
        hh = time.strftime("%H:%M:%S", time.localtime(ev.get("ts") or 0))
        tag = str(ev.get("agent") or "-")
        body = {k: v for k, v in ev.items()
                if k not in ("ts", "run", "agent", "seq", "type")}
        print(f"{hh} [{tag:<4}] {ev.get('type'):<14} {_shorten(body, 90)}")
    return 0


def cmd_tree(args) -> int:
    rid = _resolve_run(args)
    if not rid:
        return 2
    infos = {}
    order = []
    for ev in J.read_events(rid):
        t = ev.get("type")
        a = ev.get("agent")
        if t == "agent_start" and a not in infos:
            infos[a] = {"role": ev.get("role"), "depth": int(ev.get("depth") or 0),
                        "parent": ev.get("parent"), "status": "running",
                        "in": 0, "out": 0}
            order.append(a)
        elif t == "model_response" and a in infos:
            infos[a]["in"] += int(ev.get("in_tokens") or 0)
            infos[a]["out"] += int(ev.get("out_tokens") or 0)
        elif t == "report" and a in infos:
            infos[a]["status"] = (ev.get("report") or {}).get("status", "?")
    if not infos:
        print(f"{rid}: no agent events")
        return 0
    print(f"{rid}")
    for a in order:
        d = infos[a]
        print("  " * d["depth"] +
              f"{a} {d['role']:<10} {d['status']:<8} {d['in'] // 1000}k in / {d['out'] // 1000}k out")
    return 0


def cmd_usage(args) -> int:
    rid = None
    if args:
        rid = J.find_run(args[0])
        if not rid:
            print(f"hydra: run not found: {args[0]}")
            return 2
    recs = J.read_usage(rid)
    if not recs:
        print("no usage recorded yet")
        return 0
    by_model = {}
    for r in recs:
        d = by_model.setdefault(r.get("model", "?"), {"in": 0, "out": 0, "est": 0.0, "n": 0})
        d["in"] += int(r.get("in", 0))
        d["out"] += int(r.get("out", 0))
        d["est"] += float(r.get("est_usd", 0.0))
        d["n"] += 1
    scope = rid or "all runs"
    print(f"usage ({scope}):")
    for m, d in sorted(by_model.items()):
        print(f"  {m:<24} {d['n']:>3} calls  {d['in'] // 1000:>5}k in  "
              f"{d['out'] // 1000:>4}k out  ~${d['est']:.4f}")
    tin = sum(d["in"] for d in by_model.values())
    tout = sum(d["out"] for d in by_model.values())
    est = sum(d["est"] for d in by_model.values())
    print(f"  total: {tin // 1000}k in / {tout // 1000}k out · est ${est:.4f}")
    return 0


def cmd_kill(args) -> int:
    rid = J.find_run(args[0]) if args else None
    if not rid:
        print("usage: hydra kill RUN_ID")
        return 2
    d = J.load_run(rid) or {}
    pid = d.get("pid")
    result = "marked killed (process not found)"
    if pid:
        try:
            os.killpg(int(pid), signal.SIGKILL)
            result = f"killed process group {pid}"
        except Exception:
            try:
                os.kill(int(pid), signal.SIGKILL)
                result = f"killed pid {pid}"
            except Exception:
                pass
    d["status"] = "killed"
    J.save_run_atomic(rid, d)
    print(f"{rid}: {result}")
    return 0


# ------------------------------------------------------------------ doctor

def _termux_ok() -> bool:
    from . import caps
    return caps.termux_ok()


def _doctor_limits(cfg) -> int:
    """`hydra doctor --limits`: T08 probe, <=6 tiny calls, write limits.json."""
    from . import hygiene
    from . import limits as Limits
    from .client import OllamaCloud

    key = C.load_key(cfg) or ""
    host = C.get(cfg, "api.host", "https://ollama.com")
    cli = OllamaCloud(host, key, C.get(cfg, "api.connect_timeout", 15) or 15,
                      C.get(cfg, "api.idle_timeout", 90) or 90)
    cli.redact = hygiene.make_redactor(cfg)
    try:
        data = Limits.probe(cli, cfg)
    except Limits.ProbeError as exc:
        print(f"[FAIL] limits probe: {exc}")
        return 1
    except Exception as exc:
        print(f"[FAIL] limits probe: {type(exc).__name__}: {exc}")
        return 1
    finally:
        cli.close()
    Limits.save_limits(data)
    print(f"limits: behaviour={data['behaviour']} "
          f"max_parallel_ok={data['max_parallel_ok']} "
          f"model={data.get('model')} calls={data.get('calls')} "
          f"checked_at={data.get('checked_at')}")
    print(f"wrote {Limits.limits_path()}")
    return 0


def _doctor_caps(cfg) -> int:
    """`hydra doctor --caps`: boolean probe of the local + hosted tools."""
    from . import caps as Caps
    from . import hygiene
    from .client import OllamaCloud

    key = C.load_key(cfg) or ""
    host = C.get(cfg, "api.host", "https://ollama.com")
    cli = OllamaCloud(host, key, C.get(cfg, "api.connect_timeout", 15) or 15)
    cli.redact = hygiene.make_redactor(cfg)
    try:
        data = Caps.get_caps(cfg, client=cli, force=True)
    except Exception as exc:
        print(f"[FAIL] caps detect: {type(exc).__name__}: {exc}")
        return 1
    finally:
        cli.close()
    for name in Caps.CAP_KEYS:
        mark = "ok  " if data.get(name) else "no  "
        extra = ""
        if name == "vision" and data.get("vision_model"):
            extra = f" ({data['vision_model']})"
        print(f"[{mark}] {name}{extra}")
    print(f"wrote {Caps.caps_path()}  ({Caps.env_line(data)})")
    return 0


def cmd_doctor(cfg, args=None) -> int:
    from .client import OllamaCloud
    args = list(args or [])
    if "--caps" in args or "--limits" in args:
        code = 0
        if "--caps" in args:
            code = _doctor_caps(cfg) or code
        if "--limits" in args:
            code = _doctor_limits(cfg) or code
        return code
    ok = True

    def chk(label, cond, hint="", soft=False):
        nonlocal ok
        mark = "ok  " if cond else ("warn" if soft else "FAIL")
        line = f"[{mark}] {label}"
        if not cond and hint:
            line += f"  -> {hint}"
        print(line)
        if not soft:
            ok = ok and bool(cond)

    chk("python >= 3.11", sys.version_info >= (3, 11), "pkg install python")
    chk("git", shutil.which("git"), "pkg install git")
    chk("ripgrep (rg)", shutil.which("rg"), "pkg install ripgrep")
    chk("jq (optional)", shutil.which("jq"), "pkg install jq")
    key = C.load_key(cfg)
    chk("Ollama API key", bool(key),
        "echo your_key > ~/.hydra/key && chmod 600 ~/.hydra/key")
    if key:
        host = C.get(cfg, "api.host", "https://ollama.com")
        t0 = time.time()
        try:
            cli = OllamaCloud(host, key, 8, 15)
            tags = cli.tags()
            ms = int((time.time() - t0) * 1000)
            chk(f"api reachable ({host}, {ms} ms, {len(tags)} models)", True)
        except Exception as exc:
            chk(f"api reachable ({host})", False, f"{type(exc).__name__}: {exc}")
    chk("termux-api battery probe", _termux_ok(), "pkg install termux-api + Termux:API app")
    chk("tmux (--detach)", shutil.which("tmux"),
        "pkg install tmux (falls back to a plain new session)", soft=True)
    chk("termux-notification (finish/park alerts)",
        shutil.which("termux-notification"),
        "pkg install termux-api + Termux:API app", soft=True)
    try:
        probe = C.home() / ".doctor"
        probe.write_text("x", encoding="utf-8")
        probe.unlink()
        chk(f"~/.hydra writable ({C.home()})", True)
    except Exception as exc:
        chk("~/.hydra writable", False, str(exc))
    print("\ndoctor: " + ("all good" if ok else "fix the FAIL lines above"))
    return 0 if ok else 1
