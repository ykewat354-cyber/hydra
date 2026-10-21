"""WO6 W6-1/W6-2 — named providers, failover and OpenAI message normalization."""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from tests.fake_ollama import FakeOllama          # noqa: E402
from tests.fake_openai import FakeOpenAI          # noqa: E402
from hydra import config as C                     # noqa: E402
from hydra import journal as J                    # noqa: E402
from hydra.client import make_client              # noqa: E402
from hydra.router import Router, parse_model_spec  # noqa: E402


class ModelSpecTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("HYDRA_HOME")
        os.environ["HYDRA_HOME"] = self.home.name
        self.cfg = C.load_config(project_dir=self.home.name)

    def tearDown(self):
        self.home.cleanup()
        if self._old_home is None:
            os.environ.pop("HYDRA_HOME", None)
        else:
            os.environ["HYDRA_HOME"] = self._old_home

    def test_ollama_tags_are_not_mistaken_for_providers(self):
        self.assertEqual(parse_model_spec("gemma4:31b", self.cfg),
                         ("default", "gemma4:31b"))
        self.assertEqual(parse_model_spec("gpt-oss:120b", self.cfg),
                         ("default", "gpt-oss:120b"))
        self.assertEqual(parse_model_spec("deepseek-chat:free", self.cfg),
                         ("default", "deepseek-chat:free"))

    def test_known_prefixes_split_provider_and_model(self):
        self.assertEqual(parse_model_spec("openrouter:deepseek/deepseek-chat", self.cfg),
                         ("openrouter", "deepseek/deepseek-chat"))
        self.assertEqual(parse_model_spec("groq:llama-3.3-70b:free", self.cfg),
                         ("groq", "llama-3.3-70b:free"))
        self.assertEqual(parse_model_spec("local:qwen2.5-coder:7b", self.cfg),
                         ("local", "qwen2.5-coder:7b"))
        self.assertEqual(parse_model_spec("ollama:gemma4:31b", self.cfg),
                         ("ollama", "gemma4:31b"))

    def test_custom_provider_is_recognized(self):
        self.cfg.setdefault("providers", {})["mybox"] = {
            "protocol": "openai", "host": "http://127.0.0.1:9999/v1"}
        self.assertEqual(parse_model_spec("mybox:model:tag", self.cfg),
                         ("mybox", "model:tag"))

    def test_model_labels_show_the_resolved_provider(self):
        self.assertEqual(C.model_label("gpt-oss:120b", self.cfg),
                         "gpt-oss:120b (ollama)")
        self.assertEqual(C.model_label("openrouter:deepseek/deepseek-chat",
                                        self.cfg),
                         "openrouter:deepseek/deepseek-chat")
        self.cfg["api"]["protocol"] = "openai"
        self.assertEqual(C.model_label("gpt-oss:120b", self.cfg),
                         "gpt-oss:120b (openai)")

    def test_provider_client_uses_its_own_host_and_key(self):
        srv = FakeOpenAI(tags=["m"], require_auth="Bearer or-key").start()
        old = os.environ.get("WO6_OR_KEY")
        os.environ["WO6_OR_KEY"] = "or-key"
        try:
            self.cfg.setdefault("providers", {})["openrouter"] = {
                "protocol": "openai", "host": srv.url + "/v1",
                "key_env": "WO6_OR_KEY", "key_file": ""}
            client = make_client(self.cfg, "openrouter")
            try:
                self.assertEqual(client.protocol, "openai")
                self.assertEqual(client.host, srv.url + "/v1")
                self.assertEqual(client.key, "or-key")
            finally:
                client.close()
        finally:
            srv.stop()
            if old is None:
                os.environ.pop("WO6_OR_KEY", None)
            else:
                os.environ["WO6_OR_KEY"] = old


