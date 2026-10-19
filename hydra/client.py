"""Ollama Cloud client: streaming chat over the native API. Stdlib only.

Transport rules (§5.2 of the spec):
  * POST {host}/api/chat, Authorization: Bearer <key>, NDJSON stream.
  * Accumulate message.thinking / message.content / message.tool_calls.
  * Final chunk (done=true) carries prompt_eval_count / eval_count.
  * Tool calls arrive whole; detect them by presence, never by done_reason.
  * connect timeout 15 s, idle timeout 90 s (phone networks stall mid-stream).
"""
from __future__ import annotations

import http.client
import json
import socket
import ssl
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from . import httpdebug


# ---------------------------------------------------------------- results

@dataclass
class Reply:
    model: str = ""
    thinking: str = ""
    content: str = ""
    tool_calls: list = field(default_factory=list)
    prompt_eval_count: int = 0
    eval_count: int = 0
    done_reason: str = "stop"
    ms: int = 0
    parsed_from_text: list = field(default_factory=list)  # calls recovered from content

    def as_message(self) -> dict:
        msg = {"role": "assistant", "content": self.content}
        if self.thinking:
            msg["thinking"] = self.thinking
        if self.tool_calls:
            msg["tool_calls"] = self.tool_calls
        return msg


class ApiError(Exception):
    retryable = False

    def __init__(self, status: int, body: str, model: str = "", retry_after=None):
        super().__init__(f"HTTP {status}: {body[:300]}")
        self.status = status
        self.body = body
        self.model = model
        self.retry_after = retry_after


class AuthError(ApiError):
    """401/403 — fatal; the key is missing or wrong."""


class PaidModel(ApiError):
    """402 — this model is not (free) for this key."""


class ModelMissing(ApiError):
    """404 — tag drift."""


class RateLimited(ApiError):
    """429/503 or a queue rejection — retryable, honor Retry-After."""
    retryable = True


class ServerError(ApiError):
    """5xx — retryable."""
    retryable = True


class BadRequest(ApiError):
    """400 — one repair re-ask is allowed before failing over."""


class ContextOverflow(BadRequest):
    """400 caused by exceeding the model's context window."""


class StreamStalled(Exception):
    """The stream stopped delivering bytes (network switch, kill, reset)."""


class CallCancelled(Exception):
    """The user pressed Ctrl-C: cancel this in-flight call, keep the run alive."""


class EmptyReply(Exception):
    """200 with no content, no thinking and no tool calls."""


_CONTEXT_MARKERS = ("context length", "context_length", "context window", "too large",
                    "exceeds", "maximum context", "too many tokens")


def classify(status: int, body: str, model: str, retry_after=None) -> ApiError:
    low = body.lower()
    if status in (401, 403):
        return AuthError(status, body, model)
    if status == 402:
        return PaidModel(status, body, model)
    if status == 404:
        return ModelMissing(status, body, model)
    if status in (429, 503):
        return RateLimited(status, body, model, retry_after)
    if 500 <= status < 600:
        return ServerError(status, body, model, retry_after)
    if status == 400 and any(m in low for m in _CONTEXT_MARKERS):
        return ContextOverflow(status, body, model)
    return BadRequest(status, body, model)


# ---------------------------------------------------------------- client

