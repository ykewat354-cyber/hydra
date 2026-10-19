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


def check(rt, agent) -> dict:
    rep = agent.report or {}
    # Deterministic checks come first: a reviewer that cannot run the server
    # should not be able to pass a page whose CSS/JS is missing or whose
    # localhost URL is already dead.
    try:
        pre = deterministic_issues(agent)
    except Exception:
        pre = []
    if pre:
        if rt.journal:
            try:
                rt.journal.event("verify", agent.id, verdict="fail",
                                 model="deterministic", issues=pre[:10])
            except Exception:
                pass
        return {"verdict": "fail", "issues": pre, "model": "deterministic",
                "issue_text": _issue_text(pre)}
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

    claim = (
        f"TASK:\n{agent.task[:1500]}\n\n"
        f"FINAL SUMMARY:\n{str(rep.get('summary') or '')[:1500]}\n\n"
        f"FILES CHANGED:\n" + "\n".join(rep.get("files_changed") or ["(none)"]) +
        "\n\nCOMMANDS RUN:\n" + "\n".join(rep.get("commands_run") or ["(none)"]) +
        "\n\nOPEN ISSUES:\n" + "\n".join(rep.get("open_issues") or ["(none)"]) +
        "\n\nARTIFACTS:\n" + str(rep.get("artifacts") or "(none)")[:2000] +
        "\n\nARTIFACT CONTENTS (read from disk; a summary is not a deliverable):\n" +
        ("\n\n".join(artifact_evidence) or "(none readable)") +
        "\n\nEVIDENCE (recent tool calls with outputs, oldest first):\n" +
        ("\n\n".join(evidence) or "(none)"))
    prompt = base + "\n\n" + claim
    from . import jsonroute
    try:
        res = jsonroute.structured_chat(
            rt.router, [{"role": "user", "content": prompt}], role="reviewer",
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
                             issues=clean)
        except Exception:
            pass
    return {"verdict": verdict, "issues": issues, "model": reply.model,
            "issue_text": _issue_text(issues)}


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
