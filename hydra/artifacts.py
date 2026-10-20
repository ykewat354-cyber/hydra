"""Honest file-change accounting for a run (WO3 A1).

The old ``recent_writes`` walked ``$HOME`` with a 5,000-entry budget and
returned a plain list even when the budget ran out.  A shell write deep in a
project could therefore be missed *and* reported as "no files changed".

Contract
--------
``files_changed`` is a list only when the scan completed.  A truncated or
timed-out scan reports ``None`` (JSON null; the UI prints "not tracked").
The report also carries ``files_changed_method`` (tool | scan | mixed) and
``scan_complete``.

Roots are the run cwd, its git root, and the project directories referenced by
tool calls (``write_file``/``edit_file``/``apply_patch`` plus absolute or ``~``
paths in ``bash``).  ``$HOME`` itself is never scanned: that is what made a
20k-file home directory hide the real change.  Files explicitly named by tools
are always included when the scan completes.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import config as C
from .project import _SENSITIVE_NAME, _WRITE_EXTS, _skip_name

_PRUNE_NAMES = (
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".cache", ".npm", ".local", ".mypy_cache", ".pytest_cache", "target",
    "vendor", "site-packages", "dist", "build", ".next", ".nuxt",
    ".svelte-kit", ".tox", ".gradle", ".idea", ".vscode", "spill",
    ".Trash", "Android", "storage", "usr", "proc", "sys",
)

_ABS_PATH = re.compile(r"(?:(?<=^)|(?<=[\s'\"=:;,(`]))(~?/[^\s'\"`;|&<>()]+)")
_REL_REDIRECT = re.compile(
    r"(?:>>?|\btee(?:\s+-a)?|\bcp\s+[^\s;|&]+\s+|\bmv\s+[^\s;|&]+\s+)"
    r"([^\s'\"`;|&<>()]+)")
_QUOTED_FILE = re.compile(
    r"['\"]([^'\"\n]{1,240}?\.(?:html?|css|m?js|cjs|jsx|tsx?|json|md|txt|sh|"
    r"toml|ya?ml|xml|svg|py|rs|go|java|c|h|cpp|sql))['\"]", re.I)
_PATCH_FILE = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (.+)$", re.M)
_SYSTEM_PREFIXES = ("/dev/", "/proc/", "/sys/", "/system/", "/vendor/",
                    "/apex/", "/data/adb/")


def _shell_path(raw: str) -> bool:
    """Reject URLs and device nodes; keep write-looking files and dir roots."""
    s = str(raw or "").strip().strip("'\"`")
    if not s or s.startswith("//") or "://" in s:
        return False
    if any(s.startswith(x) for x in _SYSTEM_PREFIXES):
        return False
    try:
        p = Path(s).expanduser()
        if p.suffix.lower() in _WRITE_EXTS:
            return True
        return p.is_dir() and s not in ("/", "~", "/tmp")
    except Exception:
        return False


def _expand(raw: str, cwd: Path) -> Path | None:
    s = str(raw or "").strip().strip("'\"`")
    if not s or s in (".", "..") or s.startswith(("-", "$")):
        return None
    try:
        p = Path(s).expanduser()
        if not p.is_absolute():
            p = cwd / p
        return p.resolve()
    except Exception:
        return None


def _looks_like_write(path: Path) -> bool:
    return path.suffix.lower() in _WRITE_EXTS


def _safe_component(name: str) -> bool:
    return bool(name) and not _skip_name(name) \
        and not _SENSITIVE_NAME.search(name)


def paths_from_call(name: str, args: dict) -> list:
    """Concrete paths a tool call may have written (files or project roots)."""
    args = args or {}
    out: list = []
    name = str(name or "")
    if name in ("write_file", "edit_file", "apply_patch"):
        if name == "apply_patch":
            out.extend(_PATCH_FILE.findall(str(args.get("patch") or "")))
        elif args.get("path"):
            out.append(str(args["path"]))
    elif name == "bash":
        cmd = str(args.get("cmd") or "")
        for rx in (_ABS_PATH, _REL_REDIRECT, _QUOTED_FILE):
            out.extend(m.group(1) for m in rx.finditer(cmd)
                       if _shell_path(m.group(1)))
    elif name in ("http", "web_fetch", "web_search"):
        return []
    return out


def _root_for(path: Path, cwd: Path) -> Path | None:
    """Turn a referenced file/dir into a directory worth scanning."""
    try:
        p = path if path.is_dir() else path.parent
    except Exception:
        p = path.parent
    # A file that does not exist yet: walk up to the nearest existing dir.
    hops = 0
    while not p.is_dir() and hops < 4:
        p = p.parent
        hops += 1
    try:
        p = p.resolve()
    except Exception:
        return None
    if not p.is_dir():
        return None
    try:
        if p == Path.home().resolve():
            return None          # never scan $HOME: that is the old bug
        if p == Path("/"):
            return None
    except Exception:
        pass
    return p


def _marker_for(since: float) -> Path:
    fd, name = tempfile.mkstemp(prefix="hydra-marker-")
    os.close(fd)
    p = Path(name)
    try:
        os.utime(p, (since - 1.0, since - 1.0))
    except Exception:
        pass
    return p


def _git_root(cwd: Path) -> Path | None:
    try:
        p = cwd.resolve()
    except Exception:
        p = cwd
    for _ in range(8):
        if (p / ".git").exists():
            return p
        if p.parent == p:
            break
        p = p.parent
    return None


def _display(path: Path, cwd: Path) -> str:
    try:
        return str(path.resolve().relative_to(cwd.resolve()))
    except Exception:
        return str(path)


def _accept(path: Path) -> bool:
    try:
        parts = path.parts
    except Exception:
        return False
    if path.suffix.lower() not in _WRITE_EXTS:
        return False
    if any(not _safe_component(x) for x in parts if x not in ("/", "")):
        return False
    return True


def _scan_with_find(root: Path, marker: Path, deadline: float,
                    max_entries: int) -> tuple[list, bool, str]:
    exe = shutil.which("find")
    if not exe:
        return [], False, "no-find"
    cmd = [exe, str(root), "-xdev", "("]
    for i, name in enumerate(_PRUNE_NAMES):
        if i:
            cmd.append("-o")
        cmd += ["-name", name]
    cmd += [")", "-prune", "-o", "-type", "f", "-newer", str(marker),
            "-print0"]
    left = deadline - time.monotonic()
    if left <= 0:
        return [], False, "budget"
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=left)
    except subprocess.TimeoutExpired:
        return [], False, "timeout"
    except Exception:
        return [], False, "find-error"
    raw = proc.stdout or b""
    parts = [x for x in raw.split(b"\0") if x]
    if len(parts) > max_entries:
        return [], False, "truncated"
    out = []
    for chunk in parts:
        try:
            p = Path(chunk.decode("utf-8", "replace"))
        except Exception:
            continue
        if _accept(p):
            out.append(p)
    return out, True, ""


def _scan_with_scandir(root: Path, since: float, deadline: float,
                       max_entries: int) -> tuple[list, bool, str]:
    out: list = []
    seen = 0
    stack = [(root, 0)]
    while stack:
        if time.monotonic() > deadline:
            return out, False, "budget"
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except Exception:
            continue
        for e in entries:
            seen += 1
            if seen > max_entries:
                return out, False, "truncated"
            try:
                if e.is_dir(follow_symlinks=False):
                    if depth < 6 and e.name not in _PRUNE_NAMES:
                        stack.append((Path(e.path), depth + 1))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                p = Path(e.path)
                if not _accept(p):
                    continue
                if float(e.stat(follow_symlinks=False).st_mtime) < since:
                    continue
                out.append(p)
            except Exception:
                continue
    return out, True, ""


def scan_writes(cwd, since: float, *, marker=None, roots=None, cfg=None,
                budget_ms=None, max_entries=None) -> dict:
    """Scan the run's project roots.  Never claims completeness on a timeout."""
    cwd = Path(cwd)
    try:
        cwd = cwd.resolve()
    except Exception:
        pass
    if budget_ms is None:
        budget_ms = C.get(cfg, "report.scan_budget_ms", 1000)
    if max_entries is None:
        max_entries = C.get(cfg, "report.scan_max_entries", 50000)
    try:
        budget_ms = max(1, int(budget_ms))
    except Exception:
        budget_ms = 1000
    try:
        max_entries = max(1, int(max_entries))
    except Exception:
        max_entries = 50000
    deadline = time.monotonic() + budget_ms / 1000.0
    start = time.monotonic()
    marker_path = Path(marker) if marker else _marker_for(float(since or 0))
    cleanup = marker is None
    root_list: list = []
    if cwd.is_dir() and cwd != Path.home().resolve():
        root_list.append(cwd)
    git = _git_root(cwd)
    if git is not None:
        root_list.append(git)
    for r in roots or []:
        try:
            p = Path(r).expanduser()
            if not p.is_absolute():
                p = cwd / p
            if p.is_dir():
                root_list.append(p.resolve())
        except Exception:
            continue
    dedup, seen_roots = [], set()
    for r in root_list:
        try:
            if r in seen_roots or r == Path.home().resolve():
                continue
            seen_roots.add(r)
            dedup.append(r)
        except Exception:
            continue
    files: list = []
    complete = True
    reason = ""
    for root in dedup:
        got, ok, why = _scan_with_find(root, marker_path, deadline, max_entries)
        if why == "no-find":
            got, ok, why = _scan_with_scandir(root, float(since or 0),
                                             deadline, max_entries)
        files.extend(got)
        if not ok:
            complete = False
            reason = why or "truncated"
        if time.monotonic() > deadline:
            complete = False
            reason = reason or "budget"
            break
    if cleanup:
        try:
            marker_path.unlink()
        except Exception:
            pass
    files = list(dict.fromkeys(files))
    files.sort(key=lambda p: (-float(p.stat().st_mtime) if p.exists() else 0,
                              str(p)))
    return {"files": [_display(p, cwd) for p in files],
            "complete": bool(complete), "reason": reason,
            "roots": [str(r) for r in dedup],
            "scanned": len(files),
            "ms": int((time.monotonic() - start) * 1000)}


