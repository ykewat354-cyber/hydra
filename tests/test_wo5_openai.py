"""WO5 W5-1 — OpenAI-compatible transport and protocol selection."""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
import sys
sys.path.insert(0, str(REPO))

from tests.fake_openai import FakeOpenAI          # noqa: E402
from hydra import config as C                    # noqa: E402
from hydra.client import (AuthError, ModelMissing, OllamaCloud,  # noqa: E402
                          OpenAiClient, PaidModel, RateLimited, make_client)


class OpenAiBase(unittest.TestCase):
    def setUp(self):
        self.srv = FakeOpenAI(tags=["test-model"],
                              require_auth="Bearer test-key").start()
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name
        self.client = OpenAiClient(self.srv.url + "/v1", "test-key", 5, 5)

    def tearDown(self):
        self.client.close()
        self.srv.stop()
        self.home.cleanup()
        self.cwd.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home


class OpenAiTransportTests(OpenAiBase):
    def test_streaming_text_and_usage(self):
        self.srv.add("test-model", {"content": "hello world", "chunks": 3,
                                    "usage": {"in": 11, "out": 2}})
        reply = self.client.chat("test-model", [{"role": "user", "content": "hi"}])
        self.assertEqual(reply.content, "hello world")
        self.assertEqual((reply.prompt_eval_count, reply.eval_count), (11, 2))
        self.assertEqual(reply.done_reason, "stop")
        req = self.srv.chat_requests()[0]
        self.assertEqual(req["path"], "/v1/chat/completions")
        self.assertEqual(req["authorization"], "Bearer test-key")
        self.assertTrue(req["body"]["stream"])
        self.assertEqual(req["body"]["messages"][-1]["content"], "hi")

    def test_tool_call_deltas_are_assembled(self):
        self.srv.add("test-model", {
            "content": "working",
            "tool_calls": [
                {"id": "call_a", "name": "add",
                 "arguments": {"a": 2, "b": 3}, "arg_chunks": 4},
                {"id": "call_b", "function": {"name": "ping", "arguments": "{}"}},
            ],
            "usage": {"in": 7, "out": 9},
        })
        reply = self.client.chat(
            "test-model", [{"role": "user", "content": "go"}],
            tools=[{"type": "function", "function": {"name": "add"}}])
        self.assertEqual(reply.done_reason, "tool_calls")
        self.assertEqual(len(reply.tool_calls), 2)
        first = reply.tool_calls[0]
        self.assertEqual(first["id"], "call_a")
        self.assertEqual(first["function"]["name"], "add")
        self.assertEqual(first["function"]["arguments"], {"a": 2, "b": 3})
        self.assertEqual(reply.tool_calls[1]["function"]["name"], "ping")

    def test_history_is_converted_and_keepalive_reused(self):
        self.srv.add("test-model", {"content": "one"})
        self.srv.add("test-model", {"content": "two"})
        messages = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": "u"},
            {"role": "assistant", "content": "",
             "tool_calls": [{"function": {"name": "add",
                                          "arguments": {"a": 1}}}]},
            {"role": "tool", "tool_name": "add", "content": "1"},
            {"role": "user", "content": "next"},
        ]
        self.client.chat("test-model", messages)
        self.client.chat("test-model", [{"role": "user", "content": "again"}])
        self.assertEqual(self.srv.accepts, 1, "keep-alive socket was not reused")
        sent = self.srv.chat_requests()[0]["body"]["messages"]
        assistant = [m for m in sent if m["role"] == "assistant"][0]
        tool = [m for m in sent if m["role"] == "tool"][0]
        self.assertEqual(assistant["tool_calls"][0]["function"]["arguments"],
                         '{"a": 1}')
        self.assertEqual(tool["tool_call_id"],
                         assistant["tool_calls"][0]["id"])
        self.assertNotIn("think", self.srv.chat_requests()[0]["body"])

    def test_non_stream_and_models_discovery(self):
        self.srv.add("test-model", {"content": "complete",
                                    "usage": {"in": 3, "out": 4}})
        reply = self.client.chat("test-model", [{"role": "user", "content": "x"}],
                                 stream=False)
        self.assertEqual(reply.content, "complete")
        self.assertEqual(reply.eval_count, 4)
        tags = self.client.tags()
        self.assertEqual([t["name"] for t in tags], ["test-model"])

    def test_error_matrix(self):
        self.srv.add("test-model", {"status": 401, "body": {"error": "bad"}})
        self.srv.add("test-model", {"status": 429, "body": {"error": "slow"},
                                    "retry_after": 2.5})
        self.srv.add("test-model", {"status": 402, "body": {"error": "credits"}})
        with self.assertRaises(AuthError):
            self.client.chat("test-model", [{"role": "user", "content": "x"}])
        with self.assertRaises(RateLimited) as cm:
            self.client.chat("test-model", [{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.retry_after, 2.5)
        with self.assertRaises(PaidModel):
            self.client.chat("test-model", [{"role": "user", "content": "x"}])


class ProtocolConfigTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name
        self._old = {k: os.environ.get(k) for k in
                     ("OPENROUTER_API_KEY", "OPENAI_API_KEY")}

    def tearDown(self):
        for key, val in self._old.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home
        self.home.cleanup()

    def _cfg(self, *extra):
        return C.load_config(overrides=[
            "api.key_env=", "api.key_file=/nonexistent/hydra-key",
        ] + list(extra))

    def test_protocol_factory_keeps_ollama_default(self):
        self.assertIsInstance(make_client(self._cfg()), OllamaCloud)
        client = make_client(self._cfg("api.protocol=openai",
                                       "api.host=http://127.0.0.1:1/v1"))
        self.assertIsInstance(client, OpenAiClient)
        self.assertEqual(client.protocol, "openai")
        client.close()

    def test_openai_key_env_fallback(self):
        os.environ.pop("OPENROUTER_API_KEY", None)
        os.environ["OPENROUTER_API_KEY"] = "test-openrouter-key"
        cfg = self._cfg("api.protocol=openai")
        self.assertEqual(C.load_key(cfg), "test-openrouter-key")

    def test_probe_all_uses_configured_models_when_tags_404(self):
        from hydra import models as M
        from hydra.gate import LLMGate

        srv = FakeOpenAI(tags=None).start()
        try:
            srv.add("fake-model", {"content": "OK"},
                    {"tool_calls": [{"name": "add",
                                     "arguments": {"a": 2, "b": 3}}]},
                    {"content": '{"ok": true}'},
                    {"content": "OK"})
            client = OpenAiClient(srv.url + "/v1", "test-key", 5, 5)
            cfg = self._cfg("api.protocol=openai",
                            f"api.host={srv.url}/v1",
                            'models.probe_candidates=["fake-model"]')
            reg = M.ModelRegistry(cfg)
            got = M.probe_all(cfg, client, LLMGate(1), reg)
            self.assertEqual(got["fake-model"]["status"], "ok")
            self.assertTrue(got["fake-model"]["tools"])
            client.close()
        finally:
            srv.stop()

    def test_models_404_keeps_vision_capability_fail_open(self):
        srv = FakeOpenAI(tags=None).start()
        try:
            client = OpenAiClient(srv.url + "/v1", "test-key", 5, 5)
            with self.assertRaises(ModelMissing):
                client.tags()
            from hydra import caps
            cfg = self._cfg("api.protocol=openai", f"api.host={srv.url}/v1")
            got = caps.detect(cfg, client=client, termux_fn=lambda: True)
            self.assertTrue(got["vision"])
            self.assertEqual(got["vision_model"],
                             C.get(cfg, "roles.vision.chain")[0])
            client.close()
        finally:
            srv.stop()


class OpenAiRuntimeTests(unittest.TestCase):
    def test_end_to_end_fake_run(self):
        from hydra.runtime import Runtime

        srv = FakeOpenAI(tags=["gpt-oss:120b"]).start()
        home = tempfile.TemporaryDirectory()
        cwd = tempfile.TemporaryDirectory()
        old_home = os.environ.get("HYDRA_HOME")
        old_key = os.environ.get("WO5_FAKE_KEY")
        os.environ["HYDRA_HOME"] = home.name
        os.environ["WO5_FAKE_KEY"] = "test-key"
        try:
            srv.add("gpt-oss:120b",
                    {"content": "writing",
                     "tool_calls": [{"name": "write_file",
                                     "arguments": {"path": "hello.py",
                                                   "content": "print(42)\n"}}]},
                    {"content": "running",
                     "tool_calls": [{"name": "bash",
                                     "arguments": {"cmd": "python hello.py"}}]},
                    {"content": "done",
                     "tool_calls": [{"name": "finish",
                                     "arguments": {"status": "done",
                                                   "summary": "hello.py created",
                                                   "files_changed": ["hello.py"]}}]})
            cfg = C.load_config(overrides=[
                "api.protocol=openai",
                f"api.host={srv.url}/v1",
                "api.key_env=WO5_FAKE_KEY",
                "api.retries=1",
                "loop.verify=never",
                "termux.wake_lock=false",
                "termux.notify_on_finish=false",
                "termux.notify_on_park=false",
            ], project_dir=cwd.name)
            rt = Runtime(cfg, cwd=cwd.name, quiet=True)
            try:
                code, report = rt.run("create hello.py and run it")
            finally:
                rt.close()
            self.assertEqual(code, 0, report)
            self.assertEqual(report["status"], "done")
            self.assertTrue((Path(cwd.name) / "hello.py").is_file())
            reqs = srv.chat_requests()
            self.assertEqual(len(reqs), 3)
            for req in reqs:
                self.assertEqual(req["path"], "/v1/chat/completions")
                self.assertTrue(req["body"].get("tools"))
                self.assertEqual(req["authorization"], "Bearer test-key")
            self.assertTrue(any(m.get("tool_call_id")
                                for m in reqs[-1]["body"]["messages"]))
        finally:
            srv.stop()
            home.cleanup()
            cwd.cleanup()
            if old_home is None:
                os.environ.pop("HYDRA_HOME", None)
            else:
                os.environ["HYDRA_HOME"] = old_home
            if old_key is None:
                os.environ.pop("WO5_FAKE_KEY", None)
            else:
                os.environ["WO5_FAKE_KEY"] = old_key


if __name__ == "__main__":
    unittest.main()
