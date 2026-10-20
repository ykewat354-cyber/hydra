"""F12 — ``hydra update``: fast-forward, changelog, offline tests, rollback.

The install directory is this package's parent (override with
``HYDRA_INSTALL_DIR``).  ``git pull --ff-only`` is the only network operation;
the test command is injected so the fixture suite can stay fully offline.
"""
from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path


def install_dir() -> Path:
    raw = os.environ.get("HYDRA_INSTALL_DIR") or \
        str(Path(__file__).resolve().parent.parent)
    return Path(raw).expanduser().resolve()


def _git(root: Path, *args, timeout=120):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                          text=True, timeout=timeout)


def _default_tests() -> list:
    return [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-q"]


def _tail(text: str, lines: int = 20) -> str:
    rows = [r for r in str(text or "").splitlines() if r.strip()]
    return "\n".join(rows[-lines:])


def _tag_at(root: Path, sha: str) -> str:
    """P5: the most specific tag describing *sha* ('' when none)."""
    for args in (["describe", "--tags", "--exact-match", sha],
                 ["describe", "--tags", sha]):
        res = _git(root, *args)
        if res.returncode == 0:
            return (res.stdout or "").strip()
    return ""


def perform_update(root=None, test_cmd=None, out=print) -> int:
    """Pull, show old..new, test, and hard-reset on test failure."""
    root = Path(root or install_dir()).expanduser().resolve()
    if not (root / ".git").exists():
        out(f"hydra update: {root} is not a git checkout")
        return 2
    old = _git(root, "rev-parse", "HEAD")
    if old.returncode != 0:
        out("hydra update: cannot read the current commit")
        return 1
    old_sha = old.stdout.strip()

    pull = _git(root, "pull", "--ff-only", timeout=300)
    if pull.returncode != 0:
        detail = _tail((pull.stderr or "") + "\n" + (pull.stdout or ""), 8)
        out("hydra update: git pull --ff-only failed; nothing changed")
        if detail:
            out(detail)
        return 1

    new = _git(root, "rev-parse", "HEAD")
    if new.returncode != 0:
        out("hydra update: cannot read the new commit")
        return 1
    new_sha = new.stdout.strip()
    if old_sha == new_sha:
        out(f"already up to date ({new_sha[:12]})")
        out(f"tag: {_tag_at(root, new_sha) or '(none)'}")
        return 0

    log = _git(root, "log", "--oneline", "--no-decorate",
               f"{old_sha}..{new_sha}")
    out(f"changelog since {old_sha[:12]}:")
    out((log.stdout or "(no commits)").strip())

    cmd = test_cmd if test_cmd is not None else _default_tests()
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)
    out("running offline tests…")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(root) + (
        os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    try:
        tests = subprocess.run(list(cmd), cwd=str(root), capture_output=True,
                               text=True, timeout=1800, env=env)
    except Exception as exc:
        tests = None
        out(f"test command failed to start: {type(exc).__name__}: {exc}")
    if tests is None or tests.returncode != 0:
        if tests is not None:
            out(_tail((tests.stdout or "") + "\n" + (tests.stderr or "")))
        rollback = _git(root, "reset", "--hard", old_sha)
        if rollback.returncode == 0:
            out(f"tests failed — rolled back to {old_sha[:12]}")
        else:
            out(f"tests failed and ROLLBACK FAILED; restore {old_sha} manually")
        return 1

    out(f"tests passed — updated {old_sha[:12]} -> {new_sha[:12]}")
    out(f"tag: {_tag_at(root, new_sha) or '(none)'}")
    return 0


def cli(cfg=None, args=None) -> int:
    args = list(args or [])
    if args:
        print("usage: hydra update")
        return 2
    return perform_update()
