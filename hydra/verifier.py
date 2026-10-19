"""Independent verification of finish claims (§5.3).

`verify = "auto"` runs a reviewer call when files changed (or acceptance
criteria exist). The reviewer uses a different model family than the producer
when the chain allows (§7.4 diversity rule) and answers with JSON:
``{"verdict": "pass"|"fail", "issues": [{"what","fix"} | "..."]}``.

The verifier never blocks completion: parse failures count as pass, and a
verifier crash is reported but not fatal.
"""
from __future__ import annotations

import re
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


def check(rt, agent) -> dict:
    rep = agent.report or {}
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
