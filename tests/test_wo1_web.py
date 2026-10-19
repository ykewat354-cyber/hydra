"""T09 regressions: web tool URL validation, untrusted wrapping, spill cap."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from hydra.tools import Executor
from hydra.tools import web as webtool


class _FakeClient:
    def web_search(self, query, n):
        return {"results": [{"title": "t", "url": "https://x/y",
                             "content": "snippet"}]}

    def web_fetch(self, url):
        return {"title": "t", "content": "hello"}


class WebToolTests(unittest.TestCase):
    def test_invalid_url_is_not_called_page_not_found(self):
        out = webtool._tool_web_fetch({"url": "this is not a url"}, None)
        self.assertTrue(out.startswith("ERROR: invalid URL"), out)
        self.assertNotIn("404", out)

    def test_search_results_are_wrapped_untrusted(self):
        ctx = SimpleNamespace(client=_FakeClient(), cfg={},
                              cwd=Path("."))
        out = webtool._tool_web_search({"query": "x", "max_results": 3}, ctx)
        self.assertTrue(out.startswith('<untrusted source="web_search">'), out)
        self.assertTrue(out.rstrip().endswith("</untrusted>"))

    def test_large_tool_output_spills_to_readable_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt = SimpleNamespace(cfg={},
                                 journal=SimpleNamespace(dir=Path(tmp),
                                                         seq=7))
            ex = Executor.__new__(Executor)
            ex.rt = rt
            agent = SimpleNamespace(id="a0")
            out = ex._cap("x" * 7000, agent, "web_fetch")
            self.assertIn("full output:", out)
            spill = Path(tmp) / "spill" / "a0-000007-web_fetch.txt"
            self.assertTrue(spill.is_file())
            self.assertEqual(len(spill.read_text(encoding="utf-8")), 7000)
            self.assertIn("chars omitted", out)


if __name__ == "__main__":
    unittest.main()
