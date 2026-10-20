"""Boot/shell auto-resume: keep one lightweight supervisor alive.

Termux cannot run code while Android has killed the app.  This module installs
two independent wake-up paths:

* ``~/.termux/boot/hydra-autoresume`` for Termux:Boot (device/boot start)
* a marked block in ``~/.bashrc`` and ``~/.zshrc`` (new Termux session)

Both call ``hydra boot tick``.  Tick starts one daemon; a kernel ``flock`` makes
duplicate shells harmless.  The daemon waits, notices a run whose recorded pid
is gone, and resumes it with the normal Runtime.  It is not a second agent and
it never runs a finished or deliberately stopped session.
"""
from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from . import config as C
from . import journal as J
from . import sessions as S

MARK_BEGIN = "# >>> HYDRA auto-resume (managed) >>>"
MARK_END = "# <<< HYDRA auto-resume (managed) <<<"
BOOT_NAME = "hydra-autoresume"


def _state_dir() -> Path:
    p = C.home() / "boot"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _pid_path() -> Path:
    return _state_dir() / "daemon.pid"


def _lock_path() -> Path:
    return _state_dir() / "daemon.lock"


def log_path() -> Path:
    return C.home() / "daemon.log"


def _hydra_argv() -> list:
    exe = shutil.which("hydra")
    if exe:
        return [exe]
    return [sys.executable, "-m", "hydra"]


def _hydra_shell() -> str:
    exe = shutil.which("hydra")
    if exe:
        return shlex.quote(exe)
    root = str(Path(__file__).resolve().parents[1])
    # `env` keeps this valid after the caller's `exec` (a bare VAR=value
    # prefix would make the shell look for a command named VAR=value).
    return (f"env PYTHONPATH={shlex.quote(root)}${{PYTHONPATH:+:$PYTHONPATH}} "
            f"{shlex.quote(sys.executable)} -m hydra")


def _rotate_log() -> None:
    p = log_path()
    try:
        if p.is_file() and p.stat().st_size > 2_000_000:
            p.replace(p.with_suffix(".log.1"))
    except Exception:
        pass


def _key_ok(cfg: dict = None) -> bool:
    cfg = cfg or C.load_config()
    host = str(C.get(cfg, "api.host", "https://ollama.com") or "")
    if "ollama.com" not in host:
        return True
    return bool(C.load_key(cfg))


# ---------------------------------------------------------------- daemon lock

class DaemonLock:
    """One supervisor per HYDRA_HOME; released by the kernel on process death."""

    def __init__(self):
        self.path = _lock_path()
        self.fh = None
        self.pid = os.getpid()

    def acquire(self) -> bool:
        try:
            import fcntl
        except ImportError:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.path, "a+", encoding="utf-8")
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        fh.seek(0)
        fh.truncate()
        fh.write(f"{self.pid}\n")
        fh.flush()
        self.fh = fh
        try:
            _pid_path().write_text(f"{self.pid}\n", encoding="utf-8")
        except Exception:
            pass
        return True

    def release(self) -> None:
        try:
            if self._owns_pid():
                _pid_path().unlink(missing_ok=True)
        except Exception:
            pass
        fh, self.fh = self.fh, None
        if fh is None:
            return
        try:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            fh.close()
        except Exception:
            pass

    def _owns_pid(self) -> bool:
        try:
            return int(_pid_path().read_text(encoding="utf-8").strip()) == self.pid
        except Exception:
            return False

    def __enter__(self):
        if not self.acquire():
            raise RuntimeError("daemon already running")
        return self

    def __exit__(self, *exc):
        self.release()


def daemon_pid() -> int | None:
    try:
        pid = int(_pid_path().read_text(encoding="utf-8").strip())
    except Exception:
        return None
    return pid if S.pid_alive(pid) else None


def daemon_alive() -> bool:
    return daemon_pid() is not None


# ---------------------------------------------------------------- lifecycle

