"""File tools: read / write / edit / apply_patch / ls / glob / grep.

read_file defaults to 200 lines; grep prefers ripgrep and falls back to a pure
Python scan. Paths resolve against the agent's workdir.
"""
from __future__ import annotations

import difflib
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


def _binary_reason(path: Path):
    """Cheap sniff: NUL bytes or many control bytes in the first 8 KiB."""
    try:
        with path.open("rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return None
    if not chunk:
        return None
    if b"\x00" in chunk:
        return "NUL bytes in the first 8 KiB"
    allowed = set(range(0x20, 0x7f)) | {0x09, 0x0a, 0x0d, 0x0c, 0x08, 0x1b}
    control = sum(1 for b in chunk if b < 0x20 and b not in allowed)
    if control > len(chunk) * 0.3:
        return f"{control * 100 // len(chunk)}% control bytes in the first 8 KiB"
    return None


def _tool_read_file(args, ctx) -> str:
    path = _resolve(ctx, args.get("path"))
    if not path.is_file():
        return f"ERROR: not a file: {path}"
    why = _binary_reason(path)
    if why:
        return (f"ERROR: {path} looks like a binary file ({why}); read_file only "
                f"reads text. Use bash (`file`, `xxd`, `strings`) to inspect it.")
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


def _closest_context(path: Path, needle: str, span: int = 1) -> str:
    """Three line-numbered candidates for a failed edit, closest first."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return ""
    wanted = [" ".join(x.split()) for x in str(needle or "").splitlines()
              if " ".join(x.split())]
    if not lines or not wanted:
        return "No similar line found."
    scored = []
    for i, line in enumerate(lines):
        norm = " ".join(line.split())
        if not norm:
            continue
        score = max(difflib.SequenceMatcher(None, w, norm).ratio()
                    for w in wanted)
        # Exact substring hits beat a merely similar line.
        if any(w in norm or norm in w for w in wanted):
            score = max(score, 0.95)
        scored.append((score, i))
    scored.sort(key=lambda x: (-x[0], x[1]))
    picks = scored[:3]
    if not picks or picks[0][0] < 0.35:
        return "No similar line found."
    chunks = []
    for _score, i in picks:
        chunks.append(f"{i + 1}: {lines[i]}")
    return "Closest matches:\n" + "\n---\n".join(chunks)



def _tool_edit_file(args, ctx) -> str:
    path = _resolve(ctx, args.get("path"))
    if not path.is_file():
        return f"ERROR: not a file: {path}"
    count = max(1, int(args.get("count") or 1))
    text = path.read_text(encoding="utf-8", errors="replace")
    before = text.count("\n")

    edits = args.get("edits")
    if edits is not None:
        if not isinstance(edits, list) or not edits:
            return "ERROR: edits must be a non-empty list of {old,new}"
        for i, e in enumerate(edits, 1):
            if not isinstance(e, dict):
                return f"ERROR: edit {i} is not an object"
            old, new = e.get("old"), e.get("new")
            if old is None or new is None:
                return f"ERROR: edit {i}: both old and new are required"
            text, err = _apply_edit(text, str(old), str(new),
                                    max(1, int(e.get("count") or 1)))
            if err:
                return (f"ERROR: edit {i}/{len(edits)} failed: {err}\n"
                        + _closest_context(path, str(old))
                        + f"\n(atomic: no changes were written to {path})")
        path.write_text(text, encoding="utf-8")
        after = text.count("\n")
        return (f"edited {path}: {len(edits)} edit(s) applied atomically; "
                f"lines {before} -> {after}")

    old = args.get("old")
    new = args.get("new")
    if old is None or new is None:
        return "ERROR: both old and new are required"
    text, err = _apply_edit(text, str(old), str(new), count)
    if err:
        return f"ERROR: {err}\n" + _closest_context(path, str(old))
    path.write_text(text, encoding="utf-8")
    after = text.count("\n")
    return (f"edited {path}: lines {before} -> {after}")


def _norm_line(s: str) -> str:
    return " ".join(s.split())


def _ws_window(text: str, old: str):
    """Find where *old* matches text with whitespace normalized.

    Returns ("ok", start_line, n_lines) | ("multi", count) | None.
    """
    old_lines = [_norm_line(l) for l in old.split("\n")]
    old_lines = [l for l in old_lines if l != ""]
    if not old_lines:
        return None
    lines = text.split("\n")
    norm = [_norm_line(l) for l in lines]
    n = len(old_lines)
    hits = []
    i = 0
    while i <= len(norm) - n:
        if norm[i:i + n] == old_lines:
            hits.append(i)
            i += n
        else:
            i += 1
    if not hits:
        return None
    if len(hits) > 1:
        return ("multi", len(hits))
    return ("ok", hits[0], n)

def _stripped_window(text: str, old: str):
    """Exact structure, but ignore trailing whitespace on each line."""
    old_lines = [x.rstrip() for x in old.split("\n")]
    if not old.strip():
        return None
    lines = [x.rstrip() for x in text.split("\n")]
    n = len(old_lines)
    hits = []
    for i in range(0, max(0, len(lines) - n + 1)):
        if lines[i:i + n] == old_lines:
            hits.append(i)
    if not hits:
        return None
    if len(hits) > 1:
        return ("multi", len(hits))
    return ("ok", hits[0], n)


def _uniform_indent_window(text: str, old: str):
    """Match after removing one *uniform* leading-indent shift.

    This is the common paste case: the block was copied at 2 spaces while the
    file uses 4.  Relative indentation inside the block must still match.
    """
    old_lines = [x.rstrip() for x in old.split("\n")]
    if not old.strip():
        return None

    def base(block):
        indents = [len(x) - len(x.lstrip(" ")) for x in block if x.strip()]
        return min(indents) if indents else 0

    ob = base(old_lines)
    oded = [x[ob:] if x.strip() else x for x in old_lines]
    lines = [x.rstrip() for x in text.split("\n")]
    n = len(old_lines)
    hits = []
    for i in range(0, max(0, len(lines) - n + 1)):
        window = lines[i:i + n]
        wb = base(window)
        wded = [x[wb:] if x.strip() else x for x in window]
        if wded == oded:
            hits.append((i, wb - ob))
    if not hits:
        return None
    if len(hits) > 1:
        return ("multi", len(hits))
    return ("ok", hits[0][0], n, hits[0][1])


def _shift_new(new: str, delta: int) -> str:
    if not delta:
        return new
    out = []
    for line in new.split("\n"):
        if line.strip():
            out.append((" " * max(0, delta)) + line if delta > 0
                       else line[-delta:] if len(line) >= -delta else line.lstrip(" "))
        else:
            out.append(line)
    return "\n".join(out)


def _apply_edit(text: str, old: str, new: str, count: int = 1):
    """Exact -> trailing-space -> uniform-indent -> whitespace-normalized."""
    occurrences = text.count(old)
    if occurrences:
        if count == 1 and occurrences > 1:
            return None, (f"old text occurs {occurrences} times; make it unique "
                          f"or pass count={occurrences}.")
        return text.replace(old, new, count) if count > 1 else text.replace(old, new, 1), None

    hit = _stripped_window(text, old)
    if isinstance(hit, tuple) and hit[0] == "ok":
        _tag, start, n = hit
        lines = text.split("\n")
        lines[start:start + n] = new.split("\n")
        return "\n".join(lines), None
    if isinstance(hit, tuple) and hit[0] == "multi":
        return None, (f"old text matches {hit[1]} places even after ignoring "
                      f"trailing whitespace; add surrounding lines to make it unique")

    hit = _uniform_indent_window(text, old)
    if isinstance(hit, tuple) and hit[0] == "ok":
        _tag, start, n, delta = hit
        lines = text.split("\n")
        lines[start:start + n] = _shift_new(new, delta).split("\n")
        return "\n".join(lines), None
    if isinstance(hit, tuple) and hit[0] == "multi":
        return None, (f"old text matches {hit[1]} places after an indentation shift; "
                      f"add surrounding lines to make it unique")

    hit = _ws_window(text, old)
    if isinstance(hit, tuple) and hit[0] == "ok":
        _tag, start, n = hit
        lines = text.split("\n")
        lines[start:start + n] = new.split("\n")
        return "\n".join(lines), None
    if isinstance(hit, tuple) and hit[0] == "multi":
        return None, (f"old text matches {hit[1]} places in the file even after "
                      f"whitespace normalization; add surrounding lines to make "
                      f"it unique")
    return None, ("old text not found in the file (also tried trailing-whitespace, "
                  "uniform-indent and whitespace-normalized matching).")

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
        "edit_file", "Replace text in a file. Exact match first; if it fails, a "
                     "whitespace-normalized match is tried (must still be unique). "
                     "Pass edits=[{old,new},…] to apply several changes atomically, "
                     "or old/new for one (count>1 replaces multiple occurrences).",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "old": {"type": "string"},
            "new": {"type": "string"}, "count": {"type": "integer"},
            "edits": {"type": "array", "items": {
                "type": "object",
                "properties": {"old": {"type": "string"}, "new": {"type": "string"}}}}},
         "required": ["path"]},
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
