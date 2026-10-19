"""Project layer (§19.1–19.2): instruction files, skills, custom commands.

Everything here is additive and token-thrifty:
* instruction files land in the env block once per run (never the system prompt)
* skills are progressive-disclosure: only name — description in the env block,
  the body loads on demand through the skill() tool
* custom commands are prompt templates with $ARGS / $1..$9 and !`cmd` expansion
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

from . import config as C

BUILTIN_SKILLS = Path(__file__).parent / "skills"


# ---------------------------------------------------------------- instructions

def _read_capped(path: Path, cap: int) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    if len(text) > cap:
        text = text[:cap] + f"\n[truncated at {cap} chars]"
    return text


def expand_includes(text: str, base: Path) -> str:
    """`@path` lines expand one level deep (§19.1)."""
    def repl(m):
        rel = m.group(1).strip().strip('"').strip("'")
        if not rel:
            return m.group(0)
        p = Path(rel)
        if not p.is_absolute():
            p = base / rel
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            return m.group(0)
    return re.sub(r"^@(\S+)\s*$", repl, text, flags=re.M)


def find_instruction_files(cwd) -> list:
    """Global ~/.hydra/HYDRA.md, then HYDRA.md/AGENTS.md walking from cwd up
    to $HOME (nearest last). Each file appears once."""
    cwd = Path(cwd).resolve()
    home = Path.home().resolve()
    out = []
    g = C.home() / "HYDRA.md"
    if g.is_file():
        out.append(g)
    chain = []
    d = cwd
    for _ in range(12):  # bounded walk
        chain.append(d)
        if d == home or d.parent == d:
            break
        d = d.parent
    for d in reversed(chain):
        for name in ("HYDRA.md", "AGENTS.md"):
            p = d / name
            if p.is_file():
                out.append(p)
                break
    return out


def project_text(cwd, cap: int = 8000) -> str:
    parts, seen = [], set()
    for p in find_instruction_files(cwd):
        rp = str(p.resolve())
        if rp in seen:
            continue
        seen.add(rp)
        text = expand_includes(_read_capped(p, cap), p.parent)
        if len(text) > cap:
            text = text[:cap] + f"\n[truncated at {cap} chars]"
        if text.strip():
            parts.append(f"## {p}\n{text.strip()}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------- skills

def parse_frontmatter(text: str):
    meta, body = {}, text
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if m:
        for line in m.group(1).splitlines():
            k, sep, v = line.partition(":")
            if sep:
                meta[k.strip().lower()] = v.strip()
        body = text[m.end():]
    return meta, body


def find_skills(cwd) -> dict:
    """name -> skill dict; project skills override global, builtin last."""
    out = {}
    for d in (BUILTIN_SKILLS, C.home() / "skills", Path(cwd) / ".hydra" / "skills"):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*/SKILL.md")):
            try:
                raw = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            meta, body = parse_frontmatter(raw)
            name = meta.get("name") or p.parent.name
            roles = [r.strip() for r in str(meta.get("roles", "")).split(",")
                     if r.strip()]
            out[name] = {"name": name, "description": meta.get("description", ""),
                         "roles": roles, "dir": str(p.parent), "path": str(p),
                         "body": body.strip(), "meta": meta}
    return out


def skills_listing(cwd, role: str = None) -> str:
    lines = []
    for name, s in sorted(find_skills(cwd).items()):
        if role and s["roles"] and role not in s["roles"]:
            continue
        lines.append(f"- {name} — {s['description']}" if s["description"]
                     else f"- {name}")
    return "\n".join(lines)


# ---------------------------------------------------------------- workspace

# Facts about cwd are injected once per run so a new run can see the existing
# project instead of building a parallel duplicate (the gym-site failure).
_HEAVY_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".cache", ".npm", ".local", ".mypy_cache", ".pytest_cache", "target",
    "vendor", "site-packages", "dist", "build", ".next", ".nuxt",
    ".svelte-kit", ".tox", ".gradle", ".idea", ".vscode", "spill",
}
_SENSITIVE_NAME = re.compile(
    r"(?i)(secret|token|passwd|password|credential|api[_-]?key|auth|"
    r"\.env($|\.)|\.pem$|id_rsa|id_ed25519)")
_MARKERS = (
    "index.html", "package.json", "pyproject.toml", "requirements.txt",
    "Cargo.toml", "go.mod", "pom.xml", "build.gradle", "HYDRA.md", "README.md",
    "main.py", "app.py", "manage.py", "main.js", "app.js", "script.js",
    "index.js", "vite.config.js", "vite.config.ts", "Dockerfile",
    "docker-compose.yml", "Makefile",
)


def _skip_name(name: str) -> bool:
    name = str(name or "")
    return (not name or name.startswith(".") or name in _HEAVY_DIRS
            or bool(_SENSITIVE_NAME.search(name)))


def _is_dir(e) -> bool:
    try:
        return bool(e.is_dir(follow_symlinks=False))
    except Exception:
        return False


def _has_file(path: Path) -> bool:
    try:
        for _root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if not _skip_name(d)]
            if any(not _skip_name(f) for f in files):
                return True
    except Exception:
        return False
    return False


def _project_facts(path: Path, cap: int = 8) -> list:
    facts = [m for m in _MARKERS if (path / m).exists()]
    try:
        for e in sorted(os.scandir(path), key=lambda x: x.name.lower()):
            if len(facts) >= cap:
                break
            if _skip_name(e.name) or not _is_dir(e):
                continue
            if _has_file(Path(e.path)):
                facts.append(e.name + "/")
    except Exception:
        pass
    out = []
    for x in facts:
        if x not in out:
            out.append(x)
    return out[:cap]


def _recent_files(cwd: Path, limit: int = 6, max_scan: int = 3000) -> list:
    found, scanned = [], 0
    stack = [(cwd, 0)]
    while stack and scanned < max_scan:
        d, depth = stack.pop()
        try:
            entries = list(os.scandir(d))
        except Exception:
            continue
        for e in entries:
            if scanned >= max_scan:
                break
            if _skip_name(e.name):
                continue
            try:
                if _is_dir(e):
                    if depth < 2:
                        stack.append((Path(e.path), depth + 1))
                    continue
                if not e.is_file(follow_symlinks=False):
                    continue
                st = e.stat(follow_symlinks=False)
            except Exception:
                continue
            try:
                rel = str(Path(e.path).relative_to(cwd))
            except Exception:
                rel = str(e.path)
            found.append((float(getattr(st, "st_mtime", 0) or 0), rel))
            scanned += 1
    found.sort(key=lambda x: -x[0])
    return [rel for _, rel in found[:limit]]


def workspace_summary(cwd, cfg=None) -> str:
    """Token-thin facts about cwd: entries, projects, recent files."""
    if cfg is not None and not C.get(cfg, "project.workspace", True):
        return ""
    cwd = Path(cwd)
    try:
        cwd = cwd.resolve()
    except Exception:
        pass
    if not cwd.is_dir():
        return ""
    try:
        entries = [e for e in os.scandir(cwd) if not _skip_name(e.name)]
    except Exception:
        return ""
    entries.sort(key=lambda e: (not _is_dir(e), e.name.lower()))
    limit = int(C.get(cfg, "project.workspace_entries", 16) or 16) if cfg else 16
    names = [e.name + ("/" if _is_dir(e) else "") for e in entries[:limit]]
    if not names:
        return ""
    lines = ["# Workspace (cwd)", "entries: " + " ".join(names)]
    plimit = int(C.get(cfg, "project.workspace_projects", 5) or 5) if cfg else 5
    projects = []
    for e in entries:
        if len(projects) >= plimit:
            break
        if not _is_dir(e):
            continue
        facts = _project_facts(Path(e.path))
        if facts:
            projects.append(f"- {e.name}/: " + ", ".join(facts))
    if projects:
        lines.append("projects:")
        lines.extend(projects)
    rlimit = int(C.get(cfg, "project.workspace_recent", 6) or 6) if cfg else 6
    recent = _recent_files(cwd, limit=rlimit)
    if recent:
        lines.append("recent files: " + ", ".join(recent))
    return "\n".join(lines)


def _report_files(events_path: Path, limit: int = 4) -> list:
    last = None
    try:
        # A long run can have a very large journal; the report event is near
        # the end, so read a bounded tail instead of the whole file.
        size = events_path.stat().st_size
        with events_path.open("rb") as fh:
            if size > 512_000:
                fh.seek(size - 512_000)
            raw = fh.read().decode("utf-8", "replace")
    except Exception:
        return []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict) and ev.get("type") == "report":
            last = ev
    if not isinstance(last, dict):
        return []
    rep = last.get("report") if isinstance(last.get("report"), dict) else {}
    files = [str(x) for x in (rep.get("files_changed") or [])] + \
            [str(x) for x in (rep.get("artifacts") or [])]
    out = []
    for f in files:
        if f and f not in out:
            out.append(f)
    return out[:limit]


def recent_runs_summary(cwd, cfg=None) -> str:
    """The last few runs that started in this same cwd (§19 continuity)."""
    limit = int(C.get(cfg, "project.recent_runs", 3) or 0) if cfg else 3
    if limit <= 0:
        return ""
    try:
        cwd_s = str(Path(cwd).resolve())
    except Exception:
        cwd_s = str(cwd)
    runs_dir = C.home() / "runs"
    if not runs_dir.is_dir():
        return ""
    try:
        candidates = list(runs_dir.iterdir())
    except Exception:
        return ""
    items = []
    for d in candidates:
        rj = d / "run.json"
        if not rj.is_file():
            continue
        try:
            data = json.loads(rj.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        rj_cwd = data.get("cwd")
        try:
            rj_cwd = str(Path(str(rj_cwd)).resolve())
        except Exception:
            rj_cwd = str(rj_cwd)
        if rj_cwd != cwd_s:
            continue
        try:
            started = float(data.get("started") or 0)
        except Exception:
            started = 0.0
        items.append((started, d.name, d, data))
    items.sort(key=lambda x: x[0], reverse=True)
    redact = None
    if cfg is not None:
        try:
            from .hygiene import make_redactor
            redact = make_redactor(cfg)
        except Exception:
            redact = None
    lines = []
    for _started, run_id, d, data in items[:limit]:
        task = " ".join(str(data.get("task") or "").split())
        if redact:
            task = redact(task)
        status = str(data.get("status") or "?")
        line = f"- {run_id} [{status}] {task[:140]}"
        files = _report_files(d / "events.jsonl")
        if files:
            line += " -> " + ", ".join(files)
        lines.append(line)
    if not lines:
        return ""
    return "# Recent runs (same cwd)\n" + "\n".join(lines)


def environment_summary(cwd, cfg=None) -> str:
    parts = []
    ws = workspace_summary(cwd, cfg)
    if ws.strip():
        parts.append(ws)
    rr = recent_runs_summary(cwd, cfg)
    if rr.strip():
        parts.append(rr)
    return "\n\n".join(parts)


# ---------------------------------------------------------------- commands

def find_commands(cwd) -> dict:
    """name -> template text; project commands override global ones."""
    out = {}
    for d in (C.home() / "commands", Path(cwd) / ".hydra" / "commands"):
        if not d.is_dir():
            continue
        for p in sorted(d.glob("*.md")):
            try:
                out[p.stem] = p.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
    return out


def render_command(text: str, args_str: str = "") -> str:
    """$ARGS, $1..$9, then !`cmd` shell substitution (executed immediately)."""
    out = text.replace("$ARGS", args_str)
    parts = str(args_str or "").split()
    for i in range(1, 10):
        out = out.replace(f"${i}", parts[i - 1] if i <= len(parts) else "")

    def repl(m):
        cmd = m.group(1)
        try:
            p = subprocess.run([C.shell_path(), "-lc", cmd], capture_output=True,
                               text=True, timeout=30)
            return (p.stdout or "").strip()
        except Exception as exc:
            return f"[command failed: {type(exc).__name__}: {exc}]"

    return re.sub(r"!`([^`]*)`", repl, out)
