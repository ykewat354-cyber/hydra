"""Undo via shadow-ref snapshots (§19.9).

Enabled by ``journal.git_snapshots = true`` (default off). At each turn
boundary where the tree changed, HYDRA writes a snapshot commit to
``refs/hydra/<run-id>/<turn>`` using plumbing with a temporary index
(``GIT_INDEX_FILE``, ``git add -A``, ``write-tree``, ``commit-tree``,
``update-ref``). The user's branch, HEAD, index and working tree are never
touched. Untracked files are included; ignored files are not.

``hydra undo`` restores the working tree to the run's baseline snapshot
(turn 0, written before the first model turn) and removes files that did not
exist then; ``--to N`` picks a specific turn instead. It is a user command;
no agent ever waits on it.
"""
from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

PRUNE_DAYS = 14


def _git(root, *args, timeout: float = 30, check: bool = False, env=None):
    return subprocess.run(["git", "-C", str(root)] + list(args),
                          capture_output=True, text=True, timeout=timeout,
                          env=env, check=check)


def is_repo(root) -> bool:
    return (Path(root) / ".git").exists() and shutil.which("git") is not None


def _status_hash(root) -> str:
    p = _git(root, "status", "--porcelain")
    if p.returncode != 0:
        return ""
    return p.stdout


def _ref_name(run_id: str, turn) -> str:
    return f"refs/hydra/{run_id}/{turn}"


def _list_turns(root, run_id: str) -> list:
    """[(turn:int, sha:str)] for a run, sorted by turn."""
    p = _git(root, "for-each-ref", f"refs/hydra/{run_id}/",
             "--format=%(refname) %(objectname)")
    out = []
    if p.returncode != 0:
        return out
    for line in p.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        try:
            turn = int(parts[0].rsplit("/", 1)[1])
        except ValueError:
            continue
        out.append((turn, parts[1]))
    return sorted(out)


def list_runs(root) -> list:
    """run ids that have snapshot refs, with their newest turn."""
    p = _git(root, "for-each-ref", "refs/hydra/", "--format=%(refname)")
    runs: dict = {}
    if p.returncode != 0:
        return []
    for ref in p.stdout.splitlines():
        ref = ref.strip()
        if not ref.startswith("refs/hydra/"):
            continue
        parts = ref.split("/")
        if len(parts) < 4:
            continue
        run_id = parts[2]
        try:
            turn = int(parts[3])
        except ValueError:
            continue
        runs[run_id] = max(runs.get(run_id, -1), turn)
    return sorted(runs.items(), key=lambda kv: kv[1])


def snapshot(root, run_id: str, turn, cfg=None) -> str | None:
    """Write refs/hydra/<run_id>/<turn>; return the commit sha or None."""
    root = Path(root).resolve()
    if not is_repo(root):
        return None
    fd, idx = tempfile.mkstemp(prefix="hydra_gitidx_")
    os.close(fd)
    try:
        os.unlink(idx)  # let git create it: a fresh file confuses add-A
    except Exception:
        pass
    env = dict(os.environ)
    env.update({
        "GIT_INDEX_FILE": idx,
        "GIT_AUTHOR_NAME": env.get("GIT_AUTHOR_NAME", "hydra"),
        "GIT_AUTHOR_EMAIL": env.get("GIT_AUTHOR_EMAIL", "hydra@localhost"),
        "GIT_COMMITTER_NAME": env.get("GIT_COMMITTER_NAME", "hydra"),
        "GIT_COMMITTER_EMAIL": env.get("GIT_COMMITTER_EMAIL", "hydra@localhost"),
    })
    try:
        p = _git(root, "add", "-A", "--", ".", env=env)
        if p.returncode != 0:
            return None
        p = _git(root, "write-tree", env=env)
        if p.returncode != 0:
            return None
        tree = p.stdout.strip()
        parents = _list_turns(root, run_id)
        msg = f"hydra snapshot run={run_id} turn={turn}"
        args = ["commit-tree", tree]
        if parents:
            args += ["-p", parents[-1][1]]
        args += ["-m", msg]
        p = _git(root, *args, env=env)
        if p.returncode != 0:
            return None
        sha = p.stdout.strip()
        p = _git(root, "update-ref", _ref_name(run_id, turn), sha)
        if p.returncode != 0:
            return None
        return sha
    except Exception:
        return None
    finally:
        for path in (idx, idx + ".lock"):
            try:
                os.unlink(path)
            except Exception:
                pass


