"""Phase 0 acceptance tests: config loader + fake Ollama server.

Run from the repo root:  python -m unittest -v
"""
from __future__ import annotations

import json
import os
import socket
import tempfile
import threading
import time
import unittest
from http.client import HTTPConnection

import urllib.error
import urllib.request

from hydra import config as C
from tests.fake_ollama import FakeOllama


def _post(url, path, obj, timeout=5):
    data = json.dumps(obj).encode()
    req = urllib.request.Request(url + path, data=data,
                                 headers={"Content-Type": "application/json",
                                          "Authorization": "Bearer test"},
                                 method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, dict(r.headers), r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read().decode()


def _stream(url, path, obj, timeout=5, idle=2.0):
    """POST and read NDJSON line by line; returns (status, [parsed lines])."""
    hostport = url.split("://", 1)[1]
    conn = HTTPConnection(hostport, timeout=timeout)
    conn.request("POST", path, json.dumps(obj).encode(),
                 {"Content-Type": "application/json", "Authorization": "Bearer test"})
    resp = conn.getresponse()
    # reach the underlying socket through the response's buffered reader
    sock = getattr(getattr(resp.fp, "raw", None), "_sock", None)
    if sock is not None:
        sock.settimeout(idle or timeout)
    lines = []
    try:
        while True:
            raw = resp.fp.readline()
            if not raw:
                break
            raw = raw.strip()
            if raw:
                lines.append(json.loads(raw))
    finally:
        conn.close()
    return resp.status, lines


def _assemble(lines):
    """Accumulate NDJSON chunks like the real client will."""
    thinking, content, tools, usage = "", "", [], {}
    for ln in lines:
        msg = ln.get("message") or {}
        thinking += msg.get("thinking") or ""
        content += msg.get("content") or ""
        for tc in msg.get("tool_calls") or []:
            tools.append(tc)
        if ln.get("done"):
            usage = {"in": ln.get("prompt_eval_count"), "out": ln.get("eval_count")}
    return {"thinking": thinking, "content": content, "tool_calls": tools, "usage": usage}


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        os.environ["HYDRA_HOME"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("HYDRA_HOME", None)
        self.tmp.cleanup()
        self.cwd.cleanup()

    def test_defaults(self):
        cfg = C.load_config(project_dir=self.cwd.name)
        self.assertEqual(C.get(cfg, "api.plan"), "free")
        self.assertEqual(C.max_inflight(cfg), 1)
        self.assertEqual(C.get(cfg, "roles.lead.chain")[0], "gpt-oss:120b")
        self.assertEqual(C.get(cfg, "loop.tool_cap"), 6000)

    def test_plan_levels(self):
        cfg = C.load_config(overrides=["api.plan=pro"], project_dir=self.cwd.name)
        self.assertEqual(C.max_inflight(cfg), 3)
        cfg = C.load_config(overrides=["api.plan=max"], project_dir=self.cwd.name)
        self.assertEqual(C.max_inflight(cfg), 10)
        cfg = C.load_config(overrides=["api.max_inflight=2"], project_dir=self.cwd.name)
        self.assertEqual(C.max_inflight(cfg), 2)

    def test_set_overrides_typed(self):
        cfg = C.load_config(overrides=["loop.working_ctx=1500", "roles.lead.think=high"],
                            project_dir=self.cwd.name)
        self.assertEqual(C.get(cfg, "loop.working_ctx"), 1500)
        self.assertIsInstance(C.get(cfg, "loop.working_ctx"), int)
        self.assertEqual(C.get(cfg, "roles.lead.think"), "high")

    def test_project_layer_wins(self):
        d = os.path.join(self.cwd.name, ".hydra")
        os.makedirs(d)
        with open(os.path.join(d, "config.toml"), "w") as fh:
            fh.write('[loop]\ntool_cap = 1234\n[roles.worker]\nthink = "off"\n')
        cfg = C.load_config(project_dir=self.cwd.name)
        self.assertEqual(C.get(cfg, "loop.tool_cap"), 1234)
        self.assertEqual(C.get(cfg, "roles.worker.think"), "off")
        # other keys untouched
        self.assertEqual(C.get(cfg, "loop.parallel_tools"), 4)

    def test_key_resolution_env_then_file(self):
        cfg = C.load_config(project_dir=self.cwd.name)
        os.environ["OLLAMA_API_KEY"] = "env-key"
        try:
            self.assertEqual(C.load_key(cfg), "env-key")
        finally:
            os.environ.pop("OLLAMA_API_KEY")
        self.assertIsNone(C.load_key(cfg))
        C.home().mkdir(parents=True, exist_ok=True)
        (C.home() / "key").write_text("file-key\n", encoding="utf-8")
        self.assertEqual(C.load_key(cfg), "file-key")

    def test_ensure_home(self):
        cfg = C.load_config(project_dir=self.cwd.name)
        C.ensure_home(cfg)
        for sub in ("memory", "tools", "roles", "skills", "commands", "queue", "runs"):
            self.assertTrue((C.home() / sub).is_dir(), sub)
        self.assertTrue((C.home() / "config.toml").is_file())
        self.assertTrue((C.home() / "prices.toml").is_file())
        self.assertIn("gpt-oss:120b", (C.home() / "prices.toml").read_text())
        # existing files are never overwritten
        (C.home() / "config.toml").write_text("[api]\nplan = 'pro'\n", encoding="utf-8")
        C.ensure_home(cfg)
        self.assertIn("pro", (C.home() / "config.toml").read_text())

    def test_prices_loading(self):
        cfg = C.load_config(project_dir=self.cwd.name)
        C.ensure_home(cfg)
        prices = C.load_prices(cfg)
        self.assertEqual(prices["gpt-oss:120b"]["out"], 0.60)


class FakeServerTests(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOllama(tags=["gpt-oss:120b", "gemma4:31b"],
                              web={"search": {"results": [{"title": "T", "url": "u", "content": "c"}]}},
                              require_auth="Bearer test").start()

    def tearDown(self):
        self.srv.stop()

    def test_tags_and_auth(self):
        req = urllib.request.Request(self.srv.url + "/api/tags")
        with urllib.request.urlopen(req) as r:
            data = json.loads(r.read())
        self.assertEqual([m["name"] for m in data["models"]], ["gpt-oss:120b", "gemma4:31b"])
        # no Authorization header -> 401 (server configured with require_auth)
        req2 = urllib.request.Request(
            self.srv.url + "/api/chat",
            data=json.dumps({"model": "gpt-oss:120b", "messages": []}).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req2, timeout=3)
        self.assertEqual(cm.exception.code, 401)

    def test_scripted_three_turn_conversation(self):
        s = self.srv
        s.add("gpt-oss:120b",
              {"content": "Adding.", "thinking": "use add",
               "tool_calls": [{"function": {"name": "add", "arguments": {"a": 2, "b": 3}}}]},
              {"content": "The sum is 5."},
              {"content": "Done.",
               "tool_calls": [{"function": {"name": "finish", "arguments": {"status": "done", "summary": "5"}}}]})

        msgs = [{"role": "user", "content": "what is 2+3? use the add tool"}]
        st, lines = _stream(s.url, "/api/chat", {"model": "gpt-oss:120b", "messages": msgs, "stream": True})
        self.assertEqual(st, 200)
        r1 = _assemble(lines)
        self.assertEqual(r1["tool_calls"][0]["function"]["name"], "add")
        self.assertEqual(r1["thinking"], "use add")
        self.assertTrue(r1["usage"]["in"] > 0 and r1["usage"]["out"] > 0)

        msgs += [{"role": "assistant", "content": r1["content"], "thinking": r1["thinking"],
                  "tool_calls": r1["tool_calls"]},
                 {"role": "tool", "tool_name": "add", "content": "5"}]
        st, lines = _stream(s.url, "/api/chat", {"model": "gpt-oss:120b", "messages": msgs, "stream": True})
        r2 = _assemble(lines)
        self.assertEqual(r2["content"], "The sum is 5.")
        self.assertEqual(r2["tool_calls"], [])

        msgs += [{"role": "assistant", "content": r2["content"]}]
        st, lines = _stream(s.url, "/api/chat", {"model": "gpt-oss:120b", "messages": msgs, "stream": True})
        r3 = _assemble(lines)
        self.assertEqual(r3["tool_calls"][0]["function"]["name"], "finish")

        self.assertEqual(len(s.chat_requests()), 3)
        self.assertEqual(s.seen_models, ["gpt-oss:120b"] * 3)

    def test_tool_arguments_as_json_string(self):
        s = self.srv
        s.add("gpt-oss:120b", {"tool_calls": [
            {"function": {"name": "add", "arguments": '{"a": 1, "b": 2}'}}]})
        st, lines = _stream(s.url, "/api/chat", {"model": "gpt-oss:120b", "messages": []})
        r = _assemble(lines)
        self.assertEqual(r["tool_calls"][0]["function"]["arguments"], '{"a": 1, "b": 2}')

    def test_error_modes(self):
        s = self.srv
        s.add("minimax-m3", {"status": 402, "body": {"error": "this model requires a subscription"}})
        st, hdrs, body = _post(s.url, "/api/chat", {"model": "minimax-m3", "messages": []})
        self.assertEqual(st, 402)
        self.assertIn("subscription", body)

        s.add("gpt-oss:20b", {"status": 429, "retry_after": 3, "body": {"error": "too many requests"}})
        st, hdrs, _ = _post(s.url, "/api/chat", {"model": "gpt-oss:20b", "messages": []})
        self.assertEqual(st, 429)
        self.assertEqual(hdrs.get("Retry-After"), "3")

        s.add("ghost:1b", {"status": 404, "body": {"error": "model 'ghost:1b' not found"}})
        st, _, _ = _post(s.url, "/api/chat", {"model": "ghost:1b", "messages": []})
        self.assertEqual(st, 404)

        st, _, body = _post(s.url, "/api/chat", {"model": "no-script", "messages": []})
        self.assertEqual(st, 500)
        self.assertIn("script exhausted", body)

    def test_stall_and_mid_fail(self):
        s = self.srv
        s.add("gpt-oss:120b", {"content": "partial answer that arrives", "chunks": 1,
                               "tail_delay": 2.0, "mid_fail": True})
        with self.assertRaises((socket.timeout, TimeoutError)):
            _stream(s.url, "/api/chat", {"model": "gpt-oss:120b", "messages": []}, idle=0.3)
        # partial content was delivered, then the stream stalled > idle timeout

    def test_max_inflight_tracking(self):
        s = self.srv
        for _ in range(2):
            s.add("gpt-oss:120b", {"content": "x" * 40, "chunks": 4, "delay_chunks": 0.05})
        t1 = threading.Thread(target=_stream, args=(s.url, "/api/chat", {"model": "gpt-oss:120b", "messages": []}))
        t2 = threading.Thread(target=_stream, args=(s.url, "/api/chat", {"model": "gpt-oss:120b", "messages": []}))
        t1.start(); t2.start(); t1.join(); t2.join()
        deadline = time.time() + 5
        while s.inflight != 0 and time.time() < deadline:
            time.sleep(0.02)
        self.assertEqual(s.max_inflight, 2)
        self.assertEqual(s.inflight, 0)

    def test_web_endpoints(self):
        st, _, body = _post(self.srv.url, "/api/web_search", {"query": "hi"})
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body)["results"][0]["title"], "T")


if __name__ == "__main__":
    unittest.main()
