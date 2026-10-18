"""File tools: read / write / edit / apply_patch / ls / glob / grep.

read_file defaults to 200 lines; grep prefers ripgrep and falls back to a pure
Python scan. Paths resolve against the agent's workdir.
"""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from . import Tool
from .. import hygiene


def _resolve(ctx, raw) -> Path:
    p = Path(str(raw or ".")).expanduser()
    if not p.is_absolute():
        p = ctx.cwd / p
    return p


def _tool_read_file(args, ctx) -> str:
    path = _resolve(ctx, args.get("path"))
    if not path.is_file():
        return f"ERROR: not a file: {path}"
    start = max(1, int(args.get("start") or 1))
    end = args.get("end")
    end = int(end) if end else start + 199
    out = []
    last = start - 1
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for i, line in enumerate(fh, 1):
            if i < start:
                continue
            if i > end:
                break
            out.append(f"{i}: {line.rstrip(chr(10))}")
            last = i
    body = "\n".join(out) if out else f"[no lines between {start} and {end}]"
    header = f"{path} — lines {start}-{last}"
    if len(out) == (end - start + 1):
        header += f" (truncated; continue with start={last + 1})"
    text = header + "\n" + body
    if hygiene.is_untrusted(ctx.cfg, path):
        return hygiene.wrap_untrusted(ctx.cfg, text, str(path))
    return text


def _tool_write_file(args, ctx) -> str:
    path = _resolve(ctx, args.get("path"))
    content = args.get("content")
    if content is None:
        return "ERROR: content is required"
    content = str(content)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".hydra-tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    return f"wrote {len(content)} chars to {path}"


def _closest_context(path: Path, needle: str, span: int = 4) -> str:
    first = needle.strip().splitlines()[0] if needle.strip() else ""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    hits = [i for i, l in enumerate(lines) if first and first in l][:3]
    if not hits:
        return "No similar line found."
    chunks = []
    for i in hits:
        lo, hi = max(0, i - span), min(len(lines), i + span + 1)
        chunks.append("\n".join(f"{n + 1}: {lines[n]}" for n in range(lo, hi)))
    return "Closest matches:\n" + "\n---\n".join(chunks)


def _tool_edit_file(args, ctx) -> str:
    path = _resolve(ctx, args.get("path"))
    if not path.is_file():
        return f"ERROR: not a file: {path}"
    old = args.get("old")
    new = args.get("new")
    if old is None or new is None:
        return "ERROR: both old and new are required"
    old, new = str(old), str(new)
    count = max(1, int(args.get("count") or 1))
    text = path.read_text(encoding="utf-8", errors="replace")
    occurrences = text.count(old)
    if occurrences == 0:
        return (f"ERROR: old text not found in {path}.\n"
                + _closest_context(path, old))
    if count == 1 and occurrences > 1:
        return (f"ERROR: old text occurs {occurrences} times in {path}; make it unique "
                f"or pass count={occurrences}.")
    new_text = text.replace(old, new, count) if count > 1 else text.replace(old, new, 1)
    before, after = text.count("\n"), new_text.count("\n")
    path.write_text(new_text, encoding="utf-8")
    return (f"edited {path}: replaced {min(count, occurrences)} of {occurrences} "
            f"occurrence(s); lines {before} -> {after}")


def _tool_apply_patch(args, ctx) -> str:
    patch = str(args.get("patch") or "")
    if not patch.strip():
        return "ERROR: empty patch"
    tmp = Path(tempfile.gettempdir()) / f"hydra_patch_{id(args)}.diff"
    tmp.write_text(patch, encoding="utf-8")
    try:
        if (ctx.cwd / ".git").exists() and shutil.which("git"):
            cmd = ["git", "apply", "--whitespace=nowarn", str(tmp)]
        elif shutil.which("patch"):
            cmd = ["patch", "-p1", "-N", "-i", str(tmp)]
        else:
            return "ERROR: neither git nor patch is available"
        proc = subprocess.run(cmd, cwd=str(ctx.cwd), capture_output=True,
                              text=True, timeout=60)
        out = (proc.stdout or "") + (proc.stderr or "")
        return (out.strip() or "patch applied") + f"\n[exit {proc.returncode}]"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
    finally:
        try:
            tmp.unlink()
        except Exception:
            pass


