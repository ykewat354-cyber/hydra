"""Display helpers shared by the plain renderer and the full-screen TUI.

Everything here is pure: no terminal, no config, no network. That keeps the
noisy formatting rules testable and lets both UIs show exactly the same facts.
"""
from __future__ import annotations

import json
import os
import re
import unicodedata
from pathlib import Path

# ------------------------------------------------------------------ width

_ZERO_WIDTH = {"\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"}


def char_width(ch: str) -> int:
    if not ch or ch in _ZERO_WIDTH or unicodedata.combining(ch):
        return 0
    if ch in ("\ufe0e", "\ufe0f"):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(char_width(ch) for ch in str(text))


def clip_display(text: str, width: int) -> str:
    """Cut *text* to at most *width* terminal cells (no ellipsis added)."""
    if width <= 0:
        return ""
    out, used = [], 0
    for ch in str(text):
        w = char_width(ch)
        if used + w > width:
            break
        out.append(ch)
        used += w
    return "".join(out)


def pad_display(text: str, width: int) -> str:
    text = clip_display(text, width)
    return text + " " * max(0, width - display_width(text))


def short(text, n: int = 90) -> str:
    """Collapse whitespace and cap at n chars."""
    s = " ".join(str(text if text is not None else "").split())
    return s if len(s) <= n else s[: max(0, n - 1)] + "…"


def short_path(path, cwd=None) -> str:
    raw = str(path or "")
    try:
        p = Path(raw).expanduser()
        home = Path.home()
        if str(p).startswith(str(home)):
            p = Path("~") / p.relative_to(home)
        if cwd and not str(p).startswith("~"):
            try:
                p = p.relative_to(Path(cwd))
            except Exception:
                pass
        return str(p)
    except Exception:
        return raw


def fmt_tokens(n) -> str:
    try:
        n = int(n or 0)
    except Exception:
        return "0"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}k"
    return str(n)


def fmt_duration(seconds) -> str:
    try:
        s = max(0, int(seconds or 0))
    except Exception:
        s = 0
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_money(value) -> str:
    try:
        v = float(value or 0)
    except Exception:
        return "$0.0000"
    if v and v < 0.0001:
        return "<$0.0001"
    return f"${v:.4f}"


# ------------------------------------------------------------------ prose

def strip_inline(text: str) -> str:
    """Markdown-lite inline cleanup: keep the words and URLs, drop the noise."""
    s = str(text or "")
    s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)",
               lambda m: (m.group(1) or "image") + f" ({m.group(2)})", s)
    s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)",
               lambda m: m.group(1) if m.group(2) in ("", m.group(1))
               else f"{m.group(1)} ({m.group(2)})", s)
    s = re.sub(r"(\*\*|__)(.+?)\1", r"\2", s)
    s = s.replace("`", "")
    s = re.sub(r"^\s*>\s?", "│ ", s)
    return s


def _is_join_mark(ch: str) -> bool:
    """Zero-width joiners and combining marks (Mn/Mc/Me) must never start a row."""
    if not ch:
        return False
    return bool(unicodedata.combining(ch)) or unicodedata.category(ch) in (
        "Mn", "Mc", "Me", "Cf")


def _cluster_start(text: str) -> int:
    """Index where the trailing grapheme-ish cluster begins.

    A row must never break before a combining/spacing mark. A virama (or ZWJ)
    before the base also binds it to the cluster before it, so ``स्त`` still
    stays together while ``दु|नि|या`` can break between syllables.
    """
    s = max(0, len(text) - 1)
    while s > 0:
        ch, prev = text[s], text[s - 1]
        if _is_join_mark(ch) or unicodedata.combining(prev) or prev in _ZERO_WIDTH:
            s -= 1
        else:
            break
    return s


def _split_word(word: str, width: int) -> list:
    parts, cur = [], ""
    for ch in word:
        if cur and display_width(cur) + char_width(ch) > width:
            if _is_join_mark(ch):
                s = _cluster_start(cur)
                if s > 0:
                    parts.append(cur[:s])
                    cur = cur[s:] + ch
                    continue
                cur += ch  # one unsplittable cluster: keep the mark attached
                continue
            parts.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        parts.append(cur)
    return parts or [""]


