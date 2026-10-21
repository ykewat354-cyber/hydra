"""Shell tools: bash, background jobs, python snippets."""
from __future__ import annotations

import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

from . import Tool
from .. import jobs as Jobs


def _bash() -> str:
    cand = Path(os.environ.get("PREFIX", "/data/data/com.termux/files/usr")) / "bin" / "bash"
    if cand.exists():
        return str(cand)
    return shutil.which("bash") or "sh"


def _env() -> dict:
    env = dict(os.environ)
    env.update({
        "TERM": "dumb",
        "PAGER": "cat",
        "GIT_PAGER": "cat",
        "CI": "1",
        "DEBIAN_FRONTEND": "noninteractive",
        "GIT_TERMINAL_PROMPT": "0",
    })
    return env


def _kill_group(proc) -> None:
    # Every Popen here starts a new session, so the process-group id is the
    # original child's pid.  Look it up by pid, not with getpgid(proc.pid):
    # after the shell exits, its pid may be gone while a background child is
    # still alive in the same group.  The old fallback (proc.kill) only
    # signalled the already-dead shell and left that child running.
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def run_shell(rt, cwd, cmd: str, timeout: float, stdin_text=None) -> str:
    try:
        proc = subprocess.Popen(
            [_bash(), "-c", cmd], cwd=str(cwd), env=_env(),
            stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
    except Exception as exc:
        return f"ERROR: could not start shell: {exc}"

    if stdin_text is not None:
        import threading

        def _feed():
            try:
                proc.stdin.write(stdin_text.encode())
                proc.stdin.close()
            except Exception:
                pass

        threading.Thread(target=_feed, daemon=True).start()

    end = time.time() + float(timeout)
    fd = proc.stdout.fileno()
    chunks: list = []
    cancelled = False
    timed_out = False
    last_emit = 0.0
    emitted = 0
    while True:
        if rt is not None and getattr(rt, "sigint", None) is not None \
                and rt.sigint.is_set():
            cancelled = True
            _kill_group(proc)
        slice_s = max(0.05, min(0.4, end - time.time()))
        try:
            ready, _, _ = select.select([fd], [], [], slice_s)
        except Exception:
            ready = []
        if ready:
            try:
                data = os.read(fd, 65536)
            except OSError:
                data = b""
            if not data:
                break  # EOF
            chunks.append(data)
        else:
            if proc.poll() is not None:
                # process is gone: one last look for bytes, then stop
                try:
                    ready2, _, _ = select.select([fd], [], [], 0.2)
                except Exception:
                    ready2 = []
                if not ready2:
                    break
                continue
        if cancelled:
            break
        # The deadline applies even when the shell itself has already exited:
        # a background child can keep the stdout pipe open and keep writing
        # (for example a foreground `python3 -m http.server`).  In that case
        # proc.poll() is not None forever, and the old `and proc.poll() is
        # None` condition meant the tool could wait indefinitely.  Kill the
        # whole process group and return a bounded error instead.
        if time.time() >= end:
            timed_out = True
            _kill_group(proc)
            break
        # §19.10: the last lines of long-running commands stream to the TUI
        now = time.time()
        if chunks and proc.poll() is None and now - last_emit >= 0.8:
            last_emit = now
            try:
                text = b"".join(chunks).decode("utf-8", "replace")
                lines = text.splitlines()
                complete = len(lines) - (0 if text.endswith("\n") else 1)
                new = lines[emitted:max(emitted, complete)]
                emitted = max(emitted, complete)
                ui = getattr(rt, "ui", None)
                if ui is not None and not getattr(ui, "quiet", False) \
                        and not getattr(ui, "json_out", False):
                    for ln in new[-20:]:
                        if ln.strip():
                            ui.info("   | " + ln[:200])
            except Exception:
                pass

    try:
        proc.wait(timeout=5)
    except Exception:
        _kill_group(proc)
    # Never block here.  A daemonized grandchild can escape the process
    # group, keep the pipe open, and make a plain os.read hang forever even
    # after the timeout branch has fired.
    try:
        os.set_blocking(fd, False)
        while True:
            rest = os.read(fd, 65536)
            if not rest:
                break
            chunks.append(rest)
    except Exception:
        pass
    try:
        proc.stdout.close()
    except Exception:
        pass
    text = b"".join(chunks).decode("utf-8", "replace")
    if cancelled:
        return text + "\n[interrupted by Ctrl-C — process group killed]"
    if timed_out:
        return text + (f"\n[timeout after {timeout}s — process group killed; "
                       "for a server or long job, retry with background=true]")
    if proc.returncode not in (0, None):
        text += f"\n[exit {proc.returncode}]"
    return text


def _tool_bash(args, ctx) -> str:
    cmd = str(args.get("cmd") or "").strip()
    if not cmd:
        return "ERROR: empty cmd"
    timeout = float(args.get("timeout") or 0) or float(
        ctx.cfg.get("loop", {}).get("tool_timeout", 120) or 120)
    cwd = Path(args.get("cwd") or ctx.cwd)
    if not cwd.is_absolute():
        cwd = ctx.cwd / cwd
    if args.get("background"):
        return _background(ctx, cwd, cmd)
    # P1/A4: a single-line server started with a shell ``&`` (the pattern
    # weak models use) must still become a tracked job, otherwise the
    # one-port rule and served_url have nothing to work with.
    plain = cmd.strip()
    if plain.endswith("&") and "\n" not in plain:
        body = plain[:-1].rstrip()
        if body and _server_port(body):
            return _background(ctx, cwd, body)
    return run_shell(ctx.rt, cwd, cmd, timeout, stdin_text=args.get("stdin"))


_PORT_PATTERNS = (
    re.compile(r"(?:--port[= ]|-p\s+|http\.server\s+|--bind[= ][^\s]*:|:)(\d{2,5})\b"),
    re.compile(r"\bPORT=(\d{2,5})\b"),
)
_SERVER_WORDS = ("server", "serve", "http.server", "uvicorn", "gunicorn",
                 "flask", "vite", "npm", "node", "django", "runserver",
                 "streamlit", "gradio")


def _server_port(cmd: str):
    """(port, start, end) for a likely server command, else None."""
    low = cmd.lower()
    if not any(w in low for w in _SERVER_WORDS):
        return None
    for rx in _PORT_PATTERNS:
        m = rx.search(cmd)
        if not m:
            continue
        try:
            port = int(m.group(1))
        except Exception:
            continue
        if 1 <= port <= 65535:
            return port, m.start(1), m.end(1)
    return None


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=0.25):
            return True
    except OSError:
        return False


