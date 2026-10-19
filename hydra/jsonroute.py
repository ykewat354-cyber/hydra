"""F1 — JSON-role routing for tool-less structured calls.

Every structured call (planner DAG, verifier verdict, summarizer digest,
report normalizer) used to follow the role chain.  F1 sends them to the
cheapest model that measurably obeys ``format``:

    candidates = role chain ∪ every model with status "ok"
    skip json_class == "no"
    rank: strict first, then estimated $/call (measured tokens × prices),
          then measured p50 latency

Each candidate gets one attempt with ``format = schema``.  A violation is
first normalized with HYDRA's own tolerant path (``parse_json_blob``, bare
array → schema-shaped wrapper); if that still fails, ONE repair re-ask carries
the validator errors; then the next candidate is tried.  Thinking is always
forced to the model's lowest supported setting.

``[routing] json_policy`` = ``"cheapest_conformant"`` (default) or
``"role_chain"`` (keep the role chain first, then the rest by cost).
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from . import config as C
from . import jsonschema
from .agent import parse_json_blob
from .models import DEFAULT_THINK_STYLE

# jsonroute schema names are mapped onto the T07 matrix names for token
# estimates (digest has no measured schema of its own).
_TOKEN_KEYS = {"plan": "plan", "verdict": "verdict",
               "digest": "report", "report": "report"}


@dataclass
class Structured:
    obj: object = None
    reply: object = None
    model: str = ""
    normalized: bool = False
    repaired: bool = False
    think: object = None
    attempts: list = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.obj is not None


def lowest_think(registry, model: str):
    """The lowest setting the model's think dialect supports."""
    style = registry.think_style(model) if registry is not None else \
        DEFAULT_THINK_STYLE.get(model, "bool")
    if style == "levels":
        return "low"
    if style == "bool":
        return False
    return None  # "none": omit the parameter


# ---------------------------------------------------------------- candidates

def _entry(registry, model: str) -> dict:
    try:
        return registry.entry(model)
    except Exception:
        return {}


def _est_tokens(registry, model: str, schema_name: str, fallback: tuple):
    e = _entry(registry, model)
    by = e.get("json_med_by_schema") or {}
    d = by.get(_TOKEN_KEYS.get(schema_name, schema_name)) or {}
    tin = d.get("med_in") or e.get("json_med_in") or fallback[0]
    tout = d.get("med_out") or e.get("json_med_out") or fallback[1]
    return int(tin), int(tout)


def _p50(registry, model: str, fallback: int) -> int:
    e = _entry(registry, model)
    v = e.get("json_p50_ms")
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    return int(fallback)