def wrap_display(text: str, width: int, initial: str = "", hang: str = "",
                 hard: bool = False) -> list:
    """Wrap text to *width* terminal cells.

    ``initial`` prefixes the first output row, ``hang`` every later row.
    Newlines always start a new row. ``hard`` breaks long words (code/URLs);
    otherwise a too-long word gets its own hard-broken rows.
    """
    width = max(6, int(width or 0))
    text = "" if text is None else str(text)
    rows, first = [], True
    for logical in text.split("\n"):
        prefix = initial if (first and initial) else hang
        avail = max(1, width - display_width(prefix))
        if hard:
            if not logical:
                rows.append("")
            else:
                chunks = _split_word(logical, avail)
                for i, chunk in enumerate(chunks):
                    pre = (initial if first else hang) if i == 0 else hang
                    rows.append(pre + chunk)
                    first = False
            continue
        words = logical.split()
        if not words:
            rows.append("")
            first = False
            continue
        cur, cur_prefix = "", prefix
        cur_avail = avail
        for word in words:
            wl = display_width(word)
            if cur:
                if display_width(cur) + 1 + wl <= cur_avail:
                    cur += " " + word
                    continue
                rows.append(cur_prefix + cur)
                first = False
                cur_prefix = hang
                cur_avail = max(1, width - display_width(hang))
                cur = word
            else:
                cur = word
            # a single word may still be wider than the row
            if display_width(cur) > cur_avail:
                chunks = _split_word(cur, cur_avail)
                for j, chunk in enumerate(chunks[:-1]):
                    rows.append((cur_prefix if j == 0 else hang) + chunk)
                    first = False
                cur = chunks[-1]
                if len(chunks) > 1:
                    cur_prefix = hang
                    cur_avail = max(1, width - display_width(hang))
        if cur:
            rows.append(cur_prefix + cur)
            first = False
    return rows or [initial.rstrip()]


def _json_scalar(v) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        return " ".join(v.split())
    try:
        return json.dumps(v, ensure_ascii=False)
    except Exception:
        return str(v)


def _json_value(v, limit: int = 240) -> str:
    """One-line text for a JSON value, never a raw brace dump."""
    if isinstance(v, dict):
        bits = []
        for k, x in list(v.items())[:12]:
            bits.append(f"{k}: {_json_scalar(x) if not isinstance(x, (dict, list))
                        else _json_value(x, 80)}")
        text = ", ".join(bits)
    elif isinstance(v, list):
        text = ", ".join(_json_scalar(x) if not isinstance(x, (dict, list))
                         else _json_value(x, 80) for x in v[:20])
        if len(v) > 20:
            text += " …"
    else:
        text = _json_scalar(v)
    return short(text, limit)


def issue_text(it) -> str:
    """Readable one-line issue from a verifier/report item (dict or string)."""
    if isinstance(it, dict):
        what = str(it.get("what") or it.get("issue") or "").strip()
        fix = str(it.get("fix") or "").strip()
        if what:
            return what + (f" (fix: {fix})" if fix else "")
    return str(it)


def json_start(text: str) -> bool:
    """True when a partial stream looks like a bare JSON object/array.

    The TUI uses this to show ``writing structured output…`` instead of
    flickering raw braces while the model streams a finish payload.
    """
    s = str(text or "").lstrip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl < 0:
            return False
        s = s[nl + 1:].lstrip()
    return s.startswith("{") or s.startswith("[")


def json_to_markdown(text: str):
    """Turn a JSON-only assistant reply into readable markdown.

    Models sometimes emit the finish payload (or a whole report) as a bare
    JSON object. Printing that verbatim was the exact "raw tool dump" the TUI
    is meant to avoid. Returns ``None`` when *text* is not JSON.
    """
    raw = str(text or "").strip()
    m = re.match(r"^```(?:json|JSON)?\s*\n?(.*?)\n?```$", raw, re.S)
    if m:
        raw = m.group(1).strip()
    if len(raw) < 2 or raw[0] not in "{[" or raw[-1] not in "}]":
        return None
    try:
        data = json.loads(raw)
    except Exception:
        return None
    if isinstance(data, list):
        lines = [f"- {_json_value(x)}" for x in data[:100] if str(x).strip()]
        if len(data) > 100:
            lines.append(f"- … {len(data) - 100} more")
        return "\n".join(lines) if lines else None
    if not isinstance(data, dict):
        return None
    known = ("status", "summary", "findings", "files_changed", "files",
             "artifacts", "open_issues", "issues")
    if any(k in data for k in known):
        lines = []
        status, summary = data.get("status"), data.get("summary")
        head = " · ".join(x for x in (
            str(status).upper() if status else "",
            _json_scalar(summary) if summary else "") if x)
        if head:
            lines.append(head)
        for key, label in (("findings", "findings"),
                           ("files_changed", "files"), ("files", "files"),
                           ("artifacts", "artifacts"),
                           ("open_issues", "open issues"), ("issues", "issues")):
            val = data.get(key)
            if not val:
                continue
            if isinstance(val, (list, tuple)):
                vals = [_json_value(x) for x in val if str(x).strip()]
                if vals:
                    shown = ", ".join(vals[:25])
                    if len(vals) > 25:
                        shown += f" … +{len(vals) - 25}"
                    lines.append(f"**{label}:** {shown}")
            else:
                lines.append(f"**{label}:** {_json_value(val)}")
        for key, val in data.items():
            if key in known or val is None or val == "" or val == [] or val == {}:
                continue
            lines.append(f"**{key}:** {_json_value(val)}")
        return "\n\n".join(lines) if lines else None
    lines = []
    for key, val in data.items():
        if val is None or val == "" or val == [] or val == {}:
            continue
        if isinstance(val, list):
            lines.append(f"**{key}:**")
            for x in val[:50]:
                lines.append(f"- {_json_value(x)}")
            if len(val) > 50:
                lines.append(f"- … {len(val) - 50} more")
        elif isinstance(val, dict):
            lines.append(f"**{key}:** {_json_value(val)}")
        else:
            lines.append(f"**{key}:** {_json_scalar(val)}")
    return "\n".join(lines) if lines else None


