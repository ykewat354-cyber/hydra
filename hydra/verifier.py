"""Independent verification of finish claims (§5.3).

`verify = "auto"` runs a reviewer call when files changed (or acceptance
criteria exist). The reviewer uses a different model family than the producer
when the chain allows (§7.4 diversity rule) and answers with JSON:
``{"verdict": "pass"|"fail", "issues": [{"what","fix"} | "..."]}``.

The verifier never blocks completion: parse failures count as pass, and a
verifier crash is reported but not fatal.
"""
from __future__ import annotations

import html.parser
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import config as C
from .agent import load_prompt
from .ui_format import issue_text, short as _short

# §19.10 structured output: tool-less verifier calls use a JSON schema.
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["pass", "fail"]},
        "issues": {"type": "array", "items": {
            "type": "object",
            "properties": {"what": {"type": "string"}, "fix": {"type": "string"}},
            "required": ["what"]}},
    },
    "required": ["verdict"],
}


def should_verify(cfg: dict, agent) -> bool:
    mode = str(C.get(cfg, "loop.verify", "auto") or "auto").lower()
    if mode == "never":
        return False
    if mode == "always":
        return True
    rep = agent.report or {}
    if rep.get("files_changed") or rep.get("artifacts"):
        return True
    # A claim of HTTP 200 / passing tests / a removed string needs checking
    # even when the run touched no file (A2/A3).
    try:
        if extract_claims(rep):
            return True
    except Exception:
        pass
    if agent.data.get("acceptance"):
        return True
    # Research/report/list tasks must be checked even when the agent forgot to
    # write a file: that is exactly the failure the verifier has to catch.
    task = str(getattr(agent, "task", "") or "").lower()
    research = ("research", "survey", "landscape", "comparison",
                "deep dive", "list", "projects", "resources",
                "options", "market", "top 10", "top 20", "100", "200")
    return any(w in task for w in research)


def _pick_model(rt, producer_model):
    chain = rt.router.chain_for("reviewer", need_tools=False) or \
        rt.router.chain_for("verifier", need_tools=False) or \
        rt.router.chain_for("lead")
    for m in chain:
        if m != producer_model:
            return m
    return chain[0] if chain else None


def _artifact_stats(text: str) -> str:
    """Structural facts the reviewer can use without reading the whole file."""
    lines = text.splitlines()
    numbered = [re.sub(r"^\s*\d+[.)]\s*", "", ln).strip()
                for ln in lines if re.match(r"^\s*\d+[.)]\s+", ln)]
    bullets = [re.sub(r"^\s*[-*+]\s+", "", ln).strip()
               for ln in lines if re.match(r"^\s*[-*+]\s+", ln)]
    items = numbered or bullets
    bits = [f"{len(lines)} lines", f"{len(text)} chars"]
    if numbered:
        bits.append(f"{len(numbered)} numbered items")
    elif bullets:
        bits.append(f"{len(bullets)} bullet items")
    if items:
        norm = [re.sub(r"[^a-z0-9]+", " ", x.lower()).strip() for x in items]
        uniq = len({x for x in norm if x})
        bits.append(f"{uniq} unique")
        if uniq < len(norm):
            seen, dup = set(), []
            for original, key in zip(items, norm):
                if key and key in seen and len(dup) < 3:
                    dup.append(original)
                seen.add(key)
            if dup:
                bits.append("duplicates: " + " | ".join(_short(x, 60) for x in dup))
    return " · ".join(bits)


def _issue_text(issues) -> str:
    out = []
    for it in issues or []:
        out.append("- " + issue_text(it))
    return "\n".join(out)


