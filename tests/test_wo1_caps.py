"""Work order #1 — F5 (limits calibration), F6 (capability auto-detect),
F7 (keep-alive connection pooling).

Run from the repo root:  python -m unittest tests.test_wo1_caps -v
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO = Path(__file__).resolve().parents[1]

from tests.fake_ollama import FakeOllama


def ok_node(text="OK"):
    return {"content": text, "usage": {"in": 4, "out": 1}}


class Base(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOllama(tags=["gpt-oss:120b", "gemma4:31b"]).start()
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        self._old_key = os.environ.pop("OLLAMA_API_KEY", None)
        os.environ["HYDRA_HOME"] = self.home.name

    def tearDown(self):
        self.srv.stop()
        self.home.cleanup()
        self.cwd.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home
        if self._old_key is not None:
            os.environ["OLLAMA_API_KEY"] = self._old_key

    def cfg(self, *extra):
        from hydra import config as C
        overrides = [
            f"api.host={self.srv.url}",
            "api.retries=1",
            "termux.wake_lock=false",
            "termux.notify_on_finish=false",
            "termux.notify_on_park=false",
        ] + list(extra)
        return C.load_config(overrides=overrides, project_dir=self.cwd.name)


# ---------------------------------------------------------------- F5 limits

class LimitsTests(Base):
    def test_queue_behaviour_probe_max_6_calls_and_gate(self):
        from hydra import limits as L
        from hydra.client import OllamaCloud
        from hydra.gate import LLMGate

        for _ in range(6):
            self.srv.add("gpt-oss:20b", ok_node())
        cli = OllamaCloud(self.srv.url, "", 5, 10)
        try:
            data = L.probe(cli, self.cfg(), model="gpt-oss:20b")
        finally:
            cli.close()

        self.assertEqual(data["behaviour"], "queue")
        self.assertEqual(data["max_parallel_ok"], 4)
        self.assertLessEqual(data["calls"], 6)
        self.assertTrue(data["checked_at"])

        L.save_limits(data)
        self.assertTrue((Path(self.home.name) / "limits.json").is_file())
        self.assertEqual(L.load_limits()["behaviour"], "queue")

        # free plan queues: the gate may widen to the measured parallel width
        self.assertEqual(L.gate_capacity(self.cfg()), 4)
        gate = LLMGate(L.gate_capacity(self.cfg()), semantics=L.semantics())
        self.assertEqual(gate.plan_cap, 4)
        gate.note_rejection(429)            # queue -> shrink would not help
        self.assertEqual(gate.cap, 4)

    def test_reject_behaviour_probe_and_gate(self):
        from hydra import limits as L
        from hydra.client import OllamaCloud
        from hydra.gate import LLMGate

        self.srv.add("gpt-oss:20b", ok_node())
        self.srv.add("gpt-oss:20b", {"status": 429, "retry_after": 1,
                                     "body": {"error": "too many requests"}})
        cli = OllamaCloud(self.srv.url, "", 5, 10)
        try:
            data = L.probe(cli, self.cfg(), model="gpt-oss:20b")
        finally:
            cli.close()

        self.assertEqual(data["behaviour"], "reject:429")
        self.assertEqual(data["max_parallel_ok"], 1)
        self.assertLessEqual(data["calls"], 6)
        self.assertEqual(len(self.srv.chat_requests()), data["calls"])

        L.save_limits(data)
        self.assertEqual(L.gate_capacity(self.cfg("api.plan=pro")), 1)
        gate = LLMGate(L.gate_capacity(self.cfg("api.plan=pro")),
                       semantics=L.semantics())
        self.assertEqual(gate.plan_cap, 1)
        gate2 = LLMGate(3, semantics=L.semantics())
        gate2.note_rejection(429)
        self.assertEqual(gate2.cap, 2)      # reject:<status> keeps AIMD intact

    def test_probe_error_reports_without_writing(self):
        from hydra import limits as L
        from hydra.client import OllamaCloud

        # a port that nothing listens on: statuses never arrive
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
        probe.close()
        cli = OllamaCloud(f"http://127.0.0.1:{port}", "", 1, 1)
        try:
            with self.assertRaises(L.ProbeError):
                L.probe(cli, self.cfg(), model="gpt-oss:20b")
        finally:
            cli.close()
        self.assertFalse((Path(self.home.name) / "limits.json").exists())


# ---------------------------------------------------------------- F6 caps

class CapsTests(Base):
    def test_web_endpoints_402_remove_web_tools(self):
        from hydra.runtime import Runtime

        self.srv.stop()
        self.srv = FakeOllama(
            tags=["gpt-oss:120b", "gemma4:31b"],
            web={"search": {"_status": 402, "body": {"error": "payment required"}},
                 "fetch": {"_status": 402, "body": {"error": "payment required"}},
                 }).start()
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            self.assertFalse(rt.caps["web_search"])
            self.assertFalse(rt.caps["web_fetch"])
            lead = rt.start_run("caps probe")
            names = {t["function"]["name"] for t in rt.tools.schemas_for(lead)}
            self.assertNotIn("web_search", names)
            self.assertNotIn("web_fetch", names)
            self.assertIn("http", names)        # raw stdlib http is not hosted
            self.assertIn("read_file", names)
            env = lead.api_messages()[1]["content"]
            self.assertIn("unavailable:", env)
            self.assertIn("web_search", env)
            self.assertEqual(env.count("unavailable:"), 1)   # exactly one line
        finally:
            rt.close()

    def test_fail_open_when_detection_breaks(self):
        from hydra import caps
        from hydra import config as C

        class Flaky:
            key = "k"
            def web_search(self, *a, **k):
                raise RuntimeError("network is down")
            def web_fetch(self, *a, **k):
                raise RuntimeError("network is down")
            def tags(self):
                raise RuntimeError("network is down")

        cfg = C.load_config(overrides=[f"api.host={self.srv.url}"],
                            project_dir=self.cwd.name)
        data = caps.detect(cfg, Flaky(), which_fn=lambda n: True,
                           termux_fn=lambda: True)
        for key in caps.CAP_KEYS:
            self.assertTrue(data[key], key)

    def test_cache_is_honoured_for_24h(self):
        from hydra import caps
        from hydra import config as C

        cfg = C.load_config(overrides=[f"api.host={self.srv.url}"],
                            project_dir=self.cwd.name)
        cached = caps.fail_open()
        cached.update({"web_search": False, "web_fetch": False})
        caps.save(cached)
        with mock.patch.object(caps, "detect",
                               side_effect=AssertionError("detect must not run")):
            got = caps.get_caps(cfg, client=None)
        self.assertFalse(got["web_search"])
        self.assertFalse(got["web_fetch"])

        expired = dict(cached, checked_at=time.time() - caps.TTL_SECS - 10)
        caps.save(expired)
        caps._PROCESS_CACHE.clear()
        ran = []
        with mock.patch.object(caps, "detect",
                               side_effect=lambda *a, **k: ran.append(True) or
                               caps.fail_open()):
            got = caps.get_caps(cfg, client=None)
        self.assertTrue(ran, "stale cache must be re-checked")
        self.assertTrue(got["web_search"])

    def test_missing_tools_are_reported_and_hidden(self):
        from hydra import caps
        from hydra import config as C

        cfg = C.load_config(overrides=[f"api.host={self.srv.url}"],
                            project_dir=self.cwd.name)
        data = caps.detect(cfg, None,
                           which_fn=lambda n: n in ("rg", "git"),
                           termux_fn=lambda: False)
        self.assertFalse(data["tmux"])
        self.assertFalse(data["termux_api"])
        line = caps.env_line(data)
        self.assertIn("tmux", line)
        self.assertIn("termux_*", line)
        self.assertNotIn("web_search", caps.unavailable_names(data))
        from hydra.tools import caps_allowed
        self.assertFalse(caps_allowed("termux_notify", data))
        self.assertTrue(caps_allowed("view_image", data))   # vision still fine

    def test_doctor_caps_forces_a_recheck(self):
        env = dict(os.environ)
        env["HYDRA_HOME"] = self.home.name
        env["PYTHONPATH"] = str(REPO)
        env.pop("OLLAMA_API_KEY", None)
        out = subprocess.run(
            [sys.executable, "-m", "hydra", "--set", f"api.host={self.srv.url}",
             "doctor", "--caps"],
            capture_output=True, text=True, env=env, cwd=self.cwd.name, timeout=90)
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("web_search", out.stdout)
        self.assertIn("wrote", out.stdout)
        self.assertTrue((Path(self.home.name) / "caps.json").is_file())


# ---------------------------------------------------------------- F7 keep-alive

class KeepAliveTests(unittest.TestCase):
    def setUp(self):
        # default FakeOllama(keepalive=None): auto mode — the client must ask
        # for keep-alive with its Connection header for the socket to persist.
        self.srv = FakeOllama(tags=["gpt-oss:120b", "gemma4:31b"]).start()
        self.home = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name

    def tearDown(self):
        self.srv.stop()
        self.home.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home

    def _client(self):
        from hydra.client import OllamaCloud
        return OllamaCloud(self.srv.url, "", 5, 10)

    def test_single_accept_for_10_sequential_streams(self):
        for _ in range(10):
            self.srv.add("gpt-oss:20b", ok_node())
        cli = self._client()
        try:
            for _ in range(10):
                reply = cli.chat("gpt-oss:20b", [{"role": "user", "content": "hi"}],
                                 stream=True)
                self.assertEqual(reply.content, "OK")
        finally:
            cli.close()
        self.assertEqual(self.srv.accepts, 1)          # one accept, ten calls
        self.assertEqual(len(self.srv.chat_requests()), 10)

    def test_reconnect_on_stale_socket(self):
        for _ in range(2):
            self.srv.add("gpt-oss:20b", ok_node())
        cli = self._client()
        try:
            cli.chat("gpt-oss:20b", [{"role": "user", "content": "one"}], stream=True)
            self.assertEqual(self.srv.accepts, 1)
            conn = cli._pool.conn
            conn.sock.close()                          # server-side death without FIN seen
            reply = cli.chat("gpt-oss:20b", [{"role": "user", "content": "two"}],
                             stream=True)
            self.assertEqual(reply.content, "OK")
        finally:
            cli.close()
        self.assertEqual(self.srv.accepts, 2)          # exactly one reconnect

    def test_f4_http_jsonl_still_logged_per_call(self):
        from hydra.httpdebug import HttpLogger

        for _ in range(10):
            self.srv.add("gpt-oss:20b", ok_node())
        path = Path(self.home.name) / "http.jsonl"
        cli = self._client()
        cli.http_log = HttpLogger(path)
        try:
            for _ in range(10):
                cli.tags()
        finally:
            cli.close()
        lines = [json.loads(ln) for ln in
                 path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(lines), 10)
        self.assertTrue(all(r["method"] == "GET" and r["path"] == "/api/tags"
                            for r in lines))
        self.assertEqual(self.srv.accepts, 1)

    def test_nonstream_reuses_connection(self):
        for _ in range(3):
            self.srv.add("gpt-oss:20b", ok_node())
        cli = self._client()
        try:
            for _ in range(3):
                reply = cli.chat("gpt-oss:20b", [{"role": "user", "content": "hi"}],
                                 stream=False)
                self.assertEqual(reply.content, "OK")
        finally:
            cli.close()
        self.assertEqual(self.srv.accepts, 1)

    def test_server_without_keepalive_closes_each_call(self):
        # forced close: auto mode must not pretend the socket persisted
        self.srv.stop()
        self.srv = FakeOllama(keepalive=False,
                              tags=["gpt-oss:120b", "gemma4:31b"]).start()
        for _ in range(3):
            self.srv.add("gpt-oss:20b", ok_node())
        cli = self._client()
        try:
            for _ in range(3):
                cli.tags()
        finally:
            cli.close()
        self.assertEqual(self.srv.accepts, 3)


if __name__ == "__main__":
    unittest.main()