def markdown_rows(text: str, width: int, max_code_lines: int = 80) -> list:
    """Render assistant markdown as ``[(text, style)]`` rows.

    Styles: ``""`` normal, ``head``, ``code``, ``border``. The goal is readable
    text on a 50-column phone, not a full CommonMark implementation.
    """
    width = max(12, int(width or 0))
    converted = json_to_markdown(text)
    if converted is not None:
        text = converted
    rows: list = []
    in_code, code_left = False, max_code_lines
    blank = True

    def add_blank():
        nonlocal blank
        if not blank:
            rows.append(("", ""))
            blank = True

    for raw in str(text or "").splitlines():
        line = raw.rstrip("\r\n")
        if line.strip().startswith("```"):
            if in_code:
                in_code = False
                code_left = max_code_lines
            else:
                add_blank()
                in_code, code_left = True, max_code_lines
            continue
        if in_code:
            if code_left <= 0:
                continue
            for chunk in wrap_display(line, width - 4, "  ", "  ", hard=True):
                rows.append((chunk, "code"))
            code_left -= 1
            blank = False
            continue
        if not line.strip():
            add_blank()
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", line)
        if m:
            add_blank()
            clean = strip_inline(m.group(2)).strip()
            for row in wrap_display(clean, width, "", ""):
                rows.append((row, "head"))
            blank = False
            continue
        if re.fullmatch(r"\s*([-*_])\1{2,}\s*", line):
            rows.append(("─" * min(width, 48), "border"))
            blank = False
            continue
        m = re.match(r"^(\s*)[-*+]\s+(.*)$", line)
        if m:
            clean = strip_inline(m.group(2)).strip()
            for i, row in enumerate(wrap_display(clean, width - 4, "", "")):
                rows.append((("  • " if i == 0 else "    ") + row, ""))
            blank = False
            continue
        m = re.match(r"^(\s*)(\d+[.)])\s+(.*)$", line)
        if m:
            label = m.group(2)
            clean = strip_inline(m.group(3)).strip()
            pad = " " * (len(label) + 1)
            for i, row in enumerate(wrap_display(clean, width - len(label) - 1,
                                                  "", "")):
                rows.append(((f"{label} " if i == 0 else pad) + row, ""))
            blank = False
            continue
        clean = strip_inline(line).strip()
        if not clean:
            add_blank()
            continue
        for row in wrap_display(clean, width, "", ""):
            rows.append((row, ""))
        blank = False
    while rows and rows[-1][0] == "":
        rows.pop()
    return rows


# ------------------------------------------------------------------ tools

def _one(v, n=120) -> str:
    if isinstance(v, (dict, list)):
        try:
            v = json.dumps(v, ensure_ascii=False)
        except Exception:
            v = str(v)
    return short(v, n)


