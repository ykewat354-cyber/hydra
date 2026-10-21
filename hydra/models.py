"""Model discovery + capability registry (§7).

`~/.hydra/models.json` is data, not logic: the router never hard-codes a model
name, it only reads statuses and flags from here.  `hydra models probe` fills
it in by running a tiny scripted conversation per candidate through the gate:

    ping  -> status: ok / paid / missing / flaky  (+ latency)
    tools -> did it emit a well-formed tool call?
    json  -> did it return parseable JSON?
    think -> "levels" (level strings), "bool", or "none" (omit the param)

Everything is serial through the gate so a free-plan account is never
hammered while it is being probed.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

from . import config as C
from .client import (AuthError, BadRequest, ModelMissing, OllamaCloud,
                     PaidModel, ServerError, StreamStalled, make_client)

# Fallback for models that were never probed.  Snapshot knowledge lives in
# exactly one place per line and nowhere in the control flow.
DEFAULT_THINK_STYLE = {
    "gpt-oss:120b": "levels",
    "gpt-oss:20b": "levels",
}

PING_MSG = [{"role": "user", "content": "Reply with OK."}]
JSON_MSG = [{"role": "user", "content": 'Reply with JSON only: {"ok": true}'}]
TOOL_MSG = [{"role": "user", "content": "Use the add tool to add 2 and 3."}]
ADD_TOOL = {
    "type": "function",
    "function": {
        "name": "add",
        "description": "Add two integers.",
        "parameters": {
            "type": "object",
            "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
            "required": ["a", "b"],
        },
    },
}


class ModelRegistry:
    """Status/capability cache backed by ~/.hydra/models.json."""

    def __init__(self, cfg: dict, path: Path = None):
        self.cfg = cfg
        self.path = Path(path) if path else (C.home() / "models.json")
        self.data = {"updated": 0.0, "models": {}}
        if self.path.is_file():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    self.data.update(raw)
                    self.data.setdefault("models", {})
            except Exception:
                pass

    # -------------------------------------------------- reads
    def entry(self, model: str) -> dict:
        return dict(self.data.get("models", {}).get(model) or {})

    def status_of(self, model: str) -> str:
        e = self.entry(model)
        if not e:
            return "unknown"
        st = e.get("status", "unknown")
        now = time.time()
        if st in ("paid", "missing"):
            recheck = float(e.get("recheck_at") or 0)
            if recheck and now >= recheck:
                return "unknown"  # re-probe got scheduled; let it try again
        if st == "ok":
            checked = float(e.get("checked_at") or 0)
            days = float(C.get(self.cfg, "models.reprobe_days", 7) or 7)
            if checked and (now - checked) > days * 86400:
                return "unknown"  # weekly re-probe: stale ok decays
        return st

    def think_style(self, model: str) -> str:
        if str(C.get(self.cfg, "api.protocol", "ollama") or "ollama").lower() == "openai":
            return "none"
        st = self.entry(model).get("think_style")
        if st in ("levels", "bool", "none"):
            return st
        return DEFAULT_THINK_STYLE.get(model, "bool")

    def is_tool_capable(self, model: str) -> bool:
        e = self.entry(model)
        if not e or e.get("status") != "ok":
            return True  # unknown models are allowed, ranked last
        if e.get("tools") is None:
            return True  # probe did not answer: unknown, not "no"
        return bool(e.get("tools"))

    def is_json_capable(self, model: str) -> bool:
        e = self.entry(model)
        if not e or e.get("status") != "ok":
            return True  # unknown models are allowed, ranked with the good ones
        return bool(e.get("json", True))

    def needs_reprobe(self) -> bool:
        days = float(C.get(self.cfg, "models.reprobe_days", 7) or 7)
        return (time.time() - float(self.data.get("updated") or 0)) > days * 86400

    def list_all(self) -> list:
        out = []
        for name in sorted(self.data.get("models", {})):
            e = self.entry(name)
            e["model"] = name
            e["status_now"] = self.status_of(name)
            out.append(e)
        return out

    # -------------------------------------------------- writes
    def mark(self, model: str, status: str, **extra) -> None:
        e = self.data.setdefault("models", {}).setdefault(model, {})
        e["status"] = status
        e["checked_at"] = time.time()
        if status in ("paid", "missing"):
            # C2: retry before the next use after >=60s, not next day.
            e["recheck_at"] = time.time() + 60
        e.update(extra)
        self.save()

    def set_think_style(self, model: str, style: str) -> None:
        e = self.data.setdefault("models", {}).setdefault(model, {})
        e["think_style"] = style
        e["think_checked_at"] = time.time()
        self.save()

    def set_json_class(self, model: str, json_class: str, **extra) -> None:
        """T07: store measured structured-output conformance (strict|tolerant|no)."""
        e = self.data.setdefault("models", {}).setdefault(model, {})
        e["json_class"] = json_class
        e["json_checked_at"] = time.time()
        e.update(extra)
        self.save()

    def json_class_of(self, model: str) -> str:
        cls = self.entry(model).get("json_class")
        return cls if cls in ("strict", "tolerant", "no") else "unknown"

    def set_json_prompt_class(self, model: str, json_class: str,
                              **extra) -> None:
        """C3: prompted JSON-only matrix (with and without ``format``)."""
        e = self.data.setdefault("models", {}).setdefault(model, {})
        e["json_prompt_class"] = json_class
        e["json_prompt_checked_at"] = time.time()
        e.update(extra)
        self.save()

    def json_prompt_class_of(self, model: str) -> str:
        cls = self.entry(model).get("json_prompt_class")
        return cls if cls in ("strict", "tolerant", "no") else "unknown"

    def routing_json_class_of(self, model: str) -> str:
        """F1 prefers the C3 prompted measurement; T07 is the fallback."""
        cls = self.json_prompt_class_of(model)
        return cls if cls != "unknown" else self.json_class_of(model)

    def save(self) -> None:
        self.data["updated"] = time.time()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False),
                       encoding="utf-8")
        os.replace(tmp, self.path)


# ------------------------------------------------------ probing

def detect_think(client, gate, model: str) -> str:
    """Figure out the `think` dialect: level string, bool, or not at all."""
    for probe, style in (("low", "levels"), (True, "bool")):
        try:
            with gate.slot(0):
                client.chat(model, [{"role": "user", "content": "Say OK"}],
                            think=probe, options={"num_predict": 8})
            return style
        except BadRequest:
            continue
        except Exception:
            return "bool"
    return "none"


def probe_one(client, gate, model: str) -> dict:
    # None = unknown (probe failed); False = the model answered without a
    # tool call.  Never turn a failed probe into a capability claim (C1).
    res = {"status": "unknown", "tools": None, "json": False,
           "think_style": "bool", "ms": 0}
    t0 = time.time()
    try:
        with gate.slot(0):
            client.chat(model, PING_MSG, options={"num_predict": 16})
    except PaidModel as exc:
        res.update(status="paid", error=str(exc)[:160])
        res["ms"] = int((time.time() - t0) * 1000)
        return res
    except ModelMissing as exc:
        res.update(status="missing", error=str(exc)[:160])
        res["ms"] = int((time.time() - t0) * 1000)
        return res
    except AuthError:
        raise
    except (ServerError, StreamStalled, BadRequest, OSError,
            ConnectionError, TimeoutError) as exc:
        res.update(status="flaky", error=f"{type(exc).__name__}: {exc}"[:160])
        res["ms"] = int((time.time() - t0) * 1000)
        return res
    res["ms"] = int((time.time() - t0) * 1000)

    # tool calling: native tool_calls first, then the same tolerant text
    # parser the loop uses.  Retry once: a model may emit the call on the
    # second try.  A failed probe stays unknown (None), never "no" (C1).
    res["tools"] = None
    for _attempt in range(2):
        try:
            with gate.slot(0):
                r = client.chat(model, TOOL_MSG, tools=[ADD_TOOL],
                                options={"num_predict": 64})
            candidates = list(r.tool_calls or [])
            if not candidates:
                try:
                    from .agent import parse_text_calls
                    candidates = parse_text_calls(r.content or "")
                except Exception:
                    candidates = []
            res["tools"] = False
            for tc in candidates:
                fn = (tc.get("function") or {})
                if fn.get("name") == "add":
                    res["tools"] = True
                    break
            if res["tools"]:
                break
        except Exception:
            res["tools"] = None
            continue

    # json output
    try:
        with gate.slot(0):
            r = client.chat(model, JSON_MSG, options={"num_predict": 48})
        m = re.search(r"\{.*\}", r.content or "", re.S)
        if m:
            json.loads(m.group(0))
            res["json"] = True
    except Exception:
        pass

    res["think_style"] = detect_think(client, gate, model)
    res["status"] = "ok"
    return res


def probe_all(cfg: dict, client, gate, registry: ModelRegistry,
              include_all: bool = False, progress=None) -> dict:
    try:
        tags = [str(m.get("name") or m.get("model")) for m in client.tags()]
    except Exception:
        tags = []
    if include_all:
        candidates = tags
    else:
        configured = list(C.get(cfg, "models.probe_candidates")
                          or C.CANDIDATE_MODELS)
        candidates = [c for c in configured if (not tags or c in tags)]
        if not candidates:
            candidates = configured
    seen, ordered = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)
    out = {}
    for name in ordered:
        res = probe_one(client, gate, name)
        registry.mark(name, res.get("status", "unknown"),
                      tools=res.get("tools"), json=res.get("json"),
                      think_style=res.get("think_style"), ms=res.get("ms"))
        out[name] = res
        if progress:
            progress(name, res)
    registry.data["tags"] = tags
    registry.save()
    return out


# ------------------------------------------------------ auto re-probe (C2)
_REPROBE_LOCK = threading.Lock()
REPROBE_GAP_S = 3600.0


def maybe_reprobe(cfg: dict, client, gate, registry, journal=None,
                  needed=None, force: bool = False) -> list:
    """Probe stale, already-known models once per hour, never every turn.

    A fresh install (empty models.json) is not probed implicitly: that is the
    explicit `hydra models probe` path.  Only entries whose checked_at is
    older than models.reprobe_days are refreshed, serial through the gate.
    """
    if not registry or not registry.data.get("models"):
        return []
    if not str(getattr(client, "key", "") or ""):
        return []
    days = float(C.get(cfg, "models.reprobe_days", 7) or 7)
    now = time.time()
    marker = C.home() / "models.reprobe"
    if not force:
        try:
            last = float(marker.read_text(encoding="utf-8").strip() or 0)
        except Exception:
            last = 0.0
        if now - last < REPROBE_GAP_S:
            return []
    names = list(needed or [])
    if not names:
        for role in ("lead", "worker", "coder", "researcher", "reviewer",
                     "verifier", "critic", "summarizer", "planner", "debugger",
                     "tester", "scribe", "vision"):
            chain = list(C.get(cfg, f"roles.{role}.chain") or [])
            if chain and str(chain[0]) not in names:
                names.append(str(chain[0]))
    todo = []
    for name in names:
        e = registry.entry(name)
        checked = float(e.get("checked_at") or 0)
        if checked and (now - checked) > days * 86400:
            todo.append(name)
    cap = int(C.get(cfg, "models.auto_reprobe_max", 2) or 2)
    todo = todo[:max(0, cap)]
    if not todo:
        return []
    with _REPROBE_LOCK:
        try:
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(str(now), encoding="utf-8")
        except Exception:
            pass
        done = []
        for name in todo:
            old = registry.entry(name)
            try:
                res = probe_one(client, gate, name)
                registry.mark(name, res.get("status", "unknown"),
                              tools=res.get("tools"), json=res.get("json"),
                              think_style=res.get("think_style"),
                              ms=res.get("ms"))
                done.append(name)
            except AuthError:
                break
            except Exception:
                continue
        if journal is not None:
            try:
                journal.event("reprobe", None, models=done, ok=bool(done),
                              old={n: registry.entry(n).get("status")
                                   for n in todo})
            except Exception:
                pass
        return done


# ------------------------------------------------------ CLI

def cli(cfg: dict, args: list) -> int:
    sub = args[0] if args else "ls"
    if sub == "probe":
        return _cmd_probe(cfg, args[1:])
    if sub in ("ls", "list"):
        return _cmd_ls(cfg, args[1:])
    if sub == "pin":
        return _cmd_pin(cfg, args[1:])
    if sub in ("bench", "tune"):
        from . import bench
        return (bench.cli_bench(cfg, args[1:]) if sub == "bench"
                else bench.cli_tune(cfg, args[1:]))
    print("usage: hydra models probe [--all] | ls [--roles] | pin MODEL [--role ROLE] "
          "| bench [--models a,b] [--tasks B1,B2,B3] [--runs 1] [--max-usd 0.12] "
          "| tune [--apply]")
    return 2


def _line(name: str, res: dict) -> str:
    tools = {True: "y", False: "n", None: "?"}.get(res.get("tools"), "?") if \
        "tools" in res else "?"
    return (f"  {name:<22} {res.get('status', '?'):<7} "
            f"tools={tools} "
            f"json={'y' if res.get('json') else 'n'} "
            f"think={res.get('think_style', '?')} {res.get('ms', 0)}ms")


def _cmd_probe(cfg: dict, args: list) -> int:
    from .gate import LLMGate

    key = C.load_key(cfg)
    host = C.get(cfg, "api.host", "https://ollama.com")
    if not key:
        print("hydra: no API key. Put it in ~/.hydra/key or set the configured "
              "api.key_env (OPENROUTER_API_KEY / OPENAI_API_KEY also work for "
              "protocol=openai).")
        return 1
    client = make_client(cfg)
    gate = LLMGate(C.max_inflight(cfg))
    reg = ModelRegistry(cfg)
    print(f"probing via {host} (serial through the gate)...")
    try:
        results = probe_all(cfg, client, gate, reg,
                            include_all="--all" in args,
                            progress=lambda n, r: print(_line(n, r)))
    except AuthError as exc:
        print(f"hydra: API rejected the key (401/403): {exc}")
        return 1
    buckets = {}
    for r in results.values():
        buckets.setdefault(r.get("status", "?"), []).append(r)
    summary = " | ".join(f"{k}: {len(v)}" for k, v in sorted(buckets.items()))
    print(f"\n{summary or 'nothing probed'}")
    print(f"saved -> {reg.path}")
    return 0 if results else 1


def _tool_flag(e: dict) -> str:
    return {True: "y", False: "n", None: "?"}.get(e.get("tools"), "?")


def _cmd_ls(cfg: dict, args: list = None) -> int:
    reg = ModelRegistry(cfg)
    if not reg.data.get("models"):
        print("no models.json yet - run: hydra models probe")
        return 0
    if args and "--roles" in args:
        roles = ("lead", "worker", "coder", "researcher", "reviewer",
                 "verifier", "critic", "summarizer", "planner", "debugger",
                 "tester", "scribe", "vision")
        for role in roles:
            chain = [str(m) for m in (C.get(cfg, f"roles.{role}.chain") or [])]
            if not chain:
                continue
            effective, dropped = [], []
            for name in chain:
                e = reg.entry(name)
                status = reg.status_of(name)
                if status in ("paid", "missing"):
                    dropped.append((name, status))
                    continue
                if (status == "ok" and role in ("lead", "coder", "worker",
                                                "debugger", "tester")
                        and not reg.is_tool_capable(name)):
                    dropped.append((name, "tools=no"))
                    continue
                effective.append((name, e, status))
            print(f"[{role}]")
            print(" effective: " + (" -> ".join(n for n, _, _ in effective)
                                    or "(none)"))
            for i, (name, e, status) in enumerate(effective):
                checked = e.get("checked_at") or 0
                when = time.strftime("%m-%d %H:%M",
                                     time.localtime(checked)) if checked \
                    else "never"
                active = " *" if i == 0 else "  "
                print(f" {active} {name:<22} {status:<7} "
                      f"tools={_tool_flag(e)} "
                      f"json={e.get('json_prompt_class') or e.get('json_class') or '?'} "
                      f"think={e.get('think_style', '?')} checked={when}")
            for name, why in dropped:
                print(f"   - {name:<22} dropped: {why}")
        lead = [str(m) for m in (C.get(cfg, "roles.lead.chain") or [])]
        lead_eff = [m for m in lead if reg.status_of(m) not in ("paid", "missing")]
        lead_eff = [m for m in lead_eff if reg.is_tool_capable(m)] or lead_eff
        print("default lead: " + (lead_eff[0] if lead_eff else "(none)") +
              " (first effective lead-chain model)")
        if reg.needs_reprobe():
            print("(stale: run `hydra models probe`)")
        return 0
    for e in reg.list_all():
        checked = time.strftime("%m-%d %H:%M",
                                time.localtime(e.get("checked_at") or 0))
        print(f"{e['model']:<22} {e['status_now']:<7} "
              f"tools={_tool_flag(e)} "
              f"json={'y' if e.get('json') else '?'} "
              f"think={e.get('think_style', '?')} checked={checked}")
    if reg.needs_reprobe():
        print("(stale: run `hydra models probe`)")
    return 0


def _cmd_pin(cfg: dict, args: list) -> int:
    if not args:
        print("usage: hydra models pin MODEL [--role ROLE]")
        return 2
    model = args[0]
    role = "lead"
    if "--role" in args:
        try:
            role = args[args.index("--role") + 1]
        except IndexError:
            print("--role needs a value")
            return 2
    base = list(C.get(cfg, f"roles.{role}.chain") or [])
    chain = [model] + [m for m in base if m != model]
    p = C.home() / "pinned.toml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        "# written by `hydra models pin`; loaded after config.toml, "
        "before project config\n"
        f"[roles.{role}]\nchain = {json.dumps(chain)}\n",
        encoding="utf-8")
    print(f"pinned {model} first for role {role}")
    print(f"wrote {p} (remove the file to unpin)")
    return 0
