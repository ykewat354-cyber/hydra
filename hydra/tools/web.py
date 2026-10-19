"""Web tools: web_search / web_fetch (Ollama hosted endpoints) + raw http.

Results are wrapped as <untrusted> so hostile pages are never treated as
instructions (zero friction: nothing is blocked, only tagged).

``web_fetch`` prefers Ollama's hosted fetcher, but that endpoint 404s on some
URLs the raw web serves fine (and reports all failures as ModelMissing). A
stdlib urllib fallback keeps research working; HTTP 404 is reported as a real
page-not-found instead of a model/credential error.
"""
from __future__ import annotations

import html as html_lib
import json
import re
import ssl
import urllib.error
import urllib.request

from . import Tool
from .. import hygiene

UA = ("Mozilla/5.0 (Android 16; Termux aarch64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Mobile Safari/537.36 HYDRA/1.0")


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


def _html_to_text(raw: str) -> str:
    """Small, safe HTML reader: title kept separately, scripts/styles removed."""
    s = re.sub(r"(?is)<(script|style|noscript|svg|canvas)[^>]*>.*?</\1>", " ", raw)
    s = re.sub(r"(?is)<!--.*?-->", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "\n- ", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr|section|article|header|footer|blockquote)>",
               "\n", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = html_lib.unescape(s).replace("\xa0", " ")
    lines, blank = [], 0
    for line in s.splitlines():
        line = " ".join(line.split())
        if line:
            lines.append(line)
            blank = 0
        elif blank == 0:
            lines.append("")
            blank = 1
    return "\n".join(lines).strip()


def _fetch_raw(url: str, timeout: int = 30) -> tuple:
    """Return (title, text) for a URL via urllib. Raises on failure."""
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.5",
        "Accept-Language": "en-US,en;q=0.8",
    })
    contexts = [None]
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=contexts[0]) as resp:
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(250_000)
            charset = resp.headers.get_content_charset() or "utf-8"
    except ssl.SSLCertVerificationError:
        # Some Termux installs have no CA store at all. The page is untrusted
        # content either way; retry unverified but say so in the marker.
        ctx = ssl._create_unverified_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            ctype = str(resp.headers.get("Content-Type") or "").lower()
            raw = resp.read(250_000)
            charset = resp.headers.get_content_charset() or "utf-8"
        ctype += "; tls-unverified"
    text = raw.decode(charset, "replace")
    title = ""
    if "html" in ctype or text.lstrip().lower().startswith(("<!doctype html", "<html")):
        m = re.search(r"(?is)<title[^>]*>(.*?)</title>", text)
        if m:
            title = html_lib.unescape(re.sub(r"\s+", " ", m.group(1))).strip()
        text = _html_to_text(text)
    else:
        # JSON is much easier to read pretty-printed, but keep it as text.
        if "json" in ctype:
            try:
                text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
            except Exception:
                pass
    if "tls-unverified" in ctype:
        title = (title + " [TLS unverified]").strip()
    return title, text


def _http_404(exc) -> bool:
    s = f"{type(exc).__name__}: {exc}"
    return "404" in s or "not found" in s.lower()


def _tool_web_fetch(args, ctx) -> str:
    url = str(args.get("url") or "").strip()
    if not url:
        return "ERROR: url is required"
    hosted = ""
    try:
        data = ctx.client.web_fetch(url)
        title = data.get("title") or ""
        content = str(data.get("content") or data.get("text") or "")
    except Exception as exc:
        hosted = f"{type(exc).__name__}: {exc}"
        if _http_404(exc):
            # The hosted endpoint also wraps 404s in ModelMissing; a missing
            # page is not a model problem. Try raw once, then report clearly.
            try:
                title, content = _fetch_raw(url)
                content = f"(hosted endpoint said 404; raw fetch succeeded)\n{content}"
            except Exception as raw_exc:
                return (f"ERROR: page not found (HTTP 404): {url} "
                        f"[raw: {type(raw_exc).__name__}: {raw_exc}]")
        else:
            try:
                title, content = _fetch_raw(url)
                content = f"(hosted endpoint failed: {hosted[:200]}; raw fetch succeeded)\n{content}"
            except Exception as raw_exc:
                return (f"ERROR: web_fetch failed: {hosted} "
                        f"[raw fallback: {type(raw_exc).__name__}: {raw_exc}]")
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
        "web_fetch", "Fetch a URL's text content. Uses the Ollama hosted endpoint "
                     "with a raw-HTTP fallback; 404 means the page does not exist.",
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
