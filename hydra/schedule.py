"""Schedule jobs (§19.8): ~/.hydra/schedule.toml, fired by the daemon timer.

Two when-formats:
  "every 30m" / "every 90s" / "every 2h" / "every 1d"
  "0 9 * * *"  (5-field cron: minute hour dom month dow; * , */n and numbers)

Firing is a normal run named ``sched-<id>-<ts>``; the daemon updates ``last``.
``hydra schedule export`` best-effort hands periodic jobs to termux-job-scheduler
(Android enforces a 15-minute minimum; the daemon timer has no such limit).
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time

from . import config as C

INTERVAL_RE = re.compile(r"^every\s+(\d+)\s*(s|m|h|d)$", re.I)
_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def _path():
    return C.home() / "schedule.toml"


def load_jobs() -> list:
    import tomllib
    p = _path()
    if not p.is_file():
        return []
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return []
    jobs = data.get("job") or []
    return [j for j in jobs if isinstance(j, dict) and j.get("id")]


def save_jobs(jobs: list) -> None:
    lines = []
    for j in jobs:
        lines += ["[[job]]",
                  f"id = {json.dumps(str(j.get('id')))}",
                  f"when = {json.dumps(str(j.get('when', '')))}",
                  f"task = {json.dumps(str(j.get('task', '')))}",
                  f"last = {float(j.get('last') or 0.0)}", ""]
    _path().write_text("\n".join(lines), encoding="utf-8")


def interval_seconds(when: str):
    m = INTERVAL_RE.match(str(when).strip())
    if not m:
        return None
    return int(m.group(1)) * _UNIT[m.group(2).lower()]


def _field_match(field: str, value: int, lo: int, hi: int) -> bool:
    for part in str(field).split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            base, _, s = part.partition("/")
            part = base or "*"
            try:
                step = max(1, int(s))
            except ValueError:
                step = 1
        if part == "*":
            if (value - lo) % step == 0:
                return True
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            try:
                a, b = int(a), int(b)
            except ValueError:
                continue
            if a <= value <= b and (value - a) % step == 0:
                return True
            continue
        try:
            if int(part) == value:
                return True
        except ValueError:
            continue
    return False


def cron_matches(when: str, t: time.struct_time) -> bool:
    parts = str(when).split()
    if len(parts) != 5:
        return False
    minute, hour, dom, month, dow = parts
    return (_field_match(minute, t.tm_min, 0, 59)
            and _field_match(hour, t.tm_hour, 0, 23)
            and _field_match(dom, t.tm_mday, 1, 31)
            and _field_match(month, t.tm_mon, 1, 12)
            and _field_match(dow, (t.tm_wday + 1) % 7, 0, 6))


def valid_when(when: str) -> bool:
    when = str(when or "").strip()
    if interval_seconds(when) is not None:
        return True
    return len(when.split()) == 5


def is_due(job: dict, now: float = None) -> bool:
    now = time.time() if now is None else now
    last = float(job.get("last") or 0.0)
    when = str(job.get("when") or "").strip()
    secs = interval_seconds(when)
    if secs is not None:
        return last == 0.0 or (now - last) >= secs
    t = time.localtime(now)
    if not cron_matches(when, t):
        return False
    minute_start = now - t.tm_sec
    return last < minute_start


def add(when: str, task: str) -> str:
    when, task = str(when or "").strip(), str(task or "").strip()
    if not valid_when(when) or not task:
        raise ValueError("when must be 'every 30m' or a 5-field cron")
    jobs = load_jobs()
    jid = f"j{int(time.time()) % 100000}{len(jobs):02d}"
    jobs.append({"id": jid, "when": when, "task": task, "last": 0.0})
    save_jobs(jobs)
    return jid


def remove(jid: str) -> bool:
    jobs = load_jobs()
    keep = [j for j in jobs if str(j.get("id")) != str(jid)]
    if len(keep) == len(jobs):
        return False
    save_jobs(keep)
    return True


def mark_fired(jid: str) -> None:
    jobs = load_jobs()
    for j in jobs:
        if str(j.get("id")) == str(jid):
            j["last"] = time.time()
    save_jobs(jobs)


# ---------------------------------------------------------------- export

def export_termux() -> str:
    """Hand periodic jobs to termux-job-scheduler (survives without the daemon)."""
    if not shutil.which("termux-job-scheduler"):
        return "termux-job-scheduler not found (pkg install termux-api + Termux:API app)"
    jobs = load_jobs()
    if not jobs:
        return "no schedule entries"
    out = []
    for i, j in enumerate(jobs, start=1):
        secs = interval_seconds(j.get("when", ""))
        period_ms = int(max(900, secs or 900) if secs else 900) * 1000
        script = C.home() / "schedule" / f"{j['id']}.sh"
        script.parent.mkdir(parents=True, exist_ok=True)
        script.write_text(
            "#!/data/data/com.termux/files/usr/bin/bash\n"
            f"exec {C.shell_path()} -lc 'hydra daemon --once'\n",
            encoding="utf-8")
        script.chmod(0o755)
        try:
            subprocess.run(["termux-job-scheduler", "--script", str(script),
                            "--period-ms", str(period_ms), "--persistent", "true",
                            "--job-id", str(i)], capture_output=True, timeout=30)
            out.append(f"{j['id']}: exported (every {period_ms // 60000} min)")
        except Exception as exc:
            out.append(f"{j['id']}: export failed ({exc})")
    return "\n".join(out)
