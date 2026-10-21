"""`hydra commit` / TUI `/commit`: stage, guard secrets, commit, show hash.

The guard is deliberately conservative: it scans the diff that would be
committed plus every untracked file that `git add -A` would stage.  It never
prints a matched value, only ``pattern`` and ``file:line``.  No model call and
no approval gate is involved.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config as C
from . import journal as J

# Keep in sync with tools/scan_secrets.py.  The duplication is intentional:
# the CLI must work when only the installed package is present.
SECRET_PATTERNS = {
    "github_token": r"gh[pousr]_[A-Za-z0-9]{36,}",
    "github_pat": r"github_pat_[A-Za-z0-9_]{50,}",
    "telegram_bot": r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b",
    "google_api": r"AIza[0-9A-Za-z_\-]{35}",
    "aws_key": r"AKIA[0-9A-Z]{16}",
    "slack_token": r"xox[baprs]-[0-9A-Za-z-]{10,}",
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "generic_assign": (r"(?i)(api[_-]?key|secret|token|passwd|password)"
                       r"\s*[:=]\s*['\"][A-Za-z0-9_\-]{20,}['\"]"),
}

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", ".hydra"}
_MAX_FILE = 4 * 1024 * 1024
_ACTIVE_KEYS: list = []


def _git(cwd, *args, check=False) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=str(cwd), text=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          timeout=60, check=check)


def _is_repo(cwd) -> bool:
    try:
        p = _git(cwd, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0 and (p.stdout or "").strip() == "true"


def _status(cwd) -> list:
    """[(xy, path)] from porcelain -z, excluding .hydra/."""
    p = _git(cwd, "status", "--porcelain=v1", "-z")
    if p.returncode != 0:
        return []
    parts = (p.stdout or "").split("\0")
    out = []
    i = 0
    while i < len(parts):
        rec = parts[i]
        i += 1
        if len(rec) < 4:
            continue
        xy, path = rec[:2], rec[3:]
        # Rename/copy records carry the source path in the next NUL field.
        if xy[0] in ("R", "C") and i < len(parts):
            path = parts[i] or path
            i += 1
        path = path.strip()
        if not path or path == ".hydra" or path.startswith(".hydra/"):
            continue
        out.append((xy, path))
    return out


def _text(path: Path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > _MAX_FILE or b"\0" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


def _scan_text(text: str, label: str, hits: list, start_line: int = 1) -> None:
    if not text:
        return
    for name, pattern in SECRET_PATTERNS.items():
        try:
            for match in re.finditer(pattern, text):
                line = start_line + text.count("\n", 0, match.start())
                hits.append((name, label, line))
        except re.error:
            continue


def _scan_diff(diff: str, hits: list) -> None:
    current = ""
    new_line = 0
    for raw in (diff or "").splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current = path[2:] if path.startswith("b/") else path
            continue
        if raw.startswith("--- "):
            continue
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if match:
            new_line = int(match.group(1))
            continue
        if not current or not raw:
            continue
        if raw.startswith("+"):
            _scan_text(raw[1:], current, hits, new_line)
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            new_line += 1


def _active_keys(cfg) -> list:
    values = []
    for provider in [None] + sorted(C.provider_names(cfg)):
        try:
            key = C.load_key(cfg, provider)
        except Exception:
            key = None
        if key and len(key) >= 8 and key not in values:
            values.append(key)
    return values


def _scan_active(text: str, label: str, hits: list, start_line: int = 1) -> None:
    for key in _ACTIVE_KEYS:
        if key and key in (text or ""):
            line = start_line + (text or "").count("\n", 0, text.index(key))
            hits.append(("active_api_key", label, line))


def _scan_diff_active(diff: str, hits: list) -> None:
    current = ""
    new_line = 0
    for raw in (diff or "").splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current = path[2:] if path.startswith("b/") else path
            continue
        match = re.match(r"^@@ -\d+(?:,\d+)? \+(\d+)", raw)
        if match:
            new_line = int(match.group(1))
            continue
        if not current or not raw:
            continue
        if raw.startswith("+"):
            _scan_active(raw[1:], current, hits, new_line)
            new_line += 1
        elif raw.startswith("-"):
            continue
        else:
            new_line += 1


def scan_commit(cfg, cwd) -> list:
    """Return secret hits for the pending commit as (name, file, line)."""
    global _ACTIVE_KEYS
    _ACTIVE_KEYS = _active_keys(cfg)
    hits = []
    head = _git(cwd, "rev-parse", "--verify", "HEAD")
    if head.returncode == 0:
        diff = _git(cwd, "diff", "--no-color", "--no-ext-diff", "HEAD", "--")
    else:
        # No first commit yet: staged + unstaged content, excluding untracked
        # (those are scanned as files below).
        a = _git(cwd, "diff", "--cached", "--no-color", "--no-ext-diff", "--")
        b = _git(cwd, "diff", "--no-color", "--no-ext-diff", "--")
        diff = subprocess.CompletedProcess(
            a.args, a.returncode, (a.stdout or "") + (b.stdout or ""), a.stderr)
    if diff.returncode == 0:
        _scan_diff(diff.stdout or "", hits)
        _scan_diff_active(diff.stdout or "", hits)
    root = Path(cwd)
    for xy, rel in _status(cwd):
        if not xy.startswith("??"):
            continue
        path = root / rel
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        text = _text(path)
        if text is None:
            continue
        _scan_text(text, rel, hits)
        _scan_active(text, rel, hits)
    # Stable de-duplication while preserving first occurrence.
    seen = set()
    out = []
    for hit in hits:
        if hit not in seen:
            seen.add(hit)
            out.append(hit)
    return out


def _clean_line(text: str, limit: int = 72) -> str:
    value = " ".join(str(text or "").split()).strip()
    if len(value) > limit:
        value = value[:limit - 1].rstrip() + "…"
    return value


def _latest_summary(cwd) -> str:
    try:
        raw = str(cwd)
        resolved = str(Path(cwd).resolve())
        rid = J.last_run_in(raw) or J.last_run_in(resolved)
        if not rid:
            return ""
        for ev in reversed(J.read_events(rid)):
            if ev.get("type") == "report":
                rep = ev.get("report") if isinstance(ev.get("report"), dict) else {}
                summary = _clean_line(rep.get("summary") or "")
                if summary:
                    return summary
    except Exception:
        pass
    return ""


def auto_message(cwd, status_rows) -> str:
    summary = _latest_summary(cwd)
    if summary:
        return summary
    n = len(status_rows or [])
    return f"chore: update {n} file{'s' if n != 1 else ''}"


def message_from_args(args) -> str:
    """Parse ``MSG`` / ``-m MSG``; return '' for an auto-generated message."""
    args = list(args or [])
    if not args:
        return ""
    if args[0] in ("-m", "--message"):
        if len(args) < 2:
            raise ValueError("commit message missing after -m")
        return " ".join(args[1:]).strip()
    return " ".join(args).strip()


def run_commit(cfg, cwd, message: str = None):
    """Stage and commit.  Return ``(exit_code, text)``; never raises for git."""
    cwd = Path(cwd)
    if not _is_repo(cwd):
        return 1, "commit failed: not inside a git repository"
    rows = _status(cwd)
    if not rows:
        return 0, "nothing to commit (working tree clean)"

    hits = scan_commit(cfg, cwd)
    if hits:
        lines = [f"commit blocked: secret pattern {name} in {path}:{line}"
                 for name, path, line in hits]
        return 1, "\n".join(lines)

    add = _git(cwd, "add", "-A", "--", ".",
               ":(exclude).hydra", ":(exclude).hydra/**")
    if add.returncode != 0:
        # Older Git versions may reject an exclusion pathspec for an absent
        # path.  Fall back to add-all, then unstage .hydra explicitly.
        add = _git(cwd, "add", "-A", "--", ".")
        if add.returncode == 0:
            _git(cwd, "reset", "-q", "--", ".hydra")
    if add.returncode != 0:
        return 1, "commit failed: git add: " + (add.stderr or "").strip()

    msg = _clean_line(message) if message else auto_message(cwd, rows)
    if not msg:
        msg = "chore: update files"
    commit = _git(cwd, "commit", "-m", msg)
    if commit.returncode != 0:
        return 1, "commit failed: " + ((commit.stderr or commit.stdout or "").strip())
    short = _git(cwd, "rev-parse", "--short", "HEAD")
    show = _git(cwd, "show", "--stat", "--oneline", "HEAD")
    hash_ = (short.stdout or "").strip() or "?"
    stat = (show.stdout or "").strip()
    # `git show --stat --oneline` already starts with "<hash> <subject>"; fall
    # back to the hash card if a platform's git omitted the stat for any reason.
    return 0, stat or f"{hash_} {msg}"