class OllamaCloud:
    def __init__(self, host: str, key: str, connect_timeout: float = 15,
                 idle_timeout: float = 90):
        self.host = host.rstrip("/")
        parts = urlsplit(self.host)
        self._scheme = parts.scheme or "https"
        self._netloc = parts.netloc or self.host
        self.key = key or ""
        self.connect_timeout = float(connect_timeout)
        self.idle_timeout = float(idle_timeout)
        self.cancel_event = None  # set by Runtime: a threading.Event (SIGINT)
        self.redact = None        # set by Runtime: fn(str)->str (secret hygiene)
        self.http_log = None      # set by Runtime (F4 --debug-http): HttpLogger

    # -------------------------------------------------- plumbing
    def _connect(self):
        if self._scheme == "https":
            ctx = ssl.create_default_context()
            conn = http.client.HTTPSConnection(self._netloc, timeout=self.connect_timeout, context=ctx)
        else:
            conn = http.client.HTTPConnection(self._netloc, timeout=self.connect_timeout)
        return conn

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "hydra/1.0 (termux)",
        }

    def _json_request(self, method: str, path: str, payload=None) -> dict:
        conn = self._connect()
        started = time.time()
        status, ttfb, req_bytes, resp_bytes = 0, None, 0, 0
        headers, error_body, got_resp = None, None, False
        try:
            body = json.dumps(payload).encode() if payload is not None else None
            req_bytes = len(body or b"")
            conn.request(method, path, body, self._headers())
            resp = conn.getresponse()
            got_resp = True
            status = resp.status
            ttfb = int((time.time() - started) * 1000)
            headers = httpdebug.pick_headers(resp)
            raw = resp.read().decode("utf-8", "replace")
            resp_bytes = len(raw.encode("utf-8", "replace"))
            if self.redact is not None:
                raw = self.redact(raw)  # a server echo must not reach tracebacks
            if resp.status >= 400:
                error_body = raw
                raise classify(resp.status, raw, "api")
            try:
                return json.loads(raw or "{}")
            except json.JSONDecodeError:
                return {}
        finally:
            if got_resp:
                self._log_http(method, path, "", status, started, ttfb, req_bytes,
                               resp_bytes, headers, error_body=error_body)
            try:
                conn.close()
            except Exception:
                pass

    def _log_http(self, method, path, model, status, started, ttfb, req_bytes,
                  resp_bytes, headers, think=None, format=None, prefix=None,
                  error_body=None) -> None:
        """F4: append one record when --debug-http is on; never raise."""
        if self.http_log is None:
            return
        try:
            self.http_log.write(
                method, path, model, status=status,
                latency_ms=max(0, int((time.time() - started) * 1000)),
                ttfb_ms=ttfb, request_bytes=req_bytes,
                response_bytes=resp_bytes, headers=headers, think=think,
                format=format, prefix=prefix, error_body=error_body)
        except Exception:
            pass

    # -------------------------------------------------- chat
    def chat(self, model: str, messages: list, tools=None, think=None,
             options=None, stream: bool = True, on_delta=None, format=None) -> Reply:
        self._check_cancel()
        payload = {"model": model, "messages": messages, "stream": bool(stream)}
        if tools:
            payload["tools"] = tools
        if think is not None:
            payload["think"] = think
        if options:
            payload["options"] = options
        if format:
            # structured outputs (§19.10): tool-less calls only — callers enforce
            payload["format"] = format

        conn = self._connect()
        started = time.time()
        prefix = httpdebug.prefix_hash(messages, tools) if self.http_log is not None else None
        status, ttfb, req_bytes, resp_bytes = 0, None, 0, 0
        headers, error_body, got_resp = None, None, False
        try:
            body = json.dumps(payload)
            if self.redact is not None:
                body = self.redact(body)  # nothing leaves for a model un-redacted
            body = body.encode()
            req_bytes = len(body)
            conn.request("POST", "/api/chat", body, self._headers())
            resp = conn.getresponse()
            got_resp = True
            status = resp.status
            ttfb = int((time.time() - started) * 1000)
            headers = httpdebug.pick_headers(resp)
            if resp.status >= 400:
                raw = resp.read().decode("utf-8", "replace")
                resp_bytes = len(raw.encode("utf-8", "replace"))
                if self.redact is not None:
                    raw = self.redact(raw)  # a server echo must not reach tracebacks
                error_body = raw
                retry_after = None
                ra = resp.getheader("Retry-After")
                if ra:
                    try:
                        retry_after = float(ra)
                    except (TypeError, ValueError):
                        pass
                raise classify(resp.status, raw, model, retry_after)

            if not stream:
                raw_bytes = resp.read()
                resp_bytes = len(raw_bytes)
                raw = raw_bytes.decode("utf-8", "replace")
                data = json.loads(raw or "{}")
                msg = data.get("message") or {}
                reply = Reply(
                    model=model,
                    thinking=msg.get("thinking") or "",
                    content=msg.get("content") or "",
                    tool_calls=list(msg.get("tool_calls") or []),
                    prompt_eval_count=int(data.get("prompt_eval_count") or 0),
                    eval_count=int(data.get("eval_count") or 0),
                    done_reason=data.get("done_reason") or "stop",
                    ms=int((time.time() - started) * 1000),
                )
                return reply

            sock = getattr(getattr(resp.fp, "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(self.idle_timeout)

            reply = Reply(model=model)
            done = False
            while True:
                self._check_cancel()
                try:
                    # resp.readline() de-chunks HTTP/1.1 bodies; reading resp.fp
                    # directly would feed chunk-size lines into the NDJSON parser.
                    line = resp.readline()
                except (socket.timeout, TimeoutError) as exc:
                    raise StreamStalled(
                        f"no bytes for {self.idle_timeout}s (network switch or stalled server)"
                    ) from exc
                except (ConnectionError, OSError) as exc:
                    raise StreamStalled("connection lost mid-stream") from exc
                if not line:
                    break
                resp_bytes += len(line)
                line = line.strip()
                if not line:
                    continue
                try:
                    chunk = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(chunk, dict):
                    continue  # ignore framing / keep-alive noise
                msg = chunk.get("message") or {}
                th = msg.get("thinking")
                if th:
                    reply.thinking += th
                    if on_delta:
                        on_delta("thinking", th)
                co = msg.get("content")
                if co:
                    reply.content += co
                    if on_delta:
                        on_delta("content", co)
                for tc in msg.get("tool_calls") or []:
                    reply.tool_calls.append(tc)
                    if on_delta:
                        on_delta("tool_call", tc)
                if chunk.get("done"):
                    done = True
                    reply.prompt_eval_count = int(chunk.get("prompt_eval_count") or 0)
                    reply.eval_count = int(chunk.get("eval_count") or 0)
                    reply.done_reason = chunk.get("done_reason") or "stop"
                    break
            reply.ms = int((time.time() - started) * 1000)
            if not done:
                raise StreamStalled("stream ended before the done chunk")
            return reply
        finally:
            if got_resp:
                self._log_http("POST", "/api/chat", model, status, started, ttfb,
                               req_bytes, resp_bytes, headers, think=think,
                               format=format, prefix=prefix,
                               error_body=error_body)
            try:
                conn.close()
            except Exception:
                pass

    def _check_cancel(self) -> None:
        ev = self.cancel_event
        if ev is not None and ev.is_set():
            raise CallCancelled("cancelled by the user (Ctrl-C)")

    # -------------------------------------------------- other endpoints
    def tags(self) -> list:
        data = self._json_request("GET", "/api/tags")
        return list(data.get("models") or [])

    def web_search(self, query: str, max_results: int = 5) -> dict:
        return self._json_request("POST", "/api/web_search",
                                  {"query": query, "max_results": int(max_results)})

    def web_fetch(self, url: str) -> dict:
        return self._json_request("POST", "/api/web_fetch", {"url": url})

    def usage(self):
        """Best-effort; the endpoint is undocumented. Never raises."""
        for path in ("/api/usage", "/api/me"):
            try:
                data = self._json_request("GET", path)
                if isinstance(data, dict) and data:
                    return data
            except Exception:
                continue
        return None