def turn_snapshot(rt, agent, force: bool = False, turn=None) -> str | None:
    """Called at turn boundaries: snapshot only when the tree changed.

    Only the root (lead) agent snapshots, so there is one series per run.
    ``turn`` overrides the ref number (used for the turn-0 baseline). Never
    raises.
    """
    try:
        if not rt.cfg.get("journal", {}).get("git_snapshots"):
            return None
        if agent is not getattr(rt, "agents", {}).get("a0"):
            return None
        root = Path(agent.workdir)
        if not is_repo(root):
            if not getattr(rt, "_snap_notice", False):
                rt._snap_notice = True
                try:
                    rt.ui.info("journal.git_snapshots: not a git repo — snapshots off")
                except Exception:
                    pass
            return None
        cur = _status_hash(root)
        last = getattr(rt, "_snap_last", None)
        if not force and last is not None and cur == last:
            return None
        rt._snap_last = cur
        t = getattr(agent, "turns", 0) if turn is None else int(turn)
        sha = snapshot(root, rt.run_id or "run", t, rt.cfg)
        if sha and rt.journal:
            try:
                rt.journal.event("snapshot", getattr(agent, "id", None),
                                 turn=t, sha=sha[:12])
            except Exception:
                pass
        return sha
    except Exception:
        return None


# ------------------------------------------------------------------ undo

def _snapshot_files(root, sha) -> list:
    p = _git(root, "ls-tree", "-r", "--name-only", sha)
    if p.returncode != 0:
        return []
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def _worktree_files(root) -> list:
    p = _git(root, "ls-files", "-co", "--exclude-standard")
    if p.returncode != 0:
        return []
    return [ln for ln in p.stdout.splitlines() if ln.strip()]


def _ref_age_days(root, sha) -> float:
    p = _git(root, "log", "-1", "--format=%ct", sha)
    try:
        return (time.time() - float(p.stdout.strip())) / 86400.0
    except Exception:
        return 0.0


def prune(root, days: float = PRUNE_DAYS) -> int:
    """Delete snapshot refs older than *days*. Returns how many were removed."""
    root = Path(root)
    p = _git(root, "for-each-ref", "refs/hydra/", "--format=%(refname) %(objectname)")
    removed = 0
    if p.returncode != 0:
        return 0
    for line in p.stdout.splitlines():
        parts = line.strip().split()
        if len(parts) != 2:
            continue
        ref, sha = parts
        if _ref_age_days(root, sha) > float(days):
            if _git(root, "update-ref", "-d", ref).returncode == 0:
                removed += 1
    return removed


def plan(root, run_id=None, to=None) -> dict:
    """Compute what an undo would do. Returns {ok, run, turn, sha, restore, remove, note}."""
    root = Path(root)
    if not is_repo(root):
        return {"ok": False, "note": f"not a git repo: {root}"}
    runs = list_runs(root)
    if not runs:
        return {"ok": False, "note": "no snapshots (journal.git_snapshots was off?)"}
    if run_id:
        turns = _list_turns(root, run_id)
        if not turns:
            return {"ok": False, "note": f"no snapshots for run {run_id!r}"}
    else:
        run_id, _last = runs[-1]
        turns = _list_turns(root, run_id)
    if to is not None:
        match = [t for t in turns if int(t[0]) == int(to)]
        if not match:
            avail = ", ".join(str(t[0]) for t in turns)
            return {"ok": False, "note": f"run {run_id} has no turn {to} (turns: {avail})"}
        chosen = match[0]
    else:
        # default: the run's baseline — the tree before the run started
        # (turn 0, written by the loop on the first turn). Older snapshot
        # series predate the baseline ref, so fall back to their first ref.
        chosen = turns[0]
    turn, sha = chosen
    snap = set(_snapshot_files(root, sha))
    work = set(_worktree_files(root))
    remove = sorted(work - snap)
    restore = sorted(snap)
    return {"ok": True, "run": run_id, "turn": turn, "sha": sha,
            "restore": restore, "remove": remove}


def undo(root, run_id=None, to=None, dry_run=False) -> dict:
    """Restore the working tree from a snapshot. Returns the plan + 'done'."""
    root = Path(root)
    info = plan(root, run_id, to)
    if not info.get("ok"):
        return info
    if dry_run:
        info["done"] = False
        return info
    sha = info["sha"]
    # 1. remove files that did not exist in the snapshot (only under root)
    for rel in info["remove"]:
        p = root / rel
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
        except Exception:
            pass
    # 2. restore every snapshot file into the worktree (never the index)
    p = _git(root, "restore", f"--source={sha}", "--worktree", "--", ".",
             timeout=120)
    if p.returncode != 0:
        info["fail"] = (p.stderr or p.stdout or "").strip()[:500]
    # clean up directories left empty by removals
    for rel in info["remove"]:
        try:
            d = (root / rel).parent
            while d != root and d.is_dir() and not any(d.iterdir()):
                d.rmdir()
                d = d.parent
        except Exception:
            pass
    info["done"] = True
    return info
