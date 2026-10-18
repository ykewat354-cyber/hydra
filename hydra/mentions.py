"""@mentions (§19.7): inline files, directories and URLs into a task string.

* ``@path/file``   -> up to 200 lines of the file (tool cap applies)
* ``@dir/``        -> an ``ls`` of that directory
* ``@https://…``   -> fetched via web_fetch, wrapped as <untrusted>

Mentions must start a word (so ``user@example.com`` is left alone). Failures
leave the mention text untouched — zero friction, never a gate.
"""
from __future__ import annotations

import re
from pathlib import Path

MENTION_RE = re.compile(r"(?<!\S)@(https?://\S+|[^\s@]+)")
MAX_LINES = 200
MAX_FILE_CHARS = 6000      # §6 tool cap
MAX_DIR_ENTRIES = 200
MAX_WEB_CHARS = 8000       # §6 web_fetch cap


def _file(cfg, raw, p: Path) -> str:
    from . import hygiene
    try:
        text = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return f"@{raw} (unreadable: {type(exc).__name__})"
    lines = text.splitlines()
    if len(lines) > MAX_LINES:
        text = "\n".join(lines[:MAX_LINES]) + f"\n[... {len(lines) - MAX_LINES} more lines]"
    if len(text) > MAX_FILE_CHARS:
        text = text[:MAX_FILE_CHARS] + "\n[... truncated to 6000 chars]"
    body = f"[file @{raw}]\n{text}\n[file end @{raw}]"
    if hygiene.is_untrusted(cfg, p):
        return hygiene.wrap_untrusted(cfg, body, str(p))
    return body


def _dir(raw, p: Path) -> str:
    try:
        entries = sorted(p.iterdir(), key=lambda q: (q.is_file(), q.name))
    except Exception as exc:
        return f"@{raw} (unreadable: {type(exc).__name__})"
    names = [e.name + ("/" if e.is_dir() else "") for e in entries[:MAX_DIR_ENTRIES]]
    more = len(entries) - len(names)
    listing = "\n".join(names) + (f"\n[... {more} more]" if more > 0 else "")
    return f"[dir @{raw}]\n{listing}\n[dir end @{raw}]"


def _http(cfg, raw: str, client) -> str:
    from . import hygiene
    if client is None:
        return f"@{raw} (web_fetch unavailable)"
    try:
        data = client.web_fetch(raw)
    except Exception as exc:
        return f"@{raw} (fetch failed: {type(exc).__name__})"
    content = str(data.get("content") or data.get("text") or "")
    title = data.get("title") or ""
    if len(content) > MAX_WEB_CHARS:
        content = content[:MAX_WEB_CHARS] + "\n[... trimmed]"
    body = f"[web @{raw}]\n# {title}\n{content}\n[web end @{raw}]"
    return hygiene.wrap_untrusted(cfg, body, raw)


def expand(cfg, text: str, cwd=".", client=None) -> str:
    """Replace all @mentions in *text*. Never raises."""
    if not text or "@" not in text:
        return text
    cwd = Path(cwd)

    def repl(m):
        raw = m.group(1)
        try:
            if raw.startswith(("http://", "https://")):
                return _http(cfg, raw, client)
            p = Path(raw).expanduser()
            if not p.is_absolute():
                p = cwd / p
            if raw.endswith("/") or p.is_dir():
                return _dir(raw, p)
            if p.is_file():
                return _file(cfg, raw, p)
        except Exception:
            pass
        return m.group(0)

    try:
        return MENTION_RE.sub(repl, text)
    except Exception:
        return text