def _free_port_after(port: int):
    for cand in range(int(port) + 1, min(65535, int(port) + 51)):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("127.0.0.1", cand))
                return cand
        except OSError:
            continue
    return None


def _job_alive(job) -> bool:
    # Duplicate-start guard: the process may be starting and not yet bound,
    # but the OS PID still means this project already has a job.
    return Jobs.job_process_alive(job)


def _job_state_text(job) -> str:
    state = Jobs.job_state(job)
    if state == "running":
        return "running"
    if state == "starting":
        return "starting"
    try:
        rc = job["proc"].poll()
    except Exception:
        rc = None
    return "exited" if rc is None else f"exited({rc})"


def _probe_served(rt, jid: str, url: str, *, timeout: float = 5.0) -> bool:
    """P1/A4: one bounded HYDRA GET while the job is starting.

    A 200 is journaled as ``served_probe`` and stored on the job, giving the
    report a ``served_url`` that HYDRA itself observed even if the agent stops
    the server before ``finish``.  No response (or a non-200) simply leaves it
    unset.  Returns True on a 200.
    """
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1.0) as resp:
                if int(getattr(resp, "status", 0) or 0) == 200:
                    job = (getattr(rt, "jobs", {}) or {}).get(jid)
                    if isinstance(job, dict):
                        job["served_url"] = url
                    if getattr(rt, "journal", None) is not None:
                        rt.journal.event("served_probe", None, jid=jid,
                                         url=url, status=200)
                    return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def _background(ctx, cwd: Path, cmd: str) -> str:
    rt = ctx.rt
    port = None
    note = ""
    found = _server_port(cmd)
    if found:
        port, start, end = found
        ours = [(j, m) for j, m in (getattr(rt, "jobs", {}) or {}).items()
                if m.get("port") == port and _job_alive(m)
                and str(m.get("cwd") or "") == str(cwd)]
        nudged = getattr(rt, "server_nudged", None)
        if not isinstance(nudged, set):
            nudged = set()
            rt.server_nudged = nudged
        if ours:
            jid0, meta0 = ours[0]
            key = (str(cwd), port)
            first = key not in nudged
            nudged.add(key)
            head = "NUDGE" if first else "already serving"
            return (f"{head}: {jid0} is already serving this project on port "
                    f"{port} (log: {meta0.get('log')}). Reuse that job instead "
                    f"of starting a second server. A second process would shadow "
                    f"the first and make the report ambiguous.")
        if _port_open(port):
            free = _free_port_after(port)
            if free is None:
                return (f"ERROR: port {port} is already in use and no free port "
                        f"was found nearby; pick a free port and state it.")
            cmd = cmd[:start] + str(free) + cmd[end:]
            note = (f"\nNOTE: port {port} was already in use by another process; "
                    f"started on port {free} instead "
                    f"(http://127.0.0.1:{free}/). State this port in your report.")
            port = free
    jid = rt.next_job_id()
    logdir = (rt.journal.dir / "jobs") if rt.journal else (Path(tempfile.gettempdir()) / "hydra-jobs")
    logdir.mkdir(parents=True, exist_ok=True)
    log = logdir / f"{jid}.log"
    fh = None
    try:
        fh = open(log, "ab")
        proc = subprocess.Popen(
            [_bash(), "-c", cmd], cwd=str(cwd), env=_env(),
            stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
            start_new_session=True)
    except Exception as exc:
        return f"ERROR: could not start background job: {exc}"
    finally:
        if fh is not None:  # the child holds its own dup of the fd
            try:
                fh.close()
            except Exception:
                pass
    meta = {"proc": proc, "log": log, "cmd": cmd, "started": time.time(),
            "agent": ctx.agent.id, "cwd": str(cwd), "port": port,
            "url": f"http://127.0.0.1:{port}/" if port else ""}
    rt.jobs[jid] = meta
    if rt.journal is not None:
        # Persist enough to re-attach after a crash: the job outlives us
        # (start_new_session) but the in-memory Popen handle does not.
        rt.journal.event("job_start", ctx.agent.id, jid=jid, pid=proc.pid,
                         cmd=cmd, log=str(log), started=meta["started"],
                         port=port, url=meta["url"])
    started = f"started job {jid} (pid {proc.pid}); log: {log}"
    if meta["url"]:
        started += f"; url: {meta['url']}"
        if _probe_served(rt, jid, meta["url"], timeout=1.5):
            started += "; hydra GET: HTTP 200"
        else:
            try:
                threading.Thread(
                    target=_probe_served, args=(rt, jid, meta["url"]),
                    kwargs={"timeout": 8.0}, daemon=True).start()
            except Exception:
                pass
    return started + note


