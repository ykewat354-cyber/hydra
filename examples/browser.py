"""Reserved browser plugin slot (§19.12) — copy to ``~/.hydra/tools/browser.py``.

HYDRA ships **no core browser**. The contract (``browser_open``,
``browser_act``, ``browser_read``) is reserved for a plugin, and this file is a
working reference implementation on top of stdlib ``urllib``:

    cp examples/browser.py ~/.hydra/tools/browser.py

Multi-tool plugin contract
--------------------------
A plugin module normally defines ``SPEC`` + ``run(args, ctx)`` (one tool). To
expose several tools from one file it may instead define ``TOOLS = [spec, ...]``
and ``run(name, args, ctx)``. Names starting with ``browser_`` are wrapped as
``<untrusted>`` by the harness automatically (web pages are data, never
instructions).

No DOM engine is bundled. ``browser_act`` supports ``action="goto"`` only;
click/type need a real browser plugin installed over this file.
"""
from __future__ import annotations

import html
import re
import urllib.request

MAX_BYTES = 2_000_000
MAX_CHARS = 8000

_PAGES: dict = {}


def _fetch(url: str, timeout: float = 20) -> str:
    req = urllib.request.Request(url, headers={
        "User-Agent": "hydra/1.0 (Termux; +https://github.com/)",
        "Accept": "text/html,application/xhtml+xml,text/plain;q=0.9,*/*;q=0.5",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read(MAX_BYTES)
        charset = resp.headers.get_content_charset() or "utf-8"
        try:
            return raw.decode(charset, "replace")
        except LookupError:
            return raw.decode("utf-8", "replace")


def _to_text(page: str) -> str:
    page = re.sub(r"(?is)<(script|style|noscript|template).*?</\1>", " ", page)
    page = re.sub(r"(?i)<br\s*/?>", "\n", page)
    page = re.sub(r"(?i)</(p|div|li|tr|h[1-6])>", "\n", page)
    page = re.sub(r"(?s)<[^>]+>", " ", page)
    page = html.unescape(page)
    lines = [" ".join(line.split()) for line in page.splitlines()]
    return "\n".join(line for line in lines if line)


def _open(url: str) -> str:
    if not url.startswith(("http://", "https://")):
        return "ERROR: browser_open needs an http(s) URL"
    try:
        _PAGES["current"] = {"url": url, "html": _fetch(url)}
    except Exception as exc:
        return f"ERROR: browser_open failed: {type(exc).__name__}: {exc}"
    return f"opened {url} ({len(_PAGES['current']['html'])} bytes)"


def run(name: str, args: dict, ctx) -> str:
    args = args or {}
    if name == "browser_open":
        return _open(str(args.get("url") or "").strip())
    if name == "browser_read":
        page = _PAGES.get("current")
        if not page:
            return "ERROR: nothing open; call browser_open first"
        text = _to_text(page["html"])
        return f"{page['url']}\n{text[:MAX_CHARS]}"
    if name == "browser_act":
        action = str(args.get("action") or "").strip().lower()
        if action in ("goto", "open", "navigate") and args.get("url"):
            return _open(str(args["url"]).strip())
        return ("ERROR: this reference plugin supports only action=\"goto\" with a "
                "URL; click/type need a real browser plugin")
    return f"ERROR: unknown browser tool {name!r}"


TOOLS = [
    {
        "name": "browser_open",
        "description": "Fetch an http(s) URL and keep it as the current page.",
        "parameters": {"type": "object",
                       "properties": {"url": {"type": "string"}},
                       "required": ["url"]},
    },
    {
        "name": "browser_read",
        "description": "Read the current page as plain text (capped).",
        "parameters": {"type": "object",
                       "properties": {"max_chars": {"type": "integer"}}},
    },
    {
        "name": "browser_act",
        "description": "Act on the current page. Supported: action=\"goto\" + url.",
        "parameters": {"type": "object",
                       "properties": {"action": {"type": "string"},
                                      "url": {"type": "string"}}},
    },
]
