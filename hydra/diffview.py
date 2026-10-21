"""Workspace diff helpers for `hydra diff` and the TUI `/diff` card.

The TUI wants a bounded, numbered view; the CLI wants the exact `git diff`
text.  Both use the same git invocation so the two surfaces cannot disagree.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def git_diff(cwd, path: str = "", max_chars: int = 60000):
    """Return ``(text, note)``.

    *text* is the diff (possibly truncated); *note* is a human one-liner when
    there is no diff or git is unavailable.  No exceptions escape: a diff
    command must never break the TUI.
    """
    cwd = Path(cwd or ".")
    if not shutil.which("git"):
        return "", "git not available"
    args = ["git", "-C", str(cwd), "diff", "--no-color", "--no-ext-diff",
            "HEAD", "--"]
    if path:
        args.append(str(path))
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=20)
        if proc.returncode != 0:
            # A repository with no commits yet has no HEAD.  Fall back to the
            # worktree-vs-index diff, which is still useful.
            fallback = ["git", "-C", str(cwd), "diff", "--no-color",
                        "--no-ext-diff", "--"]
            if path:
                fallback.append(str(path))
            proc = subprocess.run(fallback, capture_output=True, text=True,
                                  timeout=20)
            if proc.returncode != 0:
                return "", "not a git repository (or git cannot read it)"
    except Exception as exc:
        return "", f"diff failed: {type(exc).__name__}"
    text = proc.stdout or ""
    if not text.strip():
        return "", "no uncommitted changes"
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n[... diff truncated ...]\n"
    return text, ""


def numbered_rows(text: str, width: int = 80, max_rows: int = 80) -> list:
    """Turn unified diff text into compact ``(row, style)`` TUI rows."""
    rows = []
    old_no = new_no = None
    omitted = 0
    for line in str(text or "").splitlines():
        if line.startswith("@@"):
            # @@ -12,3 +12,4 @@
            try:
                bits = line.split()
                old = bits[1][1:].split(",")[0]
                new = bits[2][1:].split(",")[0]
                old_no, new_no = int(old), int(new)
            except (IndexError, ValueError):
                old_no = new_no = None
            rows.append((line[:max(1, width)], "warn"))
            continue
        if line.startswith("diff --git") or line.startswith("index ") or \
                line.startswith("--- ") or line.startswith("+++ "):
            rows.append((line[:max(1, width)], "dim"))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            if len(rows) >= max_rows:
                omitted += 1
                continue
            num = f"{new_no:>4}" if new_no is not None else "    "
            rows.append((f"{num} +{line[1:]}"[:max(1, width)].rstrip(),
                         "added"))
            if new_no is not None:
                new_no += 1
        elif line.startswith("-") and not line.startswith("---"):
            if len(rows) >= max_rows:
                omitted += 1
                continue
            num = f"{old_no:>4}" if old_no is not None else "    "
            rows.append((f"{num} -{line[1:]}"[:max(1, width)].rstrip(),
                         "removed"))
            if old_no is not None:
                old_no += 1
        else:
            if len(rows) >= max_rows:
                omitted += 1
                continue
            left = f"{old_no:>4}" if old_no is not None else "    "
            right = f"{new_no:>4}" if new_no is not None else "    "
            rows.append((f"{left} {right}  {line}"[:max(1, width)].rstrip(),
                         "dim"))
            if old_no is not None:
                old_no += 1
            if new_no is not None:
                new_no += 1
    if omitted:
        rows.append((f"… {omitted} more diff lines", "dim"))
    return rows