class CrossProviderFailoverTests(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.TemporaryDirectory()
        self.cwd = tempfile.TemporaryDirectory()
        self._old = {k: os.environ.get(k) for k in
                     ("HYDRA_HOME", "WO6_OLLAMA_KEY", "WO6_OR_KEY")}
        os.environ["HYDRA_HOME"] = self.home.name
        os.environ["WO6_OLLAMA_KEY"] = "ollama-key"
        os.environ["WO6_OR_KEY"] = "or-key"
        self.ollama = FakeOllama(tags=["paid-ollama:1b"],
                                 require_auth="Bearer ollama-key").start()
        self.openai = FakeOpenAI(tags=["deepseek-chat"],
                                 require_auth="Bearer or-key").start()

    def tearDown(self):
        self.ollama.stop()
        self.openai.stop()
        self.home.cleanup()
        self.cwd.cleanup()
        for key, val in self._old.items():
            if val is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = val

    def _cfg(self):
        cfg = C.load_config(project_dir=self.cwd.name)
        cfg["api"].update({"protocol": "ollama", "host": self.ollama.url,
                           "key_env": "WO6_OLLAMA_KEY", "key_file": "",
                           "retries": 1})
        cfg.setdefault("providers", {})["openrouter"] = {
            "protocol": "openai", "host": self.openai.url + "/v1",
            "key_env": "WO6_OR_KEY", "key_file": ""}
        cfg["roles"]["lead"]["chain"] = ["paid-ollama:1b",
                                          "openrouter:deepseek-chat"]
        cfg["loop"].update({"verify": "never", "max_turns": 5})
        cfg["termux"].update({"wake_lock": False, "notify_on_finish": False,
                              "notify_on_park": False})
        return cfg

    def test_turn_one_tool_history_survives_failover_to_strict_openai(self):
        self.ollama.add(
            "paid-ollama:1b",
            {"content": "writing",
             "tool_calls": [{"function": {
                 "name": "write_file",
                 "arguments": {"path": "x.txt", "content": "hello\n"}}}]},
            {"status": 402, "body": {"error": "credits exhausted"}})

        seen = {}

        def strict(body):
            messages = body.get("messages") or []
            seen["messages"] = messages
            assistant_at = None
            for i, msg in enumerate(messages):
                if msg.get("role") == "assistant" and msg.get("tool_calls"):
                    assistant_at = i
                    break
            if assistant_at is None:
                return {"status": 400, "body": {"error": "missing tool history"}}
            calls = messages[assistant_at]["tool_calls"]
            for call in calls:
                if not isinstance(call.get("function", {}).get("arguments"), str):
                    return {"status": 400, "body": {"error": "arguments not string"}}
                if not call.get("id"):
                    return {"status": 400, "body": {"error": "missing call id"}}
            result = messages[assistant_at + 1] if assistant_at + 1 < len(messages) else {}
            if result.get("role") != "tool" or \
                    result.get("tool_call_id") != calls[0]["id"]:
                return {"status": 400, "body": {"error": "tool id mismatch"}}
            return {"content": "done",
                    "tool_calls": [{"function": {
                        "name": "finish",
                        "arguments": {"status": "done", "summary": "x.txt written",
                                      "files_changed": ["x.txt"]}}}]}

        self.openai.add("deepseek-chat", strict)
        from hydra.runtime import Runtime
        rt = Runtime(self._cfg(), cwd=self.cwd.name, quiet=True)
        try:
            code, report = rt.run("write x.txt")
            self.assertEqual(code, 0, report)
            run_id = rt.run_id
        finally:
            rt.close()
        self.assertTrue((Path(self.cwd.name) / "x.txt").is_file())
        self.assertIn("deepseek-chat", self.openai.seen_models)
        self.assertTrue(seen.get("messages"))
        events = J.read_events(run_id)
        failovers = [e for e in events if e.get("type") == "failover"]
        self.assertTrue(failovers, events[-10:])
        self.assertEqual(failovers[0].get("from_model"), "paid-ollama:1b")
        self.assertEqual(failovers[0].get("to_model"), "openrouter:deepseek-chat")
        self.assertEqual(failovers[0].get("reason"), "402 paid/credits")


if __name__ == "__main__":
    unittest.main()
