"""Scripted fake Ollama Cloud server for tests and evals — zero credits burned.

A script is a mapping ``model name -> list of nodes``, served in order.
Node kinds (dicts unless noted):

* stream node::

      {"thinking": str, "content": str,
       "tool_calls": [{"function": {"name": str, "arguments": dict|str}}],
       "usage": {"in": int, "out": int},
       "chunks": int,          # split text into N streamed NDJSON lines
       "delay_chunks": float,  # sleep between chunks
       "delay": float}         # sleep before headers

* error node: ``{"status": 402|404|429|400|503, "body": dict|str, "retry_after": sec}``
* abort node: add ``"tail_delay": sec`` and/or ``"mid_fail": true`` to a stream node
  to stall or kill the connection mid-stream.
* callable: ``fn(request_body) -> node`` evaluated at serve time.

Use model key ``"*"`` as a fallback queue shared by any model. When a queue is
exhausted the server answers 500 with ``"script exhausted"`` so tests fail loudly
instead of hanging.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOllama:
    def __init__(self, script=None, *, tags=None, web=None, require_auth=None,
                 host="127.0.0.1", keepalive=None):
        self.script = {m: list(v) for m, v in (script or {}).items()}
        self.tags = list(tags or [])
        self.web = dict(web or {})
        self.require_auth = require_auth
        # None = auto: persist only when the client asks for keep-alive;
        # True = force HTTP/1.1 keep-alive; False = force close-each-time.
        self.keepalive = keepalive
        self.requests = []          # every request, in order
        self.seen_models = []       # model name per /api/chat call
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
        self.accepts = 0            # TCP connections accepted (F7 evidence)
        self._httpd = _Httpd((host, 0), _Handler)
        self._httpd.fake = self
        self._thread = None

    # ---------------------------------------------------------- lifecycle
    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._httpd.server_address[1]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self) -> "FakeOllama":
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self) -> "FakeOllama":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()

    # ---------------------------------------------------------- scripting
    def add(self, model: str, *nodes) -> None:
        with self.lock:
            self.script.setdefault(model, []).extend(nodes)

    def pending(self, model: str = "*") -> int:
        with self.lock:
            return len(self.script.get(model, []))

    def next_node(self, model: str):
        with self.lock:
            for key in (model, "*"):
                q = self.script.get(key)
                if q:
                    return q.pop(0)
        return {"status": 500, "body": {"error": f"fake-ollama: script exhausted for model {model}"}}

    def chat_requests(self) -> list:
        return [r for r in self.requests if r["path"] == "/api/chat"]

    def reset(self) -> None:
        with self.lock:
            self.requests.clear()
            self.seen_models.clear()
            self.inflight = 0
            self.max_inflight = 0


class _Httpd(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        sock, addr = super().get_request()
        fake = getattr(self, "fake", None)
        if fake is not None:
            with fake.lock:
                fake.accepts += 1
        return sock, addr

    def handle_error(self, request, client_address):
        pass  # clients aborting mid-stream (SIGINT tests) are expected


def _split(text: str, n: int) -> list:
    n = max(1, int(n))
    if n == 1 or len(text) <= n:
        return [text]
    size = max(1, len(text) // n)
    parts = [text[i:i + size] for i in range(0, len(text), size)]
    return parts or [""]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def _prepare_keepalive(self):
        """Decide per request whether the socket persists (F7 auto/opt-in).

        ``fake.keepalive is None`` (default) = auto: persist only when the
        request asks for it (``Connection: keep-alive``), so raw test helpers
        keep their HTTP/1.0 close-each-time framing.  ``True`` forces
        HTTP/1.1 keep-alive; ``False`` forces a close after every response.
        """
        fake = getattr(self.server, "fake", None)
        want = getattr(fake, "keepalive", None)
        if want is None:
            token = (self.headers.get("Connection") or "").lower()
            want = "keep-alive" in token
        if want:
            self.protocol_version = "HTTP/1.1"
            self.close_connection = False
        else:
            self.protocol_version = "HTTP/1.0"
            self.close_connection = True

    def log_message(self, *args):  # keep test output clean
        pass

    @property
    def fake(self) -> FakeOllama:
        return self.server.fake

    # ---------------------------------------------------------- helpers
    def _json(self, obj, status=200, extra=None):
        raw = json.dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        self.wfile.write(raw)

    # ---------------------------------------------------------- routes
    def do_GET(self):
        self._prepare_keepalive()
        f = self.fake
        if self.path == "/api/tags":
            with f.lock:
                f.requests.append({"path": "/api/tags", "method": "GET"})
            models = [{"name": n, "model": n, "size": 0,
                       "details": {"family": n.split(":")[0], "parameter_size": "?"}}
                      for n in f.tags]
            self._json({"models": models})
            return
        if self.path == "/api/version":
            self._json({"version": "fake-0.0"})
            return
        if self.path in ("/api/usage", "/api/me"):
            self._json({"plan": "free", "fake": True})
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        self._prepare_keepalive()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {"_raw": raw.decode("utf-8", "replace")}
        f = self.fake
        auth = self.headers.get("Authorization") or ""
        with f.lock:
            f.requests.append({"path": self.path, "method": "POST", "body": body, "authorization": auth})
        if f.require_auth and auth != f.require_auth:
            self._json({"error": "unauthorized"}, 401)
            return
        if self.path == "/api/chat":
            self._chat(body)
            return
        if self.path == "/api/web_search":
            self._serve_web("search", {"results": []})
            return
        if self.path == "/api/web_fetch":
            self._serve_web("fetch", {"title": "", "content": ""})
            return
        self._json({"error": "not found"}, 404)

    def _serve_web(self, key: str, default):
        """A web stub: plain result, callable, or {"_status": N, "body": ...}."""
        f = self.fake
        val = f.web.get(key, default)
        if callable(val):
            try:
                val = val(self.headers.get("Authorization") or "")
            except TypeError:
                val = val()
            if val is None:
                val = default
        if isinstance(val, dict) and "_status" in val:
            self._json(val.get("body") or {"error": "stubbed"}, int(val["_status"]))
            return
        self._json(val)

    # ---------------------------------------------------------- chat
    def _chat(self, body):
        f = self.fake
        model = str(body.get("model", "?"))
        with f.lock:
            f.seen_models.append(model)
        node = f.next_node(model)
        if callable(node):
            node = node(body)
        if not isinstance(node, dict):
            node = {"content": str(node)}
        if "delay" in node:
            time.sleep(float(node["delay"]))
        if "status" in node:
            extra = {}
            if node.get("retry_after") is not None:
                extra["Retry-After"] = node["retry_after"]
            b = node.get("body", {"error": "fake error"})
            self._json(b if isinstance(b, dict) else {"error": str(b)}, int(node["status"]), extra)
            return

        with f.lock:
            f.inflight += 1
            f.max_inflight = max(f.max_inflight, f.inflight)
        # Chunked framing only when this socket actually persists: an
        # HTTP/1.0 close-delimited body must not be chunked.
        chunked = not self.close_connection
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()

            thinking = node.get("thinking") or ""
            content = node.get("content") or ""
            tools = node.get("tool_calls") or []
            chunks = int(node.get("chunks", 3) or 3)
            delay_chunks = float(node.get("delay_chunks", 0) or 0)

            def send(obj):
                payload = (json.dumps(obj) + "\n").encode()
                if chunked:
                    self.wfile.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                else:
                    self.wfile.write(payload)
                self.wfile.flush()

            if not body.get("stream", True):
                # stream=false: one complete JSON document, like the real API.
                send({"model": model, "created_at": "fake", "done": True,
                      "done_reason": "stop",
                      "message": {"role": "assistant", "thinking": thinking,
                                  "content": content, "tool_calls": tools},
                      "prompt_eval_count": int((node.get("usage") or {}).get("in") or 4),
                      "eval_count": int((node.get("usage") or {}).get("out") or 1)})
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
                return

            def chunk(msg):
                send({"model": model, "created_at": "fake",
                      "message": {"role": "assistant", **msg}, "done": False})

            if thinking:
                for part in _split(thinking, chunks):
                    chunk({"thinking": part, "content": ""})
                    if delay_chunks:
                        time.sleep(delay_chunks)
            if content:
                for part in _split(content, chunks):
                    chunk({"thinking": "", "content": part})
                    if delay_chunks:
                        time.sleep(delay_chunks)
            if tools:
                chunk({"content": "", "tool_calls": tools})  # whole, one chunk

            if node.get("tail_delay"):
                time.sleep(float(node["tail_delay"]))
            if node.get("mid_fail"):
                try:
                    self.connection.close()
                except Exception:
                    pass
                return

            usage = node.get("usage") or {}
            pin = usage.get("in")
            eout = usage.get("out")
            if pin is None:
                pin = max(1, sum(len(str(m.get("content") or "")) for m in body.get("messages", [])) // 4)
            if eout is None:
                eout = max(1, (len(thinking) + len(content)) // 4)
            send({"model": model, "created_at": "fake",
                  "message": {"role": "assistant", "content": ""},
                  "done": True, "done_reason": "stop",
                  "prompt_eval_count": pin, "eval_count": eout,
                  "total_duration": 1000000, "eval_duration": 1000000})
            if chunked:
                self.wfile.write(b"0\r\n\r\n")
                self.wfile.flush()
        finally:
            with f.lock:
                f.inflight -= 1
