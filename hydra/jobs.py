"""OS-level liveness for background jobs.

A job's stored state is only a hint: the process may have exited while a
report, verifier, or TUI still holds an old snapshot.  Read ``/proc`` and try
the server port at the moment of the question instead.
"""
from __future__ import annotations

import socket
from pathlib import Path


def pid_alive(pid) -> bool:
    """Whether *pid* exists in /proc and is not a zombie."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        raw = Path(f"/proc/{pid}/stat").read_bytes()
        state = raw.rsplit(b") ", 1)[1].split(b" ", 1)[0]
    except Exception:
        return False
    return state not in (b"Z", b"X", b"x")


def port_open(port, *, host: str = "127.0.0.1", timeout: float = 0.35) -> bool:
    """One bounded TCP connect, not a cached or remembered result."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, TypeError, ValueError):
        return False


def job_pid(job) -> int:
    proc = job.get("proc") if isinstance(job, dict) else None
    try:
        return int(getattr(proc, "pid", 0) or job.get("pid") or 0)
    except (TypeError, ValueError):
        return 0


def job_process_alive(job) -> bool:
    """The PID is in /proc, regardless of whether the port is ready yet."""
    return pid_alive(job_pid(job))


def job_state(job) -> str:
    """``running`` (PID + port), ``starting`` (PID, port not ready), or ``exited``."""
    if not job_process_alive(job):
        return "exited"
    port = job.get("port") if isinstance(job, dict) else None
    if port and not port_open(port):
        return "starting"
    return "running"


def job_serving(job) -> bool:
    return job_state(job) == "running"