class _RefParser(html.parser.HTMLParser):
    """Collect the tags and local asset references of a generated page."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tags = set()
        self.refs = []

    def handle_starttag(self, tag, attrs):
        tag = str(tag or "").lower()
        self.tags.add(tag)
        if tag not in ("link", "script", "img"):
            return
        for key, value in attrs:
            if str(key).lower() in ("href", "src") and value:
                self.refs.append((tag, str(value).strip()))


def _artifact_path(agent, rel):
    p = Path(str(rel)).expanduser()
    if not p.is_absolute():
        p = Path(agent.workdir) / p
    return p.resolve()


def _local_asset_issue(workdir, html_path, tag, ref):
    if not ref:
        return None
    low = ref.lower()
    if low.startswith(("#", "http://", "https://", "//", "data:",
                       "javascript:", "mailto:", "tel:")):
        return None
    # Template expressions and query strings are not filesystem paths.
    if any(x in ref for x in ("{{", "{%", "${")):
        return None
    clean = ref.split("?", 1)[0].split("#", 1)[0].strip()
    if not clean:
        return None
    try:
        if clean.startswith("/"):
            # A static server may use either the page's directory or the
            # project workdir as its document root; accept both.
            targets = [(html_path.parent / clean.lstrip("/")).resolve(),
                       (Path(workdir) / clean.lstrip("/")).resolve()]
        else:
            targets = [(html_path.parent / clean).resolve()]
    except Exception:
        return None
    if any(t.is_file() for t in targets):
        return None
    return {
        "what": f"{html_path.name} references missing local asset {ref!r}",
        "fix": f"create {targets[0]} or correct the {tag} reference",
    }


def _html_issues(workdir, rel, path, text):
    issues = []
    if not text.strip():
        return [{"what": f"{rel} is empty", "fix": "write the page before finishing"}]
    parser = _RefParser()
    try:
        parser.feed(text)
        parser.close()
    except Exception as exc:
        return [{"what": f"{rel} could not be parsed as HTML: {exc}",
                 "fix": "fix the HTML structure"}]
    missing = [t for t in ("html", "body") if t not in parser.tags]
    if missing:
        tags = ", ".join("<" + t + ">" for t in missing)
        issues.append({
            "what": f"{rel} is missing {tags} structure",
            "fix": "write a complete HTML document with html/head/body tags",
        })
    for tag, ref in parser.refs:
        issue = _local_asset_issue(workdir, path, tag, ref)
        if issue:
            issues.append(issue)
    return issues


def _css_issues(rel, text):
    if not text.strip():
        return [{"what": f"{rel} is empty", "fix": "write the stylesheet"}]
    if text.count("{") != text.count("}"):
        return [{"what": f"{rel} has unbalanced CSS braces",
                 "fix": "fix the CSS syntax"}]
    return []


def _js_issues(rel, path, text):
    if not text.strip():
        return [{"what": f"{rel} is empty", "fix": "write the script"}]
    node = shutil.which("node")
    if not node:
        return []
    try:
        proc = subprocess.run([node, "--check", str(path)],
                              capture_output=True, text=True, timeout=5)
    except Exception:
        return []
    if proc.returncode == 0:
        return []
    detail = (proc.stderr or proc.stdout or "").strip().splitlines()
    tail = detail[-1] if detail else f"node --check exit {proc.returncode}"
    return [{"what": f"{rel} has a JavaScript syntax error: {tail[:240]}",
             "fix": "fix the JavaScript syntax before finishing"}]


def _url_issues(agent, has_html):
    """Catch a false 'hosted at localhost' claim (the gym-site failure)."""
    if not has_html:
        return []
    rep = agent.report or {}
    text = " ".join([str(rep.get("summary") or "")] +
                    [str(x) for x in (rep.get("findings") or [])])
    urls = re.findall(
        r"https?://(?:localhost|127\.0\.0\.1)(?::\d{2,5})?(?:/[^\s)\]}\"'<>]*)?",
        text, re.I)
    issues, seen = [], set()
    for url in urls:
        url = url.rstrip(".,;:")
        if not url or url in seen:
            continue
        seen.add(url)
        try:
            with urllib.request.urlopen(url, timeout=2) as res:
                code = int(getattr(res, "status", 200) or 200)
            if code >= 400:
                issues.append({"what": f"claimed URL {url} returned HTTP {code}",
                               "fix": "start the server with background=true and verify it"})
        except Exception as exc:
            issues.append({
                "what": f"claimed URL {url} is not reachable: "
                        f"{type(exc).__name__}: {exc}",
                "fix": "start the server with background=true, then verify the URL",
            })
        if len(issues) >= 2:
            break
    return issues


def _pending_todo_issues(agent) -> list:
    """A `done` finish with unfinished todos is not done (gym-3D run: 0/4)."""
    rep = agent.report or {}
    if str(rep.get("status") or "done") != "done":
        return []
    pending = []
    for t in (agent.data.get("todos") or []):
        s = str(t).strip()
        if s and not s.lower().startswith("x "):
            pending.append(s)
    if not pending:
        return []
    shown = "; ".join(p[:80] for p in pending[:3])
    more = f" (+{len(pending) - 3} more)" if len(pending) > 3 else ""
    return [{
        "what": f"finish status is done but {len(pending)} todo item(s) are "
                f"still open: {shown}{more}",
        "fix": "finish the work and mark each item 'x <item>' (or set status "
               "partial and list it in open_issues)",
    }]


def _dir_has_files(path: Path) -> bool:
    try:
        for _root, dirs, files in os.walk(path):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules")]
            if files:
                return True
    except Exception:
        return False
    return False


_DUP_ASSET_PAIRS = (
    ("assets/css", "css"), ("assets/css", "styles"), ("assets/js", "js"),
    ("assets/js", "scripts"), ("assets/img", "img"), ("assets/img", "images"),
    ("assets/images", "images"), ("assets/fonts", "fonts"),
)


def _duplicate_asset_issues(workdir, html_path) -> list:
    """Two live copies of the same asset tree confuse every later edit."""
    try:
        root = Path(html_path).parent
    except Exception:
        return []
    issues = []
    for a, b in _DUP_ASSET_PAIRS:
        pa, pb = root / a, root / b
        try:
            if pa.is_dir() and pb.is_dir() and _dir_has_files(pa) and _dir_has_files(pb):
                issues.append({
                    "what": f"{root.name}/ keeps two live asset trees: "
                            f"{a!r} and {b!r}",
                    "fix": "keep the one the HTML actually references and "
                           "merge/delete the stale copy before finishing",
                })
        except Exception:
            continue
    return issues


def deterministic_issues(agent) -> list:
    """Cheap, offline checks that run before the LLM reviewer.

    Missing files are left to the existing reviewer so fake/scripted runs keep
    their current behavior.  Existing code artifacts and a claimed localhost
    URL are checked here because an LLM review can miss a dead server or a
    broken HTML asset link (the gym-site false pass).  Pending todos and
    duplicate asset trees are checked because the 3D gym run finished 0/4 and
    left assets/ and css/ side by side.
    """
    rep = agent.report or {}
    paths = list(dict.fromkeys(
        [str(x) for x in (rep.get("artifacts") or [])] +
        [str(x) for x in (rep.get("files_changed") or [])]))
    issues = _pending_todo_issues(agent)
    checked, has_html = 0, False
    for rel in paths:
        if not rel:
            continue
        try:
            p = _artifact_path(agent, rel)
            if not p.is_file():
                continue
            text = p.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        suffix = p.suffix.lower()
        if suffix in (".html", ".htm"):
            has_html = True
            issues.extend(_html_issues(agent.workdir, rel, p, text))
            issues.extend(_duplicate_asset_issues(agent.workdir, p))
        elif suffix == ".css":
            issues.extend(_css_issues(rel, text))
        elif suffix == ".js":
            issues.extend(_js_issues(rel, p, text))
        else:
            continue
        checked += 1
        if checked >= 8:
            break
    if has_html:
        issues.extend(_url_issues(agent, has_html))
    return issues[:10]


# ---------------------------------------------------------------- WO3 A2/A3
_TEXT_SUFFIXES = {
    ".html", ".htm", ".css", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".js",
    ".json", ".md", ".txt", ".py", ".sh", ".toml", ".yaml", ".yml", ".xml",
    ".svg", ".sql", ".rs", ".go", ".java", ".c", ".h", ".cpp",
}
_STOP = {"the", "this", "that", "old", "new", "it", "file", "files", "code",
         "script", "line", "lines", "word", "text", "version", "page"}
_HTTP_URL = re.compile(
    r"https?://(?:localhost|127\.0\.0\.1)(?::\d{2,5})?(?:/[^\s)\]}\"'<>]*)?",
    re.I)
_PATHISH = re.compile(r"[A-Za-z0-9_./~-]+\.[A-Za-z0-9]{1,8}")
_CODEISH = re.compile(r"^[A-Z0-9_][A-Z0-9_.:/+@-]{1,60}$")


def _claim_text(rep: dict) -> str:
    return "\n".join([str(rep.get("summary") or "")] +
                     [str(x) for x in (rep.get("findings") or [])])


def _targets(text: str) -> list:
    """File names named by a claim, e.g. 'removed FIR from js/script.js'."""
    out = []
    for raw in _PATHISH.findall(str(text)):
        t = raw.strip("./").rstrip(".,;:")
        if "/" in t or t.lower().endswith(tuple(_TEXT_SUFFIXES)):
            if t and t not in out:
                out.append(t)
    return out[:4]


def _same_target(rel: str, target: str) -> bool:
    a = Path(str(rel)).as_posix().lstrip("./")
    b = Path(str(target)).as_posix().lstrip("./")
    return a == b or a.endswith("/" + b) or b.endswith("/" + a)


def _add_claim(out: list, kind: str, token: str, text: str) -> None:
    token = str(token or "").strip().strip("'\"`“”.,;:")
    if token.lower() in _STOP:
        return
    if not token and kind not in ("http200", "tests", "build"):
        return
    if kind in ("removed", "added") and not (
            _CODEISH.match(token) or "." in token or "/" in token):
        return
    key = (kind, token.lower())
    if any((c["kind"], c["token"].lower()) == key for c in out):
        return
    targets = [token] if kind == "exists" else _targets(text)
    out.append({"kind": kind, "token": token, "text": text[:160],
                "targets": targets})


def extract_claims(rep: dict) -> list:
    """Concrete claims only: quoted/code-like tokens and path-like files."""
    text = _claim_text(rep)
    out: list = []
    removed = re.compile(
        r"(?:removed|deleted|eliminated|no longer\s+(?:contains|has|shows|includes)"
        r"|hata\s+diya|hata\s+di)\s+(?:the\s+)?(?:word|text|string|token|line|code)?"
        r"\s*(['\"`“]?)([A-Za-z0-9_][\w.:/+@-]{1,60})\1", re.I)
    for m in removed.finditer(text):
        ctx = text[max(0, m.start() - 60):m.end() + 160]
        _add_claim(out, "removed", m.group(2), ctx)
    for m in re.finditer(
            r"(['\"`“])([^'\"`”\n]{2,60})\1\s+(?:was|is|has\s+been)\s+"
            r"(?:removed|deleted|gone)", text, re.I):
        ctx = text[max(0, m.start() - 60):m.end() + 160]
        _add_claim(out, "removed", m.group(2), ctx)
    for m in re.finditer(
            r"([A-Z0-9_][A-Z0-9_.:/-]{1,60})\s+(?:was\s+|is\s+)?"
            r"(?:removed|hata|hataya)", text):
        ctx = text[max(0, m.start() - 60):m.end() + 160]
        _add_claim(out, "removed", m.group(1), ctx)
    added = re.compile(
        r"(?:added|includes?|contains?|shows?)\s+(?:the\s+)?"
        r"(?:word|text|string|token|line|code)?\s*"
        r"(['\"`“])([^'\"`”\n]{2,60})\1", re.I)
    for m in added.finditer(text):
        ctx = text[max(0, m.start() - 60):m.end() + 160]
        _add_claim(out, "added", m.group(2), ctx)
    for m in re.finditer(
            r"([A-Z0-9_][A-Z0-9_.:/-]{1,60})\s+(?:was\s+|is\s+)?"
            r"(?:added|included)", text):
        ctx = text[max(0, m.start() - 60):m.end() + 160]
        _add_claim(out, "added", m.group(1), ctx)
    for m in re.finditer(
            r"(?:created|wrote|added)\s+(?:a\s+|the\s+)?"
            r"([A-Za-z0-9_./~-]+\.[A-Za-z0-9]{1,8})", text, re.I):
        _add_claim(out, "exists", m.group(1), m.group(0))
    for m in re.finditer(
            r"([A-Za-z0-9_./~-]+\.[A-Za-z0-9]{1,8})\s+"
            r"(?:was\s+|is\s+)?(?:created|exists|written|saved)", text, re.I):
        _add_claim(out, "exists", m.group(1), m.group(0))
    urls = _HTTP_URL.findall(text)
    server_words = re.search(r"\b(?:serve[sd]?|serving|running|hosted|available|"
                             r"up at|live at|URL|http)\b", text, re.I)
    if urls and server_words:
        _add_claim(out, "http200", urls[0].rstrip(".,;:"), urls[0])
    elif re.search(r"\b(?:http|HTTP)\s*200\b|\bstatus\s*[:=]?\s*200\b", text):
        _add_claim(out, "http200", "", "HTTP 200")
    if re.search(r"\b(?:tests?|pytest|unittest|suite)\b[^\n]{0,40}"
                 r"\b(?:pass|passed|green|all good)\b", text, re.I) or \
            re.search(r"\b\d+\s+tests?\s+passed\b", text, re.I):
        _add_claim(out, "tests", "", "tests pass")
    if re.search(r"\bbuild\b[^\n]{0,30}\b(?:ok|pass|passed|success|succeeded)\b",
                 text, re.I) or "compiled successfully" in text.lower():
        _add_claim(out, "build", "", "build ok")
    return out


def _changed_text_files(agent) -> list:
    rep = agent.report or {}
    out, seen = [], set()
    for rel in list(rep.get("files_changed") or []) + \
            list(rep.get("artifacts") or []):
        try:
            p = _artifact_path(agent, rel)
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            if p.suffix.lower() not in _TEXT_SUFFIXES:
                continue
            out.append((str(rel), p,
                        p.read_text(encoding="utf-8", errors="replace")))
        except Exception:
            continue
    return out


def _token_re(token: str):
    flags = 0 if any(c.isupper() for c in token) else re.I
    return re.compile(r"(?<![\w])" + re.escape(token) + r"(?![\w])", flags)


def _file_claim_issues(agent, claim: dict, files: list) -> list:
    kind, token = claim["kind"], claim["token"]
    if kind == "exists":
        try:
            p = _artifact_path(agent, token)
        except Exception:
            p = _artifact_path(agent, token)
        if not p.is_file() or p.stat().st_size <= 0:
            return [{"what": f"claim: created '{token}'; file is missing or empty",
                     "fix": f"write {token} with real content and verify it"}]
        return []
    rep = agent.report or {}
    if rep.get("files_changed") is None:
        return [{"what": f"claim: {kind} '{token}'; files_changed is not tracked "
                         f"(the change scan was incomplete)",
                 "fix": "make the change through a tool or a project-root shell "
                        "command so the report can list it"}]
    targets = [t for t in (claim.get("targets") or []) if t]
    if targets:
        picked = [(rel, p, txt) for rel, p, txt in files
                  if any(_same_target(rel, t) for t in targets)]
        if not picked:
            # The claim names a file that the scan did not list: check it
            # directly so a wrong name is caught instead of ignored.
            for t in targets:
                try:
                    p = _artifact_path(agent, t)
                    if p.is_file() and p.suffix.lower() in _TEXT_SUFFIXES:
                        picked.append((t, p, p.read_text(encoding="utf-8",
                                                         errors="replace")))
                except Exception:
                    continue
        if not picked:
            return [{"what": f"claim: {kind} '{token}' in '{targets[0]}'; that "
                             f"file is missing or not a changed text file",
                     "fix": f"make and verify the change in {targets[0]}"}]
        files = picked
    if not files:
        return [{"what": f"claim: {kind} '{token}'; no changed text file to prove it",
                 "fix": "make the change in a tracked file (write_file/edit_file)"}]
    rx = _token_re(token)
    hits = []
    for rel, _p, text in files:
        m = rx.search(text)
        if m:
            line = text[:m.start()].count("\n") + 1
            hits.append(f"{rel}:{line}")
    if kind == "removed":
        if hits:
            return [{"what": f"claim: removed '{token}'; still found in "
                             + ", ".join(hits[:3]),
                     "fix": f"remove '{token}' from every changed file, then verify"}]
        return []
    if kind == "added":
        if hits:
            return []
        return [{"what": f"claim: added '{token}'; not found in any changed file",
                 "fix": f"add '{token}' to a changed file and verify"}]
    return []


def _tool_rows(agent) -> list:
    rows = []
    for msg in getattr(agent, "messages", []) or []:
        if msg.get("role") != "tool":
            continue
        text = str(msg.get("content") or "")
        if not text.strip():
            continue
        rows.append({"tool": str(msg.get("tool_name") or ""), "text": text,
                     "ev": msg.get("ev"), "turn": msg.get("turn"),
                     "changed": bool(msg.get("changed"))})
    return rows


def _http_status(text: str):
    if re.search(r"HTTP/[\d.]+\s+200\b", text) or \
            re.search(r"['\"]status['\"]\s*:\s*200\b", text) or \
            re.search(r"\bhttp_code\s*[:=]?\s*200\b", text, re.I) or \
            text.strip() == "200" or \
            re.search(r"\bstatus\s*=\s*200\b", text):
        return "ok"
    if re.search(r"curl:\s*\(\d+\)", text) or \
            re.search(r"HTTP/[\d.]+\s+[45]\d\d\b", text) or \
            re.search(r"['\"]status['\"]\s*:\s*[45]\d\d\b", text):
        return "fail"
    return None


def _tests_status(text: str):
    if re.search(r"\b\d+\s+passed\b", text, re.I) or \
            re.search(r"^OK\b", text, re.M) or \
            re.search(r"\bAll tests passed\b", text, re.I) or \
            re.search(r"\b0\s+failed\b", text, re.I):
        return "ok"
    if re.search(r"\b\d+\s+failed\b", text, re.I) or \
            re.search(r"^FAILED\b", text, re.M) or \
            re.search(r"\b\d+\s+errors?\b", text, re.I):
        return "fail"
    return None


def _build_status(text: str):
    low = text.lower()
    if "build succeeded" in low or "compiled successfully" in low or \
            re.search(r"\bbuilt in\b", low):
        return "ok"
    if re.search(r"build\s+failed", low) or re.search(r"\b[1-9]\d*\s+errors?\b", low):
        return "fail"
    return None


def _latest(rows: list, kind: str, start: int = 0):
    """Latest matching evidence as (status, row, index)."""
    last = None
    for i in range(max(0, start), len(rows)):
        row = rows[i]
        status = {"http200": _http_status, "tests": _tests_status,
                  "build": _build_status}.get(kind, lambda _t: None)(row["text"])
        if status:
            last = (status, row, i)
    return last


def _live_get(url: str):
    try:
        with urllib.request.urlopen(url, timeout=2) as res:
            return int(getattr(res, "status", 200) or 200)
    except Exception:
        return 0


def _server_jobs(agent) -> list:
    rt = getattr(agent, "runtime", None)
    out = []
    for jid, job in (getattr(rt, "jobs", {}) or {}).items() if rt else []:
        if job.get("agent") not in (None, getattr(agent, "id", None)):
            continue
        port = job.get("port")
        if not port:
            continue
        try:
            alive = job.get("proc") is not None and job["proc"].poll() is None
        except Exception:
            alive = False
        out.append({"id": str(jid), "port": int(port),
                    "url": job.get("url") or f"http://127.0.0.1:{port}/",
                    "state": "running" if alive else "exited",
                    "served_url": str(job.get("served_url") or "")})
    return out


def audit_claims(agent) -> dict:
    """A2/A3: map concrete finish claims to evidence (latest success wins)."""
    rep = agent.report or {}
    claims = extract_claims(rep)
    files = _changed_text_files(agent)
    rows = _tool_rows(agent)
    last_change = -1
    for i, row in enumerate(rows):
        if row["changed"]:
            last_change = i
    issues, evidence, served_url = [], [], ""
    for claim in claims[:12]:
        if claim["kind"] in ("removed", "added", "exists"):
            issues.extend(_file_claim_issues(agent, claim, files))
            continue
        if claim["kind"] == "http200":
            url = claim["token"]
            latest = _latest(rows, "http200", 0)
            last = latest if latest is not None and latest[2] >= last_change else None
            if last is not None and last[0] == "fail":
                # A later failure invalidates an earlier success, even if some
                # unrelated process now answers on the same port.
                issues.append({
                    "what": "claim: HTTP 200; latest evidence is a failure ("
                            + last[1]["text"][:80].replace("\n", " ") + ")",
                    "fix": "restart/fix the server and curl it again after the "
                           "last change"})
                continue
            if last is not None and last[0] == "ok":
                if not url:
                    if last[1]["ev"]:
                        evidence.append(int(last[1]["ev"]))
                    continue
                code = _live_get(url)
                if code == 200:
                    served_url = url
                    if last[1]["ev"]:
                        evidence.append(int(last[1]["ev"]))
                    continue
                issues.append({
                    "what": f"claim: serves {url}; the last recorded check passed "
                            f"but the verifier GET returned "
                            f"{('HTTP %d' % code) if code else 'no response'}",
                    "fix": "keep the server alive with background=true and verify "
                           "the URL at finish"})
                continue
            if url:
                code = _live_get(url)
                if code == 200:
                    served_url = url
                    continue
                issues.append({
                    "what": f"claim: serves {url}; no evidence and the verifier "
                            f"GET returned {('HTTP %d' % code) if code else 'no response'}",
                    "fix": "start the server with background=true and curl it"})
                continue
            issues.append({"what": "claim: HTTP 200; no evidence after the "
                                   "last change",
                           "fix": "curl the URL and keep the 200 output"})
            continue
        if claim["kind"] in ("tests", "build"):
            latest = _latest(rows, claim["kind"], 0)
            last = latest if latest is not None and latest[2] >= last_change else None
            label = "tests pass" if claim["kind"] == "tests" else "build ok"
            if last is None:
                issues.append({"what": f"claim: {label}; no evidence after the "
                                       f"last change",
                               "fix": "run the tests/build after the last edit"})
            elif last[0] == "ok":
                if last[1]["ev"]:
                    evidence.append(int(last[1]["ev"]))
            else:
                issues.append({"what": f"claim: {label}; latest evidence is a "
                                       f"failure",
                               "fix": "fix the failure and run it again"})
    jobs = _server_jobs(agent)
    if not served_url:
        for job in reversed(jobs):
            if job["state"] != "running":
                continue
            if _live_get(job["url"]) == 200:
                served_url = job["url"]
            break
    if not served_url:
        # P1/A4: HYDRA probed the job URL right after job_start; use that
        # bounded, real GET even if the agent stopped the server before finish.
        for job in reversed(jobs):
            if job.get("served_url"):
                served_url = str(job["served_url"])
                break
    return {"issues": issues[:8], "evidence": sorted(set(evidence)),
            "served_url": served_url, "claims": claims, "jobs": jobs}


def _verify_cost(rt, reply) -> float:
    prices = (getattr(getattr(rt, "router", None), "prices", {}) or {}).get(
        getattr(reply, "model", ""), {}) or {}
    return ((int(getattr(reply, "prompt_eval_count", 0) or 0) *
             float(prices.get("in", 0) or 0)) +
            (int(getattr(reply, "eval_count", 0) or 0) *
             float(prices.get("out", 0) or 0))) / 1e6


def check(rt, agent) -> dict:
    t0 = time.time()
    rep = agent.report or {}
    rep["verify_tool_calls"] = 0   # tool-less by design: no ask_models/web
    rep["verify_rounds"] = int(rep.get("verify_rounds") or 0) + 1
    # Deterministic checks come first: a reviewer that cannot run the server
    # should not be able to pass a page whose CSS/JS is missing or whose
    # localhost URL is already dead.
    try:
        pre = deterministic_issues(agent)
    except Exception:
        pre = []
    audit = {"issues": [], "evidence": [], "served_url": "", "claims": []}
    try:
        audit = audit_claims(agent)
    except Exception:
        pass
    issues = list(pre) + list(audit.get("issues") or [])
    if audit.get("evidence"):
        rep["evidence"] = sorted(set(int(x) for x in audit["evidence"]))
    if audit.get("served_url"):
        rep["served_url"] = str(audit["served_url"])
    if audit.get("jobs"):
        rep["jobs"] = list(audit["jobs"])
    if issues:
        if rt.journal:
            try:
                rt.journal.event("verify", agent.id, verdict="fail",
                                 model="deterministic", issues=issues[:10],
                                 evidence=rep.get("evidence") or [],
                                 served_url=rep.get("served_url") or "",
                                 claims=audit.get("claims") or [])
            except Exception:
                pass
        return {"verdict": "fail", "issues": issues, "model": "deterministic",
                "issue_text": _issue_text(issues),
                "evidence": rep.get("evidence") or [],
                "served_url": rep.get("served_url") or ""}
    producer = agent.data.get("last_model")
    model = _pick_model(rt, producer)
    base = load_prompt("verifier") or (
        'You are an independent verifier. Reply with JSON only: '
        '{"verdict":"pass"|"fail","issues":[{"what":"...","fix":"..."}]}')
    evidence = []
    for msg in reversed(getattr(agent, "messages", []) or []):
        if msg.get("role") != "tool":
            continue
        name = str(msg.get("tool_name") or "tool")
        if name in ("finish", "todo", "note", "recall"):
            continue
        text = str(msg.get("content") or "").strip()
        if not text:
            continue
        evidence.append(f"[{name}]\n{text[:1200]}")
        if len(evidence) >= 6:
            break
    evidence.reverse()

    # The reviewer cannot read files itself (tool-less JSON call). Include a
    # bounded excerpt of the actual deliverables so a claimed list/report is
    # checked instead of trusted.
    artifact_evidence, seen = [], set()
    paths = list(rep.get("artifacts") or []) + list(rep.get("files_changed") or [])
    for rel in paths:
        try:
            p = Path(str(rel))
            if not p.is_absolute():
                p = Path(agent.workdir) / p
            p = p.resolve()
            if p in seen or not p.is_file():
                continue
            seen.add(p)
            text = p.read_text(encoding="utf-8", errors="replace")
            stats = _artifact_stats(text)
            if len(text) > 3000:
                excerpt = text[:2000] + "\n[... middle omitted ...]\n" + text[-1000:]
            else:
                excerpt = text
            artifact_evidence.append(f"[file {rel} · {stats}]\n" + excerpt)
        except Exception:
            continue
        if len(artifact_evidence) >= 3:
            break

    if rep.get("files_changed") is None:
        files_line = "(not tracked — the change scan was incomplete)"
    else:
        files_line = "\n".join(rep.get("files_changed") or ["(none)"])
    det = "pass" if audit.get("claims") else "(no concrete claims)"
    claim = (
        f"TASK:\n{agent.task[:1500]}\n\n"
        f"FINAL SUMMARY:\n{str(rep.get('summary') or '')[:1500]}\n\n"
        f"FILES CHANGED:\n" + files_line +
        "\n\nCOMMANDS RUN:\n" + "\n".join(rep.get("commands_run") or ["(none)"]) +
        "\n\nOPEN ISSUES:\n" + "\n".join(rep.get("open_issues") or ["(none)"]) +
        "\n\nARTIFACTS:\n" + str(rep.get("artifacts") or "(none)")[:2000] +
        "\n\nDETERMINISTIC CLAIM CHECK: " + det +
        " · claims=" + str([c.get("kind") + ":" + c.get("token", "")
                             for c in (audit.get("claims") or [])][:8]) +
        " · evidence_events=" + str(rep.get("evidence") or []) +
        " · served_url=" + str(rep.get("served_url") or "") +
        "\n\nARTIFACT CONTENTS (read from disk; a summary is not a deliverable):\n" +
        ("\n\n".join(artifact_evidence) or "(none readable)") +
        "\n\nEVIDENCE (recent tool calls with outputs, oldest first):\n" +
        ("\n\n".join(evidence) or "(none)"))
    prompt = base + "\n\n" + claim
    from . import jsonroute
    try:
        res = jsonroute.structured_chat(
            rt.router, [{"role": "user", "content": prompt}], role="reviewer",
            usage_role="verifier",
            schema=VERDICT_SCHEMA, schema_name="verdict", think="low",
            agent=agent, exclude=(producer,) if producer else (),
            model_hint=model)
    except Exception as exc:
        if rt.journal:
            try:
                rt.journal.event("verify", agent.id, verdict="skipped",
                                 error=f"{type(exc).__name__}: {exc}"[:200])
            except Exception:
                pass
        return {"verdict": "pass", "issues": [], "model": model,
                "note": f"verifier unavailable: {exc}"}
    if res.reply is None:
        if rt.journal:
            try:
                rt.journal.event("verify", agent.id, verdict="skipped",
                                 error=str(res.error)[:200])
            except Exception:
                pass
        return {"verdict": "pass", "issues": [], "model": model,
                "note": f"verifier unavailable: {res.error}"}
    reply = res.reply
    cost = _verify_cost(rt, reply)
    rep["verify_cost_usd"] = round(
        float(rep.get("verify_cost_usd") or 0) + cost, 6)
    rep["verify_ms"] = int((time.time() - t0) * 1000)
    rep["verify_model"] = getattr(reply, "model", "") or model
    limits = C.get(rt.cfg, "verification", {}) or {}
    rep["verify_budget_ok"] = bool(
        int(rep["verify_tool_calls"]) <= int(limits.get("max_tool_calls", 5) or 5)
        and float(rep["verify_cost_usd"]) <= float(limits.get("max_usd", 0.01) or 0.01)
        and rep["verify_ms"] <= int(limits.get("budget_s", 60) or 60) * 1000)
    obj = res.obj
    verdict, issues = "pass", []
    if isinstance(obj, dict):
        if str(obj.get("verdict") or "pass").lower() == "fail":
            verdict = "fail"
            issues = obj.get("issues") or []
    if rt.journal:
        try:
            clean = []
            for i in (issues or [])[:10]:
                if isinstance(i, dict):
                    clean.append({"what": str(i.get("what") or i)[:300],
                                  "fix": str(i.get("fix") or "")[:300]})
                else:
                    clean.append(str(i)[:300])
            rt.journal.event("verify", agent.id, verdict=verdict, model=reply.model,
                             issues=clean, evidence=rep.get("evidence") or [],
                             served_url=rep.get("served_url") or "",
                             calls=rep["verify_tool_calls"],
                             cost_usd=rep["verify_cost_usd"],
                             ms=rep["verify_ms"],
                             budget_ok=rep["verify_budget_ok"])
        except Exception:
            pass
    return {"verdict": verdict, "issues": issues, "model": reply.model,
            "issue_text": _issue_text(issues),
            "evidence": rep.get("evidence") or [],
            "served_url": rep.get("served_url") or "",
            "verify_cost_usd": rep["verify_cost_usd"],
            "verify_tool_calls": rep["verify_tool_calls"]}


def run_once(rt, agent) -> bool:
    """Verify after a finish. Returns True if the loop should continue (failed)."""
    if not should_verify(rt.cfg, agent):
        return False
    if int(getattr(agent, "verify_rounds_left", 0) or 0) <= 0:
        return False
    result = check(rt, agent)
    if result.get("verdict") != "fail":
        return False
    agent.verify_rounds_left -= 1
    text = result.get("issue_text") or "\n".join(
        str(i) for i in (result.get("issues") or [])) or "(no details)"
    if agent.verify_rounds_left <= 0:
        # Cap reached (§5.3): stop re-entering the loop. Keep the rejection in
        # the report so the user sees why the run ended partial.
        rep = agent.report if isinstance(agent.report, dict) else None
        if rep is not None:
            issues = rep.setdefault("open_issues", [])
            try:
                issues.append("verifier rejected the finish: " + text[:400])
            except Exception:
                pass
            if str(rep.get("status") or "") == "done":
                rep["status"] = "partial"
        if rt.journal:
            try:
                rt.journal.event("verify_cap", agent.id, model=result.get("model"))
            except Exception:
                pass
        agent.finished = True
        return False
    agent.finished = False
    agent.steer(
        "An independent reviewer REJECTED your finish (round "
        f"{result.get('model') or '?'}). Issues:\n{text}\n\n"
        "Fix the issues with tools and call finish again, or fix unverifiable "
        "claims. If the review itself is wrong, say why and finish again.")
    return True
