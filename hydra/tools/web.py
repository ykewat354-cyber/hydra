"""Web tools: web_search / web_fetch (Ollama hosted endpoints) + raw http.

Results are wrapped as <untrusted> so hostile pages are never treated as
instructions (zero friction: nothing is blocked, only tagged).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request

from . import Tool
from .. import hygiene


def _tool_web_search(args, ctx) -> str:
    query = str(args.get("query") or "").strip()
    if not query:
        return "ERROR: query is required"
    n = int(args.get("max_results") or 5)
    try:
        data = ctx.client.web_search(query, n)
    except Exception as exc:
        return f"ERROR: web_search failed: {type(exc).__name__}: {exc}"
    results = data.get("results") or data.get("items") or []
    if not results:
        return "(no results)"
    lines = []
    for r in results[:n]:
        if not isinstance(r, dict):
            lines.append(str(r))
            continue
        title = r.get("title") or ""
        url = r.get("url") or r.get("link") or ""
        content = str(r.get("content") or r.get("snippet") or r.get("text") or "")
        lines.append(f"### {title}\n{url}\n{content[:600]}")
    text = "\n\n".join(lines)
    return hygiene.wrap_untrusted(ctx.cfg, text, "web_search")


def _tool_web_fetch(args, ctx) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "ERROR: url is required"
    try:
        data = ctx.client.web_fetch(url)
    except Exception as exc:
        return f"ERROR: web_fetch failed: {type(exc).__name__}: {exc}"
    title = data.get("title") or ""
    content = str(data.get("content") or data.get("text") or "")
    trimmed = content[:8000]
    if len(content) > 8000:
        trimmed += f"\n[... {len(content) - 8000} chars trimmed]"
    text = (f"# {title}\n{url}\n\n{trimmed}").strip()
    return hygiene.wrap_untrusted(ctx.cfg, text, url)


def _tool_http(args, ctx) -> str:
    method = str(args.get("method") or "GET").upper()
    url = str(args.get("url") or "")
    if not url:
        return "ERROR: url is required"
    headers = args.get("headers") or {}
    body = args.get("body")
    data = None
    if body is not None:
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={str(k): str(v) for k, v in headers.items()})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read(65536).decode("utf-8", "replace")
            text = (f"HTTP {resp.status}\n"
                    + "\n".join(f"{k}: {v}" for k, v in resp.headers.items())
                    + f"\n\n{raw[:4000]}")
    except urllib.error.HTTPError as exc:
        raw = exc.read(8192).decode("utf-8", "replace")
        text = f"HTTP {exc.code}\n\n{raw[:4000]}"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"
    return hygiene.wrap_untrusted(ctx.cfg, text, url)


def register(reg) -> None:
    reg.register(Tool(
        "web_search", "Search the web via the Ollama hosted endpoint. Returns "
                      "titles, URLs and snippets.",
        {"type": "object", "properties": {"query": {"type": "string"},
                                          "max_results": {"type": "integer"}},
         "required": ["query"]},
        _tool_web_search))
    reg.register(Tool(
        "web_fetch", "Fetch a URL's text content via the Ollama hosted endpoint.",
        {"type": "object", "properties": {"url": {"type": "string"}},
         "required": ["url"]},
        _tool_web_fetch))
    reg.register(Tool(
        "http", "Raw HTTP request to any endpoint (urllib). Returns status, headers, "
                "body head.",
        {"type": "object", "properties": {
            "method": {"type": "string"}, "url": {"type": "string"},
            "headers": {"type": "object"}, "body": {"type": "string"}},
         "required": ["url"]},
        _tool_http))
