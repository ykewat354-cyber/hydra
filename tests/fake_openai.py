"""Minimal OpenAI-compatible fake server for W5-1 tests.

Supports the two wire shapes HYDRA needs: ``/v1/chat/completions`` streaming
SSE and ``/v1/models`` discovery.  No third-party packages and no real keys.
"""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeOpenAI:
    def __init__(self, script=None, *, tags=None, require_auth=None,
                 host="127.0.0.1", keepalive=True):
        self.script = {m: list(v) for m, v in (script or {}).items()}
        self.tags = list(tags or []) if tags is not None else None
        self.require_auth = require_auth
        self.keepalive = bool(keepalive)
        self.requests = []
        self.seen_models = []
        self.accepts = 0
        self.lock = threading.Lock()
        self._httpd = _Httpd((host, 0), _Handler)
        self._httpd.fake = self
        self._thread = None

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._httpd.server_address[1]

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    def start(self):
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread:
            self._thread.join(timeout=5)

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    def add(self, model, *nodes):
        with self.lock:
            self.script.setdefault(model, []).extend(nodes)

    def next_node(self, model):
        with self.lock:
            for key in (model, "*"):
                q = self.script.get(key)
                if q:
                    return q.pop(0)
        return {"status": 500, "body": {"error": f"fake-openai: script exhausted for {model}"}}

    def chat_requests(self):
        return [r for r in self.requests
                if r["path"].rstrip("/").endswith("/chat/completions")]


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
        pass


def _split(text, n):
    n = max(1, int(n))
    if n == 1 or len(text) <= n:
        return [text]
    size = max(1, len(text) // n)
    return [text[i:i + size] for i in range(0, len(text), size)]


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def fake(self) -> FakeOpenAI:
        return self.server.fake

    def log_message(self, *args):
        pass

    def _keepalive(self):
        token = (self.headers.get("Connection") or "").lower()
        if self.fake.keepalive and "close" not in token:
            self.close_connection = False
        else:
            self.close_connection = True

    def _send(self, raw: bytes, status=200, ctype="application/json", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(raw)))
        if self.close_connection:
            self.send_header("Connection", "close")
        else:
            self.send_header("Connection", "keep-alive")
        for k, v in (extra or {}).items():
            self.send_header(k, str(v))
        self.end_headers()
        try:
            self.wfile.write(raw)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, status=200, extra=None):
        self._send(json.dumps(obj).encode(), status, "application/json", extra)

    def do_GET(self):
        self._keepalive()
        with self.fake.lock:
            self.fake.requests.append({"path": self.path, "method": "GET"})
        if self.path.rstrip("/").endswith("/models"):
            if self.fake.tags is None:
                self._json({"error": {"message": "models not supported"}}, 404)
                return
            data = [{"id": name, "object": "model", "owned_by": "fake"}
                    for name in self.fake.tags]
            self._json({"object": "list", "data": data})
            return
        self._json({"error": {"message": "not found"}}, 404)

    def do_POST(self):
        self._keepalive()
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            body = {}
        auth = self.headers.get("Authorization") or ""
        with self.fake.lock:
            self.fake.requests.append({"path": self.path, "method": "POST",
                                       "body": body, "authorization": auth})
        if self.fake.require_auth and auth != self.fake.require_auth:
            self._json({"error": {"message": "invalid api key"}}, 401)
            return
        if not self.path.rstrip("/").endswith("/chat/completions"):
            self._json({"error": {"message": "not found"}}, 404)
            return
        model = str(body.get("model") or "?")
        with self.fake.lock:
            self.fake.seen_models.append(model)
        node = self.fake.next_node(model)
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
            self._json(node.get("body") or {"error": {"message": "fake error"}},
                       int(node["status"]), extra)
            return
        if body.get("stream"):
            self._stream(node, model)
        else:
            self._complete(node, model)

    def _complete(self, node, model):
        calls = []
        for i, tc in enumerate(node.get("tool_calls") or []):
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            calls.append({"id": tc.get("id") or f"call_{i}", "type": "function",
                          "function": {"name": fn.get("name") or "",
                                       "arguments": _args(fn.get("arguments"))}})
        msg = {"role": "assistant",
               "content": str(node.get("content") or ""),
               "tool_calls": calls or None}
        if not calls:
            msg.pop("tool_calls")
        usage = node.get("usage") or {}
        self._json({"id": "chatcmpl-fake", "object": "chat.completion",
                    "model": model,
                    "choices": [{"index": 0, "message": msg,
                                 "finish_reason": node.get("finish_reason") or
                                 ("tool_calls" if calls else "stop")}],
                    "usage": {"prompt_tokens": int(usage.get("in") or 0),
                              "completion_tokens": int(usage.get("out") or 0)}})

    def _stream(self, node, model):
        frames = []
        content = str(node.get("content") or "")
        chunks = _split(content, int(node.get("chunks") or 1))
        frames.append({"choices": [{"index": 0, "delta": {"role": "assistant",
                                                          "content": ""},
                                    "finish_reason": None}]})
        for part in chunks:
            if part:
                frames.append({"choices": [{"index": 0,
                                            "delta": {"content": part},
                                            "finish_reason": None}]})
        thinking = str(node.get("thinking") or node.get("reasoning") or "")
        for part in _split(thinking, int(node.get("think_chunks") or 1)):
            if part:
                frames.append({"choices": [{"index": 0,
                                            "delta": {"reasoning_content": part},
                                            "finish_reason": None}]})
        raw_calls = []
        for i, tc in enumerate(node.get("tool_calls") or []):
            fn = tc.get("function") if isinstance(tc.get("function"), dict) else tc
            raw_calls.append((tc.get("id") or f"call_{i}", fn.get("name") or "",
                              _args(fn.get("arguments")),
                              tc.get("arg_chunks") or node.get("arg_chunks") or 2))
        for i, (cid, name, args, n) in enumerate(raw_calls):
            parts = _split(args, int(n))
            for j, part in enumerate(parts):
                delta = {"tool_calls": [{"index": i,
                                         "function": {"arguments": part}}]}
                if j == 0:
                    delta["tool_calls"][0].update({"id": cid, "type": "function"})
                    delta["tool_calls"][0]["function"]["name"] = name
                frames.append({"choices": [{"index": 0, "delta": delta,
                                            "finish_reason": None}]})
        reason = node.get("finish_reason") or ("tool_calls" if raw_calls else "stop")
        frames.append({"choices": [{"index": 0, "delta": {},
                                    "finish_reason": reason}]})
        usage = node.get("usage") or {}
        frames.append({"choices": [], "usage": {
            "prompt_tokens": int(usage.get("in") or 0),
            "completion_tokens": int(usage.get("out") or 0)}})
        body = b"".join(b"data: " + json.dumps(f, ensure_ascii=False).encode() +
                        b"\n\n" for f in frames)
        body += b"data: [DONE]\n\n"
        delay = float(node.get("delay_chunks") or 0)
        if delay:
            # Rebuild in timed pieces without changing the visible stream.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            for i in range(0, len(body), 4096):
                self.wfile.write(body[i:i + 4096])
                self.wfile.flush()
                time.sleep(delay)
            return
        self._send(body, 200, "text/event-stream")


def _args(value):
    if isinstance(value, str):
        return value
    return json.dumps(value if value is not None else {}, ensure_ascii=False)