def _tool_python(args, ctx) -> str:
    code = str(args.get("code") or "")
    if not code.strip():
        return "ERROR: empty code"
    timeout = float(args.get("timeout") or 0) or float(
        ctx.cfg.get("loop", {}).get("tool_timeout", 120) or 120)
    tmp = Path(tempfile.gettempdir()) / f"hydra_snip_{os.getpid()}_{int(time.time()*1000)}.py"
    try:
        tmp.write_text(code, encoding="utf-8")
        py = shutil.which("python") or sys.executable
        out = run_shell(ctx.rt, ctx.cwd, f'{py} {shlex(tmp)}', timeout)
        return out
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def shlex(p: Path) -> str:
    return "'" + str(p).replace("'", "'\\''") + "'"


def _tool_job_status(args, ctx) -> str:
    rt = ctx.rt
    if not rt.jobs:
        return "no background jobs"
    ids = [str(args["id"])] if args.get("id") else sorted(rt.jobs)
    lines = []
    for jid in ids:
        job = rt.jobs.get(jid)
        if not job:
            lines.append(f"{jid}: unknown")
            continue
        state = _job_state_text(job)
        extra = f" port={job['port']} url={job.get('url')}" if job.get("port") else ""
        lines.append(f"{jid}: {state} pid={Jobs.job_pid(job)}{extra} "
                     f"cmd={job['cmd'][:100]}")
    return "\n".join(lines)


def _tool_job_output(args, ctx) -> str:
    rt = ctx.rt
    jid = str(args.get("id") or "")
    job = rt.jobs.get(jid)
    if not job:
        return f"ERROR: unknown job {jid!r}"
    tail = max(1, int(args.get("tail") or 100))
    try:
        text = job["log"].read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"ERROR: cannot read log: {exc}"
    lines = text.splitlines()[-tail:]
    state = _job_state_text(job)
    return f"job {jid} ({state}), last {len(lines)} lines:\n" + "\n".join(lines)


def _tool_job_kill(args, ctx) -> str:
    rt = ctx.rt
    jid = str(args.get("id") or "")
    job = rt.jobs.get(jid)
    if not job:
        return f"ERROR: unknown job {jid!r}"
    _kill_group(job["proc"])
    return f"killed job {jid}"


def register(reg) -> None:
    reg.register(Tool(
        "bash", "Run a shell command (bash -c) in the shared working directory. "
                "Returns merged stdout+stderr. Timeout kills the whole process group. "
                "Never run a server in the foreground; background=true returns a "
                "job id for long-running servers/builds.",
        {"type": "object", "properties": {
            "cmd": {"type": "string"},
            "timeout": {"type": "integer", "description": "seconds (default 120)"},
            "background": {"type": "boolean"},
            "cwd": {"type": "string", "description": "relative to the workdir"},
            "stdin": {"type": "string"}},
         "required": ["cmd"]},
        _tool_bash))
    reg.register(Tool(
        "python", "Run a Python snippet in a subprocess (fresh interpreter, workdir cwd).",
        {"type": "object", "properties": {
            "code": {"type": "string"},
            "timeout": {"type": "integer"}},
         "required": ["code"]},
        _tool_python))
    reg.register(Tool(
        "job_status", "List background jobs with pid and state, or one job by id.",
        {"type": "object", "properties": {"id": {"type": "string"}}},
        _tool_job_status))
    reg.register(Tool(
        "job_output", "Last N lines of a background job's log.",
        {"type": "object", "properties": {"id": {"type": "string"},
                                          "tail": {"type": "integer"}},
         "required": ["id"]},
        _tool_job_output))
    reg.register(Tool(
        "job_kill", "Kill a background job's process group.",
        {"type": "object", "properties": {"id": {"type": "string"}},
         "required": ["id"]},
        _tool_job_kill))
