"""Repo map (§19.4): a file tree with top-level symbols, cached and budgeted.

* Python files -> ``ast`` (classes, functions, signatures)
* everything else -> ``universal-ctags`` when installed, else regex heuristics
* cached in ``<root>/.hydra/repomap.json`` keyed by (path, mtime, size),
  so rebuilds are incremental
* ranked by recent git activity (``git log --name-only -n 50``), then path
  depth, then truncated to a token budget (chars / 3.5)

Never injected automatically.
"""
from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import time
from pathlib import Path

SKIP_DIRS = {
    ".git", ".hg", ".svn", "__pycache__", "node_modules", ".venv", "venv",
    "dist", "build", "target", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".idea", ".vscode", ".hydra", ".cache", ".gradle", ".tox",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz",
    ".xz", ".tar", ".7z", ".apk", ".so", ".a", ".o", ".class", ".jar", ".dex",
    ".mp3", ".mp4", ".woff", ".woff2", ".ttf", ".otf", ".exe", ".dll", ".pyc",
    ".whl", ".sqlite", ".db", ".bin",
}
PY_EXT = {".py", ".pyi"}
CTAGS_LANG_EXT = {
    ".c", ".h", ".cpp", ".cc", ".hpp", ".java", ".kt", ".rs", ".rb", ".go",
    ".js", ".jsx", ".ts", ".tsx", ".php", ".cs", ".swift", ".lua", ".sh",
    ".bash", ".zsh",
}
MAX_FILES_WALK = 4000
MAX_FILE_BYTES = 512 * 1024
CHARS_PER_TOKEN = 3.5  # same estimator as the context manager (§6)
STALE_DAYS = 7


# ------------------------------------------------------------------ symbols

def _sig(node) -> str:
    """Render a function signature only (no body)."""
    try:
        a = node.args
        parts = []
        posonly = list(getattr(a, "posonlyargs", []) or [])
        allpos = posonly + list(a.args or [])
        defaults = [None] * (len(allpos) - len(a.defaults or [])) + list(a.defaults or [])
        for i, (arg, default) in enumerate(zip(allpos, defaults)):
            s = arg.arg
            if getattr(arg, "annotation", None) is not None:
                s += ": " + ast.unparse(arg.annotation)
            if default is not None:
                s += "=" + ast.unparse(default)
            parts.append(s)
            if posonly and i == len(posonly) - 1:
                parts.append("/")
        if a.vararg is not None:
            s = "*" + a.vararg.arg
            if getattr(a.vararg, "annotation", None) is not None:
                s += ": " + ast.unparse(a.vararg.annotation)
            parts.append(s)
        elif a.kwonlyargs:
            parts.append("*")
        for arg, default in zip(a.kwonlyargs or [], a.kw_defaults or []):
            s = arg.arg
            if getattr(arg, "annotation", None) is not None:
                s += ": " + ast.unparse(arg.annotation)
            if default is not None:
                s += "=" + ast.unparse(default)
            parts.append(s)
        if a.kwarg is not None:
            s = "**" + a.kwarg.arg
            if getattr(a.kwarg, "annotation", None) is not None:
                s += ": " + ast.unparse(a.kwarg.annotation)
            parts.append(s)
        ret = ""
        if getattr(node, "returns", None) is not None:
            ret = " -> " + ast.unparse(node.returns)
        return "(" + ", ".join(parts) + ")" + ret
    except Exception:
        return ""


def _py_symbols(text: str) -> list:
    try:
        tree = ast.parse(text)
    except Exception:
        return []
    out = []

    def one(node, indent: str = ""):
        if isinstance(node, ast.ClassDef):
            bases = ",".join(_sig(b) for b in node.bases if b is not None)
            out.append(f"{indent}class {node.name}" + (f"({bases})" if bases else ""))
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    pre = "async def" if isinstance(sub, ast.AsyncFunctionDef) else "def"
                    out.append(f"{indent}  {pre} {sub.name}{_sig(sub)}")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            pre = "async def" if isinstance(node, ast.AsyncFunctionDef) else "def"
            out.append(f"{indent}{pre} {node.name}{_sig(node)}")

    for node in tree.body:
        one(node, "")
    return out[:60]


_JS_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?"
    r"(?:class\s+(\w+)|(?:async\s+)?function\s+(\w+)|"
    r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?(?:\(|function))", re.M)
_GO_RE = re.compile(r"^(?:func\s+(?:\([^)]*\)\s*)?(\w+)|type\s+(\w+)\s+struct)", re.M)
_RS_RE = re.compile(r"^\s*(?:pub\s+)?(?:fn|struct|enum|trait)\s+(\w+)", re.M)
_CTAG_RE = re.compile(r"^(\S+)\s+(\S+)\s+(\d+)\s+(.+)$", re.M)


def _ctags_symbols(path: Path) -> list | None:
    if not shutil.which("ctags"):
        return None
    try:
        p = subprocess.run(["ctags", "-x", "--sort=no", "-f", "-", str(path)],
                           capture_output=True, text=True, timeout=10)
        if p.returncode != 0:
            return None
        out = []
        for m in _CTAG_RE.finditer(p.stdout or ""):
            name, kind = m.group(1), m.group(2)
            if kind in ("file", "macro", "header"):
                continue
            out.append(f"{kind} {name}")
        return out[:60] if out else []
    except Exception:
        return None


