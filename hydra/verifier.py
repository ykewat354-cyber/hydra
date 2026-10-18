"""Independent verification of finish claims (§5.3).

`verify = "auto"` runs a reviewer call when files changed (or acceptance
criteria exist). The reviewer uses a different model family than the producer
when the chain allows (§7.4 diversity rule) and answers with JSON:
``{"verdict": "pass"|"fail", "issues": [{"what","fix"} | "..."]}``.

The verifier never blocks completion: parse failures count as pass, and a
verifier crash is reported but not fatal.
"""
from __future__ import annotations

from . import config as C
from .agent import load_prompt, parse_json_blob

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
    return bool(rep.get("files_changed")) or bool(agent.data.get("acceptance"))


def _pick_model(rt, producer_model):
    chain = rt.router.chain_for("reviewer", need_tools=False) or \
        rt.router.chain_for("lead")
    for m in chain:
        if m != producer_model:
            return m
    return chain[0] if chain else None


def _issue_text(issues) -> str:
    out = []
    for it in issues or []:
        if isinstance(it, dict):
            what = str(it.get("what") or it.get("issue") or it)
            fix = str(it.get("fix") or "").strip()
            out.append(f"- {what}" + (f" (fix: {fix})" if fix else ""))
        else:
            out.append(f"- {it}")
    return "\n".join(out)


def check(rt, agent) -> dict:
    rep = agent.report or {}
    producer = agent.data.get("last_model")
    model = _pick_model(rt, producer)
    base = load_prompt("verifier") or (
        'You are an independent verifier. Reply with JSON only: '
        '{"verdict":"pass"|"fail","issues":[{"what":"...","fix":"..."}]}')
    claim = (
        f"TASK:\n{agent.task[:1500]}\n\n"
        f"FINAL SUMMARY:\n{str(rep.get('summary') or '')[:1500]}\n\n"
        f"FILES CHANGED:\n" + "\n".join(rep.get("files_changed") or ["(none)"]) +
        "\n\nCOMMANDS RUN:\n" + "\n".join(rep.get("commands_run") or ["(none)"]) +
        "\n\nOPEN ISSUES:\n" + "\n".join(rep.get("open_issues") or ["(none)"]))
    prompt = base + "\n\n" + claim
    try:
        reply = rt.router.chat([{"role": "user", "content": prompt}],
                               role="reviewer", model=model, agent=agent,
                               think="low", format=VERDICT_SCHEMA)
    except Exception as exc:
        if rt.journal:
            try:
                rt.journal.event("verify", agent.id, verdict="skipped",
                                 error=f"{type(exc).__name__}: {exc}"[:200])
            except Exception:
                pass
        return {"verdict": "pass", "issues": [], "model": model,
                "note": f"verifier unavailable: {exc}"}
    obj = parse_json_blob(reply.content or "")
    verdict, issues = "pass", []
    if isinstance(obj, dict):
        if str(obj.get("verdict") or "pass").lower() == "fail":
            verdict = "fail"
            issues = obj.get("issues") or []
    if rt.journal:
        try:
            rt.journal.event("verify", agent.id, verdict=verdict, model=reply.model,
                             issues=[str(i)[:300] for i in issues][:10])
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
    agent.finished = False
    text = result.get("issue_text") or "\n".join(
        str(i) for i in (result.get("issues") or [])) or "(no details)"
    agent.steer(
        "An independent reviewer REJECTED your finish (round "
        f"{result.get('model') or '?'}). Issues:\n{text}\n\n"
        "Fix the issues with tools and call finish again, or fix unverifiable "
        "claims. If the review itself is wrong, say why and finish again.")
    return True
