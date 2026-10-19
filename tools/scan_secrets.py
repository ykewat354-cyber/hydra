#!/usr/bin/env python3
"""Secret scanner (work order T02).

Prints only ``file:line:pattern-name`` (or ``blob:path:pattern-name`` for git
history). The matched text is never printed, logged or stored. Stdlib only.

Usage:
    python tools/scan_secrets.py [ROOT]            # working tree only
    python tools/scan_secrets.py [ROOT] --history  # tree + every blob in history
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

PATTERNS = {
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

_SKIP_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv"}
_MAX_FILE = 4 * 1024 * 1024
_ALLOWLIST = Path(__file__).with_name("secret_allowlist.txt")


def _load_allowlist() -> set[str]:
    """Exact location strings that are known test fixtures (documented)."""
    if not _ALLOWLIST.exists():
        return set()
    return {
        line.strip()
        for line in _ALLOWLIST.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    }


def _read_text(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > _MAX_FILE or b"\x00" in data[:4096]:
        return None
    return data.decode("utf-8", "replace")


def scan_tree(root: Path) -> list[str]:
    hits: list[str] = []
    for path in sorted(root.rglob("*")):
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        if not path.is_file():
            continue
        text = _read_text(path)
        if text is None:
            continue
        rel = path if path.is_absolute() else Path(root.name) / path
        try:
            rel = path.relative_to(root.parent)
        except ValueError:
            rel = path
        for name, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text):
                line = text.count("\n", 0, match.start()) + 1
                hits.append(f"{rel}:{line}:{name}")
    return hits


def scan_history(root: Path) -> list[str]:
    """Scan every unique blob in every reachable commit."""
    try:
        listing = subprocess.run(
            ["git", "rev-list", "--objects", "--all"],
            cwd=root, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return ["(git history unavailable)"]
    blobs: dict[str, str] = {}
    for line in listing.splitlines():
        sha, _, path = line.partition(" ")
        if len(sha) == 40 and path:
            blobs.setdefault(sha, path)
    hits: set[str] = set()
    for sha, path in blobs.items():
        try:
            blob = subprocess.run(
                ["git", "cat-file", "blob", sha],
                cwd=root, capture_output=True, check=True,
            ).stdout
        except (OSError, subprocess.CalledProcessError):
            continue
        if b"\x00" in blob[:4096]:
            continue
        text = blob.decode("utf-8", "replace")
        for name, pattern in PATTERNS.items():
            if re.search(pattern, text):
                hits.add(f"blob {sha[:10]}:{path}:{name}")
    return sorted(hits)


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("--")]
    root = Path(args[0]).resolve() if args else Path.cwd()
    hits = scan_tree(root)
    if "--history" in argv:
        hits += scan_history(root)
    allow = _load_allowlist()
    suppressed = [h for h in hits if h in allow]
    hits = [h for h in hits if h not in allow]
    if hits:
        print("# SECRET PATTERN HITS (locations only, no values)")
        for hit in hits:
            print(hit)
        print(f"# {len(hits)} hit(s)")
        return 1
    note = (f" ({len(suppressed)} documented test-fixture location(s) "
            "suppressed)" if suppressed else "")
    print(f"clean: no secret patterns under {root}"
          + (" (tree + history)" if "--history" in argv else " (tree)") + note)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