def collect(agent, explicit=None) -> dict:
    """Return files_changed + method + scan_complete for one agent."""
    cwd = Path(getattr(agent, "workdir", os.getcwd()))
    seen: list = []
    roots: list = []
    for raw in (explicit or []):
        p = _expand(raw, cwd)
        if p is None:
            continue
        r = _root_for(p, cwd)
        if r is not None:
            roots.append(r)
        if p.is_dir():
            continue
        seen.append(str(raw))
    for raw in (agent.data.get("files_written") or []):
        p = _expand(raw, cwd)
        if p is None:
            continue
        r = _root_for(p, cwd)
        if r is not None:
            roots.append(r)
        if p.is_dir():
            continue
        if str(raw) not in seen:
            seen.append(str(raw))
    # Shell writes are only visible in the assistant's tool calls.
    for msg in getattr(agent, "messages", []) or []:
        for tc in (msg.get("tool_calls") or []):
            fn = tc.get("function") or {}
            try:
                args = fn.get("arguments") or {}
                if isinstance(args, str):
                    import json as _json
                    args = _json.loads(args)
            except Exception:
                args = {}
            for raw in paths_from_call(str(fn.get("name") or ""), args):
                p = _expand(raw, cwd)
                if p is None:
                    continue
                r = _root_for(p, cwd)
                if r is not None:
                    roots.append(r)
                if p.is_dir() or (not p.exists() and not p.suffix):
                    continue
                if str(raw) not in seen:
                    seen.append(str(raw))
    child_null = bool(agent.data.get("child_files_null"))
    child_files = [str(x) for x in (agent.data.get("child_files") or [])]
    marker = agent.data.get("files_marker")
    since = float(getattr(agent, "started_at", 0) or 0)
    scan = scan_writes(cwd, since, marker=marker, roots=roots,
                       cfg=getattr(agent, "cfg", None))
    complete = bool(scan["complete"]) and not child_null
    method_parts = []
    if seen or child_files:
        method_parts.append("tool")
    if scan["roots"]:
        method_parts.append("scan")
    method = "mixed" if len(method_parts) > 1 else (method_parts[0] if method_parts else "scan")
    if not complete:
        return {"files": None, "method": method, "complete": False,
                "reason": scan.get("reason") or ("child-not-tracked" if child_null else ""),
                "scan": scan}
    files = []
    for raw in seen + child_files + list(scan["files"]):
        if raw and raw not in files:
            files.append(raw)
    return {"files": files, "method": method, "complete": True,
            "reason": "", "scan": scan}


_WRITE_CMD = re.compile(
    r"(?:(?:^|[^&])>>?\s*(?!&|/dev/null)\S"
    r"|\btee\b|\bcp\b|\bmv\b|\bsed\s+-i\b|\brm\b|\btouch\b"
    r"|open\([^)]*['\"][wa])")


def is_write_call(name: str, args: dict) -> bool:
    """True when a tool call can change files (used for evidence ordering)."""
    name = str(name or "")
    if name in ("write_file", "edit_file", "apply_patch"):
        return True
    if name != "bash":
        return False
    cmd = str((args or {}).get("cmd") or "")
    return bool(_WRITE_CMD.search(cmd))