def _regex_symbols(text: str, ext: str) -> list:
    out = []
    if ext in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        for m in _JS_RE.finditer(text):
            name = m.group(1) or m.group(2) or m.group(3)
            kind = "class" if m.group(1) else "function"
            out.append(f"{kind} {name}")
    elif ext == ".go":
        for m in _GO_RE.finditer(text):
            out.append(f"func {m.group(1)}" if m.group(1) else f"type {m.group(2)}")
    elif ext == ".rs":
        for m in _RS_RE.finditer(text):
            out.append(f"fn/type {m.group(1)}")
    elif ext in (".sh", ".bash", ".zsh"):
        for m in re.finditer(r"^\s*(?:function\s+)?(\w+)\s*\(\)\s*\{", text, re.M):
            out.append(f"function {m.group(1)}")
    return out[:40]


def _symbols_for(path: Path) -> list:
    ext = path.suffix.lower()
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return ["(large file — symbols skipped)"]
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return []
    if ext in PY_EXT:
        return _py_symbols(text)
    if ext in CTAGS_LANG_EXT:
        got = _ctags_symbols(path)
        if got is not None:
            return got
        return _regex_symbols(text, ext)
    return []


# ------------------------------------------------------------------ cache

def _cache_path(root: Path) -> Path:
    return root / ".hydra" / "repomap.json"


def _load_cache(root: Path) -> dict:
    try:
        data = json.loads(_cache_path(root).read_text(encoding="utf-8"))
        if isinstance(data, dict) and data.get("version") == 1:
            return data
    except Exception:
        pass
    return {"version": 1, "files": {}}


def _save_cache(root: Path, cache: dict) -> None:
    p = _cache_path(root)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        tmp.replace(p)
    except Exception:
        pass


# ------------------------------------------------------------------ walk

def _walk(root: Path) -> list:
    """[(relpath, mtime, size)] for text-ish files, skip dirs, capped."""
    out = []
    stack = [root]
    count = 0
    while stack and count < MAX_FILES_WALK:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name)
        except Exception:
            continue
        for p in entries:
            if p.is_dir():
                if p.name in SKIP_DIRS or p.name.startswith(".git"):
                    continue
                stack.append(p)
            elif p.is_file():
                count += 1
                if p.suffix.lower() in BINARY_EXT:
                    continue
                try:
                    st = p.stat()
                except Exception:
                    continue
                out.append((str(p.relative_to(root)), st.st_mtime, st.st_size))
                if count >= MAX_FILES_WALK:
                    break
    return out


def _git_recent(root: Path) -> dict:
    counts: dict = {}
    if not shutil.which("git") or not (root / ".git").exists():
        return counts
    try:
        p = subprocess.run(["git", "-C", str(root), "log", "--name-only",
                            "-n", "50", "--pretty=format:"],
                           capture_output=True, text=True, timeout=20)
        for line in (p.stdout or "").splitlines():
            line = line.strip()
            if line:
                counts[line] = counts.get(line, 0) + 1
    except Exception:
        pass
    return counts


# ------------------------------------------------------------------ build

def build(root=".", budget=1500, cfg=None) -> tuple:
    """Return (text, stats). Incremental via the cache."""
    root = Path(root).expanduser().resolve()
    if not root.is_dir():
        return f"ERROR: not a directory: {root}", {"files": 0}
    budget = max(200, int(budget or 1500))
    cache = _load_cache(root)
    files = _walk(root)
    seen = set()
    updated = 0
    for rel, mtime, size in files:
        seen.add(rel)
        rec = cache["files"].get(rel)
        if rec and abs(float(rec.get("mtime", 0)) - mtime) < 1e-6 \
                and int(rec.get("size", -1)) == int(size):
            continue
        syms = _symbols_for(root / rel)
        cache["files"][rel] = {"mtime": mtime, "size": size, "symbols": syms}
        updated += 1
    for gone in [k for k in cache["files"] if k not in seen]:
        del cache["files"][gone]
    _save_cache(root, cache)

    recent = _git_recent(root)
    ranked = sorted(
        seen,
        key=lambda r: (-recent.get(r, 0), r.count("/"), r))
    budget_chars = int(budget * CHARS_PER_TOKEN)
    lines = []
    used = 0
    shown = 0
    head = f"repo map: {root} ({len(seen)} files)"
    used += len(head) + 1
    lines.append(head)
    for rel in ranked:
        syms = (cache["files"].get(rel) or {}).get("symbols") or []
        block = [f"  {rel}"]
        block += [f"    {s}" for s in syms[:12]]
        text = "\n".join(block)
        if used + len(text) + 1 > budget_chars:
            if shown == 0:  # always show at least something
                lines.append(text[: max(0, budget_chars - used)])
                shown += 1
            break
        lines.append(text)
        used += len(text) + 1
        shown += 1
    if shown < len(seen):
        lines.append(f"… {len(seen) - shown} more files (budget {budget} tokens)")
    text = "\n".join(lines)
    return text, {"files": len(seen), "shown": shown, "updated": updated}


def stats_for_tool(root=".", budget=1500, cfg=None) -> str:
    text, st = build(root, budget, cfg)
    if st.get("files") == 0:
        return text
    return text