def start(cfg: dict = None) -> tuple:
    """Start the supervisor if needed. Returns (started, pid, reason)."""
    if daemon_alive():
        return False, daemon_pid(), "already running"
    if not bool(C.get(cfg or C.load_config(), "resume.auto", True)):
        return False, None, "resume.auto is off"
    if not _key_ok(cfg or C.load_config()):
        return False, None, "no Ollama key"
    _rotate_log()
    log = log_path()
    log.parent.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    root = str(Path(__file__).resolve().parents[1])
    have = env.get("PYTHONPATH", "").split(os.pathsep) if env.get("PYTHONPATH") else []
    if root not in have:
        env["PYTHONPATH"] = os.pathsep.join([root] + have) if have else root
    env["HYDRA_BOOT"] = "1"
    try:
        with open(log, "ab") as fh:
            subprocess.Popen(
                _hydra_argv() + ["daemon", "--resume"],
                stdin=subprocess.DEVNULL, stdout=fh, stderr=subprocess.STDOUT,
                cwd=str(Path.home()), env=env, start_new_session=True,
                close_fds=True)
    except Exception as exc:
        return False, None, f"start failed: {type(exc).__name__}: {exc}"
    deadline = time.time() + 2.0
    while time.time() < deadline:
        pid = daemon_pid()
        if pid:
            return True, pid, "started"
        time.sleep(0.05)
    return False, daemon_pid(), "started, waiting for lock"


def stop(timeout: float = 5.0) -> tuple:
    pid = daemon_pid()
    if not pid:
        try:
            _pid_path().unlink(missing_ok=True)
        except Exception:
            pass
        return False, "not running"
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as exc:
        return False, f"signal failed: {exc}"
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        if not S.pid_alive(pid):
            break
        time.sleep(0.1)
    alive = S.pid_alive(pid)
    if not alive:
        try:
            _pid_path().unlink(missing_ok=True)
        except Exception:
            pass
    return (not alive), ("stopped" if not alive else f"still busy (pid {pid})")


def tick() -> bool:
    """Cheap hook for shell startup. Never prints; safe to call repeatedly."""
    cfg = C.load_config()
    if not bool(C.get(cfg, "resume.auto", True)):
        return False
    if daemon_alive():
        return False
    return bool(start(cfg)[0])


# ---------------------------------------------------------------- install

def _boot_script() -> str:
    return f"""#!{C.shell_path()}
# Managed by `hydra boot install`. Safe to delete with `hydra boot uninstall`.
# Termux:Boot may not source the login profile; make its PATH explicit so the
# `hydra` wrapper can find `python` even before a shell has been opened.
PREFIX="${{PREFIX:-/data/data/com.termux/files/usr}}"
export PATH="$PREFIX/bin:$HOME/bin:$PATH"
cd "$HOME" 2>/dev/null || true
mkdir -p "${{HYDRA_HOME:-$HOME/.hydra}}" 2>/dev/null || true
exec {_hydra_shell()} daemon --resume >> "${{HYDRA_HOME:-$HOME/.hydra}}/daemon.log" 2>&1
"""


def _hook_block() -> str:
    return (f"{MARK_BEGIN}\n"
            "if command -v hydra >/dev/null 2>&1; then\n"
            "  hydra boot tick >/dev/null 2>&1 || true\n"
            "fi\n"
            f"{MARK_END}\n")


def _replace_block(text: str) -> str:
    block = _hook_block()
    if MARK_BEGIN in text and MARK_END in text:
        before, _, rest = text.partition(MARK_BEGIN)
        _, _, after = rest.partition(MARK_END)
        return before.rstrip("\n") + ("\n" if before.strip() else "") + block + \
            after.lstrip("\n")
    text = text.rstrip("\n")
    return (text + "\n\n" if text else "") + block


def _rc_paths(home: Path) -> list:
    return [home / ".bashrc", home / ".zshrc"]


def install(home: Path = None) -> dict:
    home = Path(home or Path.home()).expanduser()
    out = {"boot": False, "rc": [], "errors": []}
    boot_dir = home / ".termux" / "boot"
    try:
        boot_dir.mkdir(parents=True, exist_ok=True)
        script = boot_dir / BOOT_NAME
        script.write_text(_boot_script(), encoding="utf-8")
        script.chmod(0o700)
        out["boot"] = True
    except Exception as exc:
        out["errors"].append(f"boot script: {exc}")
    for path in _rc_paths(home):
        try:
            old = path.read_text(encoding="utf-8") if path.is_file() else ""
            new = _replace_block(old)
            if new != old:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new, encoding="utf-8")
                try:
                    path.chmod(0o600)
                except Exception:
                    pass
            out["rc"].append(str(path))
        except Exception as exc:
            out["errors"].append(f"{path}: {exc}")
    return out


