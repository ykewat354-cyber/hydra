"""Shell tools: bash, background jobs, python snippets."""
from __future__ import annotations

import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from . import Tool


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
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
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
        if time.time() >= end and proc.poll() is None:
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
    try:
        rest = os.read(fd, 65536)
        if rest:
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
        return text + f"\n[timeout after {timeout}s — process group killed]"
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
    return run_shell(ctx.rt, cwd, cmd, timeout, stdin_text=args.get("stdin"))


def _background(ctx, cwd: Path, cmd: str) -> str:
    rt = ctx.rt
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
            "agent": ctx.agent.id}
    rt.jobs[jid] = meta
    if rt.journal is not None:
        # Persist enough to re-attach after a crash: the job outlives us
        # (start_new_session) but the in-memory Popen handle does not.
        rt.journal.event("job_start", ctx.agent.id, jid=jid, pid=proc.pid,
                         cmd=cmd, log=str(log), started=meta["started"])
    return f"started job {jid} (pid {proc.pid}); log: {log}"


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
        rc = job["proc"].poll()
        state = "running" if rc is None else f"exited({rc})"
        lines.append(f"{jid}: {state} pid={job['proc'].pid} cmd={job['cmd'][:100]}")
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
    rc = job["proc"].poll()
    state = "running" if rc is None else f"exited({rc})"
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
                "background=true returns a job id for long-running servers/builds.",
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
