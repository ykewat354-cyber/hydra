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
    def __init__(self, script=None, *, tags=None, web=None, require_auth=None, host="127.0.0.1"):
        self.script = {m: list(v) for m, v in (script or {}).items()}
        self.tags = list(tags or [])
        self.web = dict(web or {})
        self.require_auth = require_auth
        self.requests = []          # every request, in order
        self.seen_models = []       # model name per /api/chat call
        self.lock = threading.Lock()
        self.inflight = 0
        self.max_inflight = 0
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


def _split(text: str, n: int) -> list:
    n = max(1, int(n))
    if n == 1 or len(text) <= n:
        return [text]
    size = max(1, len(text) // n)
    parts = [text[i:i + size] for i in range(0, len(text), size)]
    return parts or [""]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

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
            self._json(f.web.get("search", {"results": []}))
            return
        if self.path == "/api/web_fetch":
            self._json(f.web.get("fetch", {"title": "", "content": ""}))
            return
        self._json({"error": "not found"}, 404)

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
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.end_headers()

            thinking = node.get("thinking") or ""
            content = node.get("content") or ""
            tools = node.get("tool_calls") or []
            chunks = int(node.get("chunks", 3) or 3)
            delay_chunks = float(node.get("delay_chunks", 0) or 0)

            def send(obj):
                self.wfile.write((json.dumps(obj) + "\n").encode())
                self.wfile.flush()

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
        finally:
            with f.lock:
                f.inflight -= 1