def candidates(router, role: str, schema_name: str = "", policy: str = None,
               exclude=()) -> list:
    """Role chain ∪ all ok models, filtered by json_class, cheapness-ranked."""
    cfg = getattr(router, "cfg", None) or {}
    if policy is None:
        policy = C.get(cfg, "routing.json_policy", "cheapest_conformant")
    policy = str(policy or "cheapest_conformant").lower()
    registry = getattr(router, "registry", None)
    chain = list(router.chain_for(role, need_tools=False))
    if registry is None:
        return [m for m in chain if m not in exclude] or list(chain)

    union = list(chain)
    for name in sorted(registry.data.get("models") or {}):
        if name not in union and registry.status_of(name) == "ok":
            union.append(name)
    keep = [m for m in union if registry.json_class_of(m) != "no"]
    filtered = [m for m in keep if m not in exclude]
    if filtered:
        keep = filtered
    elif exclude:
        keep = list(keep)  # only the excluded model conforms: keep it anyway
    measured = [m for m in keep
                if registry.json_class_of(m) in ("strict", "tolerant")]
    if not measured:
        # Nothing measured-conformant yet (or everything is class "no"):
        # keep the pre-F1 chain order instead of ranking on fabricated costs.
        # When every candidate is measured "no", still try the chain so the
        # tolerant normalize/repair path stays available.
        return keep or ([] if exclude else list(chain))
    if policy == "role_chain":
        return keep

    # fallback token estimate: median of the measured candidates, else a
    # neutral small structured call (800 in / 250 out).
    pairs = [_est_tokens(registry, m, schema_name, (0, 0)) for m in keep]
    measured = [(i, o) for i, o in pairs if i and o]
    fb = (int(sorted(p[0] for p in measured)[len(measured) // 2]),
          int(sorted(p[1] for p in measured)[len(measured) // 2])) if measured \
        else (800, 250)
    p50s = [_p50(registry, m, 0) for m in keep]
    measured_p50 = [p for p in p50s if p]
    fb_p50 = int(sorted(measured_p50)[len(measured_p50) // 2]) if measured_p50 \
        else 999999

    prices = getattr(router, "prices", None) or {}

    def rank(m):
        cls = registry.json_class_of(m)
        cls_rank = 0 if cls == "strict" else (1 if cls == "tolerant" else 2)
        tin, tout = _est_tokens(registry, m, schema_name, fb)
        cost = C.est_usd(prices, m, tin, tout)
        return (cls_rank, cost, _p50(registry, m, fb_p50), m)

    return sorted(keep, key=rank)


# ---------------------------------------------------------------- validation

def _repair_prompt(schema_name: str, errors: list) -> str:
    err = "; ".join(str(e) for e in errors[:5]) or "invalid JSON"
    return ("Your previous reply did not satisfy the required JSON schema for "
            f"a {schema_name}. Validator errors: {err}. Reply with ONLY one "
            "JSON value that matches the schema exactly — no prose, no code "
            "fences.")


def _wrap_plan(obj, task: str, schema: dict):
    """Mirror planner.make_dag: a bare list of steps becomes a plan object."""
    props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
    if isinstance(obj, list):
        if "nodes" in props and "steps" not in props:
            obj = {"goal": task, "acceptance": [], "nodes": obj}
        else:
            obj = {"goal": task, "steps": obj}
    if not isinstance(obj, dict):
        return obj
    if "nodes" in props and not isinstance(obj.get("nodes"), list) \
            and isinstance(obj.get("steps"), list):
        enum = (((props.get("nodes") or {}).get("items") or {})
                .get("properties", {}).get("role", {}).get("enum") or [])
        nodes = []
        for i, step in enumerate(obj["steps"], 1):
            if not isinstance(step, dict):
                return obj
            title = step.get("task") or step.get("title") or step.get("step")
            if title is None:
                return obj
            try:
                sid = str(step.get("id") if step.get("id") is not None else i)
            except (TypeError, ValueError):
                sid = str(i)
            role = str(step.get("role") or "worker").strip()
            if enum and role not in enum:
                role = "worker"
            deps = step.get("deps") or []
            if not isinstance(deps, list):
                deps = []
            nodes.append({"id": sid, "role": role, "task": str(title),
                          "deps": [str(d) for d in deps]})
        obj = {"goal": str(obj.get("goal") or task),
               "acceptance": obj.get("acceptance") or [],
               "nodes": nodes}
    return obj


def _wrap_verdict(obj):
    """Mirror verifier._issue_text: a bare string issue is valid downstream."""
    if isinstance(obj, dict) and isinstance(obj.get("issues"), list):
        obj = dict(obj)
        obj["issues"] = [{"what": str(it), "fix": ""} if isinstance(it, str)
                         else it for it in obj["issues"]]
    return obj


def accept(raw: str, schema: dict, schema_name: str, task: str = ""):
    """(obj, errors, normalized). Strict first, then HYDRA normalization."""
    text = (raw or "").strip()
    obj, strict_errs = None, None
    try:
        obj = json.loads(text)
    except Exception as exc:
        strict_errs = [f"not JSON ({type(exc).__name__})"]
    if obj is not None:
        errs = jsonschema.validate(obj, schema)
        if not errs:
            return obj, [], False
        strict_errs = errs
    cand = parse_json_blob(text)
    if schema_name == "plan":
        cand = _wrap_plan(cand, task, schema)
    elif schema_name == "verdict":
        cand = _wrap_verdict(cand)
    if cand is not None:
        errs2 = jsonschema.validate(cand, schema)
        if not errs2:
            return cand, [], True
        return None, errs2[:5], False
    return None, (strict_errs or ["empty reply"])[:5], False


# ---------------------------------------------------------------- the call

def _journal(router, role, schema_name, model, attempts, ok, policy) -> None:
    j = getattr(router, "journal", None)
    if j is None:
        return
    try:
        j.event("json_route", None, role=role, schema=schema_name, model=model,
                policy=policy, ok=bool(ok),
                attempts=[{k: a.get(k) for k in
                           ("model", "stage", "ok", "normalized", "error")}
                          for a in attempts])
    except Exception:
        pass


def _single_fallback(router, messages, role, schema, schema_name, think,
                     agent, task, model_hint=None) -> Structured:
    """No measured-conformant candidate: pre-F1 behaviour, one router call.

    The router keeps its own failover across the role chain; the tolerant
    normalization still runs, but there is no repair re-ask: the candidates
    are known to ignore ``format``, so a second call would just burn tokens.
    ``model_hint`` preserves call-site preferences (the verifier's diversity
    rule) that the pre-F1 code expressed by passing ``model=``.
    """
    attempts: list = []
    try:
        reply = router.chat(messages, role=role, agent=agent, think=think,
                            format=schema, model=model_hint)
    except Exception as exc:
        return Structured(attempts=attempts,
                          error=f"{type(exc).__name__}: {exc}"[:200])
    obj, errs, normalized = accept(reply.content or "", schema, schema_name, task)
    attempts.append({"model": reply.model, "stage": "fallback",
                     "ok": obj is not None, "normalized": normalized,
                     "errors": errs, "in": reply.prompt_eval_count,
                     "out": reply.eval_count, "ms": reply.ms})
    _journal(router, role, schema_name, reply.model, attempts, obj is not None,
             "fallback")
    if obj is None:
        return Structured(reply=reply, model=reply.model, think=think,
                          attempts=attempts,
                          error="; ".join(str(e) for e in errs) or "invalid JSON")
    return Structured(obj=obj, reply=reply, model=reply.model,
                      normalized=normalized, think=think, attempts=attempts)


def structured_chat(router, messages, *, role: str, schema: dict,
                    schema_name: str, think=None, agent=None, exclude=(),
                    policy: str = None, task: str = "",
                    model_hint: str = None) -> Structured:
    """One structured call, F1-routed. Never raises for schema violations.

    ``think`` is accepted for call-site compatibility and ignored: F1 always
    uses the lowest supported setting.  Network/API errors just move on to the
    next candidate; the caller keeps its existing fallback behaviour.  When no
    candidate has a measured ``json_class`` of strict/tolerant (fresh install,
    or T07 found none), routing degrades to the pre-F1 single call and
    ``model_hint`` is forwarded to the router.
    """
    cfg = getattr(router, "cfg", None) or {}
    registry = getattr(router, "registry", None)
    if policy is None:
        policy = str(C.get(cfg, "routing.json_policy", "cheapest_conformant"))
    cands = candidates(router, role, schema_name=schema_name, policy=policy,
                       exclude=exclude)
    policy_ok = any(registry is not None
                    and registry.json_class_of(m) in ("strict", "tolerant")
                    for m in cands)
    if not policy_ok:
        return _single_fallback(router, list(messages), role, schema,
                                schema_name, think, agent, task, model_hint)
    attempts: list = []
    base = list(messages)
    for model in cands:
        think_val = lowest_think(registry, model)
        stage = "first"
        msgs = list(base)
        while True:
            try:
                reply = router.chat(msgs, role=role, agent=agent, model=model,
                                    think=think_val, format=schema,
                                    chain_override=[model])
            except Exception as exc:
                attempts.append({"model": model, "stage": stage,
                                 "error": f"{type(exc).__name__}: {exc}"[:200]})
                break
            raw = reply.content or ""
            obj, errs, normalized = accept(raw, schema, schema_name, task)
            attempts.append({"model": model, "stage": stage, "ok": obj is not None,
                             "normalized": normalized, "errors": errs,
                             "in": reply.prompt_eval_count, "out": reply.eval_count,
                             "ms": reply.ms})
            if obj is not None:
                _journal(router, role, schema_name, model, attempts, True, policy)
                return Structured(obj=obj, reply=reply, model=model,
                                  normalized=normalized,
                                  repaired=stage == "repair",
                                  think=think_val, attempts=attempts)
            if stage == "first":
                msgs = base + [{"role": "assistant", "content": raw[:4000]},
                               {"role": "user",
                                "content": _repair_prompt(schema_name, errs)}]
                stage = "repair"
                continue
            break  # repair failed: next candidate
    _journal(router, role, schema_name, "", attempts, False, policy)
    last = attempts[-1] if attempts else {}
    error = last.get("error") or ("no candidate produced valid JSON"
                                  if cands else "no conformant candidates")
    return Structured(attempts=attempts, error=error)
