"""F5 — limits calibration (``hydra doctor --limits``).

Runs the T08 probe shape with **at most 6 tiny calls** and records the server's
concurrency semantics in ``~/.hydra/limits.json``:

    {"behaviour": "queue" | "reject:429", "max_parallel_ok": N,
     "checked_at": "...", "model": "...", "calls": N}

* ``queue``          — every concurrent call was accepted (the server queues).
* ``reject:<status>`` — concurrency is capped and the server answers <status>.

The LLM gate seeds itself from the observation and AIMD shrinks only when the
observed semantics say concurrency is what gets rejected (a queueing server
rejects for quota reasons, not because we ran two calls at once).

Real free-plan measurement (T08): 14/14 calls 200, no 429 — QUEUES.
"""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path

from . import config as C

PING = [{"role": "user", "content": "Reply with the single word OK."}]
MAX_CALLS = 6
REJECT_HINTS = (429, 503)


class ProbeError(RuntimeError):
    """Could not classify the server (network/auth/model errors)."""


class BudgetExceeded(RuntimeError):
    """F10: opt-in USD cap reached; checkpoint + park (exit 75)."""


def limits_path() -> Path:
    return C.home() / "limits.json"


def load_limits(path=None) -> dict:
    try:
        data = json.loads((Path(path) if path else limits_path()).read_text(
            encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_limits(data: dict, path=None) -> None:
    target = Path(path) if path else limits_path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(target)
    except Exception:
        pass


def pick_model(cfg: dict) -> str:
    """Cheapest sane probe target: first worker model, else first lead model."""
    for role in ("worker", "lead"):
        chain = C.get(cfg, f"roles.{role}.chain") or []
        for model in chain:
            if model:
                return str(model)
    return "gpt-oss:20b"


def _one(client, model: str) -> dict:
    try:
        reply = client.chat(model, PING, options={"num_predict": 8}, stream=True)
        return {"ok": True, "status": 200, "ms": int(getattr(reply, "ms", 0) or 0)}
    except Exception as exc:              # noqa: BLE001 - the probe classifies it
        return {"ok": False, "status": getattr(exc, "status", None),
                "error": type(exc).__name__}


def _group(client, model: str, n: int) -> list:
    barrier = threading.Barrier(n)
    results: list = [None] * n

    def worker(i: int) -> None:
        try:
            barrier.wait(timeout=15)
        except Exception:
            pass
        results[i] = _one(client, model)

    threads = [threading.Thread(target=worker, args=(i,), daemon=True,
                                name=f"limits-{n}-{i}") for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=120)
    return [r or {"ok": False, "status": None, "error": "no result"}
            for r in results]


def _verdict(groups: list, calls: list) -> dict:
    flat = [r for g in groups for r in g["calls"]]
    if all(r["ok"] for r in flat):
        return {"behaviour": "queue", "max_parallel_ok": int(groups[-1]["n"])}
    if any(r.get("status") is None for r in flat):
        bad = next(r for r in flat if r.get("status") is None)
        raise ProbeError(f"network/model error before a status arrived "
                         f"({bad.get('error')})")
    first = next((int(r["status"]) for r in flat if not r["ok"]), None)
    if first is None:                     # mixed 200/error with no status is impossible
        raise ProbeError("probe could not classify the server")
    successes = max(sum(1 for r in g["calls"] if r["ok"]) for g in groups)
    return {"behaviour": f"reject:{first}", "max_parallel_ok": max(1, successes)}


def probe(client, cfg: dict = None, model: str = None) -> dict:
    """At most 6 tiny calls: a 2-burst, and (only when all 200) a 4-burst."""
    model = str(model or pick_model(cfg or {}))
    calls: list = []

    g1 = _group(client, model, 2)
    calls += g1
    groups = [{"n": 2, "calls": g1}]
    if all(r["ok"] for r in g1):
        g2 = _group(client, model, 4)
        calls += g2
        groups.append({"n": 4, "calls": g2})

    data = _verdict(groups, calls)
    data["checked_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    data["model"] = model
    data["calls"] = len(calls)
    return data


# ---------------------------------------------------------------- consumers

def semantics() -> str:
    return str(load_limits().get("behaviour") or "")


def rejection_status():
    """The calibrated rejection status (int), else None."""
    behaviour = semantics()
    if behaviour.startswith("reject:"):
        try:
            return int(behaviour.split(":", 1)[1])
        except (TypeError, ValueError):
            return None
    return None


def gate_capacity(cfg: dict, base: int = None) -> int:
    """Plan cap adjusted by the observation.

    * queue  -> concurrency is safe; allow the measured parallel width;
    * reject -> never exceed the width that was observed to work;
    * nothing recorded / unknown -> built-in plan capacity (fail open).
    """
    base = int(C.max_inflight(cfg) if base is None else base)
    data = load_limits()
    if not data:
        return base
    try:
        measured = int(data.get("max_parallel_ok") or 0)
    except (TypeError, ValueError):
        measured = 0
    if measured < 1:
        return base
    behaviour = str(data.get("behaviour") or "")
    if behaviour == "queue":
        # an explicit --max-inflight / api.max_inflight wins over widening
        if int(C.get(cfg, "api.max_inflight", 0) or 0) > 0:
            return base
        return max(base, measured)
    if behaviour.startswith("reject"):
        return max(1, min(base, measured))
    return base