def tool_summary(name: str, args) -> str:
    """One clean line describing a tool call — never raw JSON for known tools."""
    a = args if isinstance(args, dict) else {}
    name = str(name or "tool")
    if name == "bash":
        cmd = _one(a.get("cmd") or a.get("command"), 110)
        return ("& " if a.get("background") else "") + (cmd or "(empty command)")
    if name == "python":
        return _one(a.get("code") or a.get("cmd"), 110) or "(empty)"
    if name in ("read_file", "view_image"):
        return short_path(a.get("path"), a.get("cwd"))
    if name in ("write_file", "edit_file", "apply_patch", "create_file"):
        p = short_path(a.get("path") or a.get("file"), a.get("cwd"))
        if name == "apply_patch" and not p:
            return "patch"
        return p or "(no path)"
    if name == "ls":
        return short_path(a.get("path") or ".", a.get("cwd"))
    if name == "glob":
        return f'{a.get("pattern") or "*"}  {short_path(a.get("path") or ".", a.get("cwd"))}'
    if name == "grep":
        pat = _one(a.get("pattern"), 60)
        p = short_path(a.get("path") or ".", a.get("cwd"))
        return f"{pat!r}  {p}"
    if name == "repo_map":
        return short_path(a.get("path") or ".", a.get("cwd"))
    if name == "web_search":
        return '"' + _one(a.get("query"), 90) + '"'
    if name in ("web_fetch", "http", "browser_open", "browser_read"):
        return _one(a.get("url"), 110)
    if name == "browser_act":
        return _one(a.get("action") or a.get("url"), 90)
    if name == "spawn_agent":
        role = a.get("role") or "worker"
        return f"[{role}] " + (_one(a.get("task"), 90) or "(no task)")
    if name == "spawn_swarm":
        tasks = a.get("tasks") or []
        return f"{len(tasks)} parallel tasks · " + _one(a.get("role") or "worker", 20)
    if name in ("agent_status", "agent_wait", "agent_kill", "agent_result"):
        ids = a.get("ids") or a.get("id") or "all"
        return _one(ids, 80)
    if name == "agent_send":
        return f'{a.get("id")}: ' + _one(a.get("message"), 70)
    if name == "todo":
        return "update todos" if a.get("items") is not None else "read todos"
    if name == "note":
        return _one(a.get("key"), 50)
    if name == "recall":
        return _one(a.get("key") or "all", 50)
    if name == "skill":
        return _one(a.get("name") or "(list)", 50)
    if name == "finish":
        return _one(a.get("status") or "done", 20)
    if name == "ask_models":
        return f'{a.get("mode") or "vote"} · ' + _one(a.get("prompt") or "", 70)
    if name == "reload_tools":
        return "reload plugins"
    if name == "job_status":
        return _one(a.get("id") or "all", 40)
    if name == "job_output":
        return _one(a.get("id"), 40)
    if name == "job_kill":
        return _one(a.get("id") or "all", 40)
    # unknown / plugin tool: compact, bounded JSON is acceptable here
    return _one(a, 100) or "(no args)"


def _clean_preview(preview: str) -> str:
    s = str(preview or "")
    s = re.sub(r"</?untrusted[^>]*>", "", s)
    s = s.strip()
    return s


def result_brief(name: str, preview: str, *, chars=None, ms=None,
                 lines=None, width=70) -> tuple:
    """Return ``(ok, text)`` for a tool result row. ``ok`` is bool."""
    name = str(name or "tool")
    raw = _clean_preview(preview)
    first = next((ln.strip() for ln in raw.splitlines() if ln.strip()), "")
    ok = not first.upper().startswith(("ERROR", "TRACEBACK", "FAILED",
                                       "INTERRUPTED:", "CANCELLED"))
    suffix = ""
    if ms is not None:
        try:
            if int(ms) >= 100:
                suffix = f" · {int(ms) / 1000:.1f}s"
        except Exception:
            pass
    if name in ("web_search", "web_fetch", "http", "browser_read"):
        n = int(chars or len(raw) or 0)
        text = f"{n:,} chars" if n else (first or "done")
        return ok, short(text, max(20, width - len(suffix))) + suffix
    if name == "read_file":
        n = int(lines or (raw.count("\n") + 1 if raw else 0))
        return ok, short(f"{n} lines" if n else first or "done",
                         max(20, width - len(suffix))) + suffix
    if name in ("write_file", "edit_file", "apply_patch", "create_file"):
        return ok, ("written" if ok else short(first, width)) + suffix
    if name == "finish":
        return True, "recorded" + suffix
    if name == "todo":
        return ok, short(first or "updated", max(20, width - len(suffix))) + suffix
    if not first:
        return ok, ("(no output)" + suffix)
    return ok, short(first, max(20, width - len(suffix))) + suffix


def is_error_text(text: str) -> bool:
    return str(text or "").strip().upper().startswith(
        ("ERROR", "TRACEBACK", "FAILED", "INTERRUPTED:", "CANCELLED"))


def rel_files(paths, cwd=None, limit=8) -> list:
    out, seen = [], set()
    for p in paths or []:
        s = short_path(p, cwd)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
        if len(out) >= limit:
            break
    return out
