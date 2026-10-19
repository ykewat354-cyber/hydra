"""F6 — capability auto-detect, cached for 24 h in ``~/.hydra/caps.json``.

Detected at startup (Runtime __init__) so schemas never advertise a dead tool:

* ``web_search`` / ``web_fetch`` — hosted endpoint answers (a hosted 404 for
  web_fetch is fine: the stdlib raw fallback is what serves pages, T09);
* ``vision`` — one of the configured vision-role models appears in ``/api/tags``;
* ``termux_api`` — ``termux-battery-status`` answers within the timeout;
* ``rg`` / ``git`` / ``tmux`` — binaries on $PATH.

Fail open: anything that looks like *detection* trouble (transport reset,
unparseable tags, exception) leaves the capability available, so a phone in a
tunnel never loses half its tools.  ``hydra doctor --caps`` forces a re-check.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config as C

TTL_SECS = 24 * 3600
TERMUX_MEMO_SECS = 300.0

CAP_KEYS = ("web_search", "web_fetch", "vision", "termux_api", "rg", "git", "tmux")

ALL_AVAILABLE = {
    "web_search": True,
    "web_fetch": True,
    "vision": True,
    "vision_model": None,
    "termux_api": True,
    "rg": True,
    "git": True,
    "tmux": True,
}

_lock = threading.Lock()
_PROCESS_CACHE: dict = {}     # home str -> (caps, monotonic ts)
_TERMUX_MEMO: dict = {}       # "v" -> (monotonic ts, ok)

# HTTP statuses that mean "the endpoint is not there for this key".
_DEAD_STATUSES = (401, 402, 403, 404, 405, 501)


def caps_path() -> Path:
    return C.home() / "caps.json"


def load_cached() -> dict:
    """The on-disk cache, or {} when missing/corrupt."""
    try:
        data = json.loads(caps_path().read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save(caps: dict) -> None:
    """Atomic write; never raises into startup."""
    path = caps_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(caps, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except Exception:
        pass


def fresh(caps: dict, ttl: float = TTL_SECS) -> bool:
    try:
        checked = float(caps.get("checked_at") or 0)
    except (TypeError, ValueError):
        return False
    return checked > 0 and (time.time() - checked) < float(ttl)


def fail_open() -> dict:
    caps = dict(ALL_AVAILABLE)
    caps["checked_at"] = time.time()
    return caps


# ---------------------------------------------------------------- probes

def termux_ok(timeout: float = 3.0) -> bool:
    """True when termux-battery-status exists and answers (memoized 5 min)."""
    now = time.monotonic()
    memo = _TERMUX_MEMO.get("v")
    if memo and (now - memo[0]) < TERMUX_MEMO_SECS:
        return bool(memo[1])
    ok = False
    if shutil.which("termux-battery-status"):
        try:
            proc = subprocess.run(["termux-battery-status"], capture_output=True,
                                  text=True, timeout=timeout)
            ok = proc.returncode == 0
        except Exception:
            ok = False
    _TERMUX_MEMO["v"] = (now, ok)
    return ok


def _network_reachable(cfg: dict, client) -> bool:
    """Skip hosted probes for the real host when there is no key: fail open."""
    if client is None or not hasattr(client, "web_search"):
        return False
    key = str(getattr(client, "key", "") or "")
    if key:
        return True
    host = str(C.get(cfg, "api.host", "") or "")
    return "ollama.com" not in host


def _probe_web_search(client) -> bool:
    try:
        client.web_search("hydra capability probe", 1)
        return True
    except Exception as exc:              # noqa: BLE001 - fail open on transport
        status = getattr(exc, "status", None)
        if status in _DEAD_STATUSES:
            return False
        return True


def _probe_web_fetch(client) -> bool:
    try:
        client.web_fetch("https://example.com/")
        return True
    except Exception as exc:              # noqa: BLE001
        status = getattr(exc, "status", None)
        if status == 402:
            return False
        # 404 (and every other failure) leaves the raw-http fallback usable.
        return True


def _probe_vision(cfg: dict, client) -> tuple:
    """(available, model) — empty/missing tags fail open."""
    try:
        tags = client.tags()
    except Exception:
        return True, None
    names = set()
    for item in tags or []:
        if isinstance(item, dict):
            names.add(str(item.get("name") or item.get("model") or ""))
        elif item:
            names.add(str(item))
    names.discard("")
    if not names:
        return True, None
    for model in (C.get(cfg, "roles.vision.chain") or []):
        if str(model) in names:
            return True, str(model)
    return False, None


def detect(cfg: dict, client=None, *, which_fn=None, termux_fn=None) -> dict:
    """Run every probe; each probe fails open on its own."""
    which = which_fn or shutil.which
    caps = {
        "checked_at": time.time(),
        "web_search": True,
        "web_fetch": True,
        "vision": True,
        "vision_model": None,
        "termux_api": True,
        "rg": bool(which("rg")),
        "git": bool(which("git")),
        "tmux": bool(which("tmux")),
    }
    if _network_reachable(cfg, client):
        caps["web_search"] = _probe_web_search(client)
        caps["web_fetch"] = _probe_web_fetch(client)
        caps["vision"], caps["vision_model"] = _probe_vision(cfg, client)
    try:
        caps["termux_api"] = bool((termux_fn or termux_ok)())
    except Exception:
        caps["termux_api"] = True       # detection trouble -> fail open
    return caps


def get_caps(cfg: dict, client=None, force: bool = False) -> dict:
    """Fresh caps: disk cache -> per-process cache -> detect + save."""
    if not force:
        cached = load_cached()
        if cached and fresh(cached):
            return cached
        home = str(C.home())
        with _lock:
            ent = _PROCESS_CACHE.get(home)
        if ent and fresh(ent[0]):
            return ent[0]
    try:
        caps = detect(cfg, client)
    except Exception:
        caps = fail_open()
    save(caps)
    with _lock:
        _PROCESS_CACHE[str(C.home())] = (caps, time.monotonic())
    return caps


# ---------------------------------------------------------------- rendering

def unavailable_names(caps: dict) -> list:
    """Human names of everything missing / hidden from the schemas."""
    out = []
    if not caps.get("web_search", True):
        out.append("web_search")
    if not caps.get("web_fetch", True):
        out.append("web_fetch")
    if not caps.get("vision", True):
        out.append("view_image (no vision model)")
    if not caps.get("termux_api", True):
        out.append("termux_* (termux-api)")
    for name in ("rg", "git", "tmux"):
        if not caps.get(name, True):
            out.append(name)
    return out


def env_line(caps: dict) -> str:
    """One line for the env block: what is missing, and that tools were cut."""
    names = unavailable_names(caps)
    if not names:
        return "unavailable: none (all detected tools are present)"
    hidden = []
    if not caps.get("web_search", True):
        hidden.append("web_search")
    if not caps.get("web_fetch", True):
        hidden.append("web_fetch")
    if not caps.get("vision", True):
        hidden.append("view_image")
    if not caps.get("termux_api", True):
        hidden.append("termux_*")
    line = "unavailable: " + ", ".join(names)
    if hidden:
        line += "; removed from your tools: " + ", ".join(hidden)
    return line
