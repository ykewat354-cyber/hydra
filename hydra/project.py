"""Project layer (§19.1–19.2): instruction files, skills, custom commands.

Everything here is additive and token-thrifty:
* instruction files land in the env block once per run (never the system prompt)
* skills are progressive-disclosure: only name — description in the env block,
  the body loads on demand through the skill() tool
* custom commands are prompt templates with $ARGS / $1..$9 and !`cmd` expansion
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from . import config as C


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
    """name -> skill dict; project skills override global ones."""
    out = {}
    for d in (C.home() / "skills", Path(cwd) / ".hydra" / "skills"):
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