def _tool_ls(args, ctx) -> str:
    path = _resolve(ctx, args.get("path") or ".")
    if not path.is_dir():
        return f"ERROR: not a directory: {path}"
    entries = sorted(path.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
    lines = []
    for e in entries[:500]:
        if e.is_dir():
            lines.append(f"{e.name}/")
        else:
            try:
                size = e.stat().st_size
            except Exception:
                size = 0
            lines.append(f"{e.name}  ({size} B)")
    extra = "" if len(entries) <= 500 else f"\n[... {len(entries) - 500} more entries]"
    return "\n".join(lines) + extra if lines else "(empty directory)"


def _tool_glob(args, ctx) -> str:
    pattern = str(args.get("pattern") or "*")
    base = _resolve(ctx, args.get("path") or ".")
    limit = int(args.get("limit") or 200)
    try:
        hits = [p for p in base.glob(pattern) if p.exists()][:limit]
    except Exception as exc:
        return f"ERROR: bad pattern: {exc}"
    if not hits:
        return "(no matches)"
    lines = []
    for p in sorted(hits):
        try:
            rel = p.relative_to(ctx.cwd)
        except ValueError:
            rel = p
        lines.append(str(rel) + ("/" if p.is_dir() else ""))
    return "\n".join(lines)


def _grep_python(pattern, base: Path, use_glob, ignore_case, limit):
    import fnmatch
    import re
    flags = re.IGNORECASE if ignore_case else 0
    try:
        rx = re.compile(pattern, flags)
    except re.error as exc:
        return f"ERROR: bad regex: {exc}"
    out = []
    skip = {".git", "__pycache__", "node_modules", ".hydra"}
    for p in base.rglob("*"):
        if len(out) >= limit:
            break
        if not p.is_file() or any(part in skip for part in p.parts):
            continue
        if use_glob and not fnmatch.fnmatch(p.name, use_glob):
            continue
        try:
            with p.open("r", encoding="utf-8", errors="strict") as fh:
                for i, line in enumerate(fh, 1):
                    if rx.search(line):
                        out.append(f"{p}:{i}:{line.rstrip(chr(10))[:200]}")
                        if len(out) >= limit:
                            break
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(out) if out else "(no matches)"


def _tool_grep(args, ctx) -> str:
    pattern = str(args.get("pattern") or "")
    if not pattern:
        return "ERROR: pattern is required"
    base = _resolve(ctx, args.get("path") or ".")
    use_glob = args.get("glob")
    ignore_case = bool(args.get("ignore_case"))
    limit = int(args.get("max_results") or 100)
    if shutil.which("rg"):
        cmd = ["rg", "-n", "--no-heading", "--color", "never", "-m", "50"]
        if ignore_case:
            cmd.append("-i")
        if use_glob:
            cmd += ["-g", str(use_glob)]
        cmd += ["--", pattern, str(base)]
        try:
            proc = subprocess.run(cmd, cwd=str(ctx.cwd), capture_output=True,
                                  text=True, timeout=60)
            text = proc.stdout or ""
            lines = text.splitlines()
            if len(lines) > limit:
                lines = lines[:limit] + [f"[... {len(text.splitlines()) - limit} more matches]"]
            return "\n".join(lines) if lines else "(no matches)"
        except Exception:
            pass  # fall through to the Python scan
    return _grep_python(pattern, base, use_glob, ignore_case, limit)


def register(reg) -> None:
    reg.register(Tool(
        "read_file", "Read a text file with line numbers. Default 200 lines; "
                     "grep first, read second.",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "start": {"type": "integer"},
            "end": {"type": "integer"}},
         "required": ["path"]},
        _tool_read_file))
    reg.register(Tool(
        "write_file", "Create or overwrite a file (parents auto-created).",
        {"type": "object", "properties": {"path": {"type": "string"},
                                          "content": {"type": "string"}},
         "required": ["path", "content"]},
        _tool_write_file))
    reg.register(Tool(
        "edit_file", "Exact-match replace in a file. Errors if 'old' is missing or "
                     "not unique (pass count>1 to replace multiple).",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"},
            "new": {"type": "string"}, "count": {"type": "integer"}},
         "required": ["path", "old", "new"]},
        _tool_edit_file))
    reg.register(Tool(
        "apply_patch", "Apply a unified diff (git apply or patch -p1).",
        {"type": "object", "properties": {"patch": {"type": "string"}},
         "required": ["patch"]},
        _tool_apply_patch))
    reg.register(Tool(
        "ls", "List a directory (dirs get a trailing /).",
        {"type": "object", "properties": {"path": {"type": "string"}}},
        _tool_ls))
    reg.register(Tool(
        "glob", "Find files by glob pattern (e.g. '**/*.py'). Newest-agnostic, capped.",
        {"type": "object", "properties": {"pattern": {"type": "string"},
                                          "path": {"type": "string"},
                                          "limit": {"type": "integer"}},
         "required": ["pattern"]},
        _tool_glob))
    reg.register(Tool(
        "grep", "Search inside files (rg when installed, else Python scan). "
                "Returns path:line:text.",
        {"type": "object", "properties": {
            "pattern": {"type": "string"}, "path": {"type": "string"},
            "glob": {"type": "string"}, "ignore_case": {"type": "boolean"},
            "max_results": {"type": "integer"}},
         "required": ["pattern"]},
        _tool_grep))