def uninstall(home: Path = None, stop_daemon: bool = True) -> dict:
    home = Path(home or Path.home()).expanduser()
    out = {"boot": False, "rc": [], "daemon": "", "errors": []}
    if stop_daemon:
        _, out["daemon"] = stop()
    script = home / ".termux" / "boot" / BOOT_NAME
    try:
        script.unlink(missing_ok=True)
        out["boot"] = True
    except Exception as exc:
        out["errors"].append(f"boot script: {exc}")
    for path in _rc_paths(home):
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8")
            if MARK_BEGIN in text and MARK_END in text:
                before, _, rest = text.partition(MARK_BEGIN)
                _, _, after = rest.partition(MARK_END)
                text = before.rstrip("\n") + ("\n" if before.strip() else "") + \
                    after.lstrip("\n")
                path.write_text(text.rstrip("\n") + "\n", encoding="utf-8")
            out["rc"].append(str(path))
        except Exception as exc:
            out["errors"].append(f"{path}: {exc}")
    return out


def _has_hook(path: Path) -> bool:
    try:
        if not path.is_file():
            return False
        text = path.read_text(encoding="utf-8", errors="replace")
        return MARK_BEGIN in text and MARK_END in text
    except Exception:
        return False


def status(cfg: dict = None) -> dict:
    cfg = cfg or C.load_config()
    interrupted = [r for r in S.session_rows(limit=200, with_activity=False,
                                             with_cost=False)
                   if r["state"] == "interrupted"]
    return {
        "auto": bool(C.get(cfg, "resume.auto", True)),
        "daemon": daemon_pid(),
        "log": str(log_path()),
        "boot_script": (Path.home() / ".termux" / "boot" / BOOT_NAME).is_file(),
        "rc": [str(p) for p in _rc_paths(Path.home()) if _has_hook(p)],
        "interrupted": interrupted[:8],
    }


def cli(cfg: dict, args: list) -> int:
    sub = str(args[0] if args else "status").lower()
    if sub in ("install", "on"):
        out = install()
        started = False
        if "--no-start" not in args:
            started, pid, reason = start(cfg)
            out["daemon"] = (started, pid, reason)
        print("auto-resume installed")
        print(f"  boot script  {'ok' if out['boot'] else 'FAILED'}")
        print(f"  shell hooks  {', '.join(out['rc']) or 'none'}")
        if out.get("daemon"):
            started, pid, reason = out["daemon"]
            print(f"  daemon       {'pid ' + str(pid) if pid else 'not running'} ({reason})")
        print("  note         Termux:Boot must be installed for device-boot start;")
        print("               opening Termux also starts it from .bashrc/.zshrc")
        for err in out["errors"]:
            print(f"  error        {err}")
        return 0 if not out["errors"] else 1
    if sub in ("uninstall", "off"):
        out = uninstall()
        print("auto-resume uninstalled")
        if out["daemon"]:
            print(f"  daemon       {out['daemon']}")
        for err in out["errors"]:
            print(f"  error        {err}")
        return 0 if not out["errors"] else 1
    if sub in ("tick", "start", "run"):
        started, pid, reason = start(cfg)
        if started:
            print(f"auto-resume supervisor started (pid {pid})")
        elif pid:
            print(f"auto-resume supervisor already running (pid {pid})")
        else:
            print(f"auto-resume not started: {reason}")
        return 0
    if sub == "stop":
        ok, message = stop()
        print(f"auto-resume supervisor {message}")
        return 0 if ok or "not running" in message else 1
    if sub == "status":
        st = status(cfg)
        print(f"auto-resume  {'on' if st['auto'] else 'off'}")
        print(f"daemon       {('pid ' + str(st['daemon'])) if st['daemon'] else 'stopped'}")
        print(f"boot script  {'installed' if st['boot_script'] else 'missing'}")
        print(f"shell hooks  {', '.join(Path(p).name for p in st['rc']) or 'missing'}")
        print(f"log          {st['log']}")
        if st["interrupted"]:
            print("interrupted sessions:")
            for row in st["interrupted"]:
                print(f"  {row['run']:<16} {row['title']}")
        else:
            print("interrupted  none")
        return 0
    print("usage: hydra boot [status|install|uninstall|tick|start|stop]")
    return 2
