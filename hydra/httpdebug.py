"""F4 ``--debug-http``: one redacted NDJSON record per API call.

Enabled by ``--debug-http`` (config ``api.debug_http``) or the environment
variable ``HYDRA_DEBUG_HTTP=1``. Records land in ``runs/<run_id>/http.jsonl``
(path via :func:`hydra.config.run_dir`).

Hard rules (work order F4):
  * the ``Authorization`` header and request bodies are never written;
  * a non-2xx ``error_body`` is capped at 500 chars;
  * every written line goes through the existing hygiene redactor, so the API
    key can never leak even if a server echoes it back.

The file is append-only, one JSON object per line, written under a lock so
parallel model calls cannot interleave a line.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path

from . import config as C

ENV_NAME = "HYDRA_DEBUG_HTTP"

# Always recorded when present, plus any header whose name looks rate-limit-ish.
BASE_HEADERS = ("retry-after", "content-type", "transfer-encoding")
RATE_HINTS = ("rate", "limit", "retry", "quota", "remaining", "reset", "usage")


def enabled(cfg: dict) -> bool:
    """True when --debug-http / HYDRA_DEBUG_HTTP=1 is on. Default: off."""
    val = os.environ.get(ENV_NAME, "")
    if str(val).strip().lower() in ("1", "true", "yes", "on"):
        return True
    return bool(C.get(cfg, "api.debug_http", False))


def prefix_hash(messages: list, tools=None) -> str:
    """sha256 over the byte-stable request prefix: system prompt + tools JSON.

    This is the pinned cached prefix from §6 of the spec; it must not move
    between turns unless a compaction really changed it.
    """
    system = ""
    for m in messages or ():
        if isinstance(m, dict) and str(m.get("role")) == "system":
            system = str(m.get("content") or "")
            break
    tools_json = json.dumps(tools or [], sort_keys=True, ensure_ascii=False)
    return hashlib.sha256((system + "\n" + tools_json).encode("utf-8")).hexdigest()


def pick_headers(resp) -> dict:
    """The selected response headers (lower-cased names, capped values)."""
    out = {}
    try:
        items = list(resp.headers.items())
    except Exception:
        return out
    for name, value in items:
        low = str(name).lower()
        if low in BASE_HEADERS or any(h in low for h in RATE_HINTS):
            out[low] = str(value)[:200]
    return out


def brief(value, cap: int = 300):
    """A small, log-safe rendering of the think/format parameter values."""
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:cap]
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True)[:cap]
    except Exception:
        return str(value)[:cap]


class HttpLogger:
    """Thread-safe append-only writer for ``runs/<id>/http.jsonl``."""

    def __init__(self, path, redact=None):
        self.path = Path(path)
        self.redact = redact
        self.lock = threading.Lock()

    def write(self, method, path, model="", *, status=0, latency_ms=0,
              ttfb_ms=None, request_bytes=0, response_bytes=0, headers=None,
              think=None, format=None, prefix=None, error_body=None) -> dict:
        rec = {
            "ts": round(time.time(), 3),
            "method": str(method),
            "path": str(path),
            "model": str(model or ""),
            "status": int(status),
            "latency_ms": int(latency_ms),
            "ttfb_ms": int(ttfb_ms) if ttfb_ms is not None else None,
            "request_bytes": int(request_bytes),
            "response_bytes": int(response_bytes),
            "headers": dict(headers or {}),
            "think": brief(think),
            "format": brief(format),
            "prefix_hash": prefix,
        }
        if error_body and not (200 <= rec["status"] < 300):
            rec["error_body"] = str(error_body)[:500]
        line = json.dumps(rec, ensure_ascii=False)
        if self.redact is not None:
            line = self.redact(line)  # reuse the repo's redaction path
        with self.lock:
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(self.path, "a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
                    fh.flush()
            except Exception:
                pass  # debug logging must never break a run
        return rec
