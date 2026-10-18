"""Untrusted-content tagging + secret redaction. Config toggles, never gates."""
from __future__ import annotations

import fnmatch
import os
from pathlib import Path

from . import config as C


def make_redactor(cfg: dict):
    """Return fn(str)->str that masks the API key and configured env values."""
    if not C.get(cfg, "hygiene.redact", True):
        return None
    secrets = []
    for name in (C.get(cfg, "hygiene.redact_env", []) or []):
        val = os.environ.get(name)
        if val and len(val) >= 8:
            secrets.append(val)
    key = C.load_key(cfg)
    if key and len(key) >= 8 and key not in secrets:
        secrets.append(key)
    if not secrets:
        return None

    def redact(text):
        if not isinstance(text, str):
            return text
        for s in secrets:
            text = text.replace(s, "***")
        return text

    return redact


def is_untrusted(cfg: dict, path) -> bool:
    if not C.get(cfg, "hygiene.untrusted_tagging", True):
        return False
    try:
        p = str(Path(path).expanduser())
    except Exception:
        return False
    for pat in (C.get(cfg, "hygiene.untrusted_paths", []) or []):
        try:
            if fnmatch.fnmatch(p, str(C.expand(pat))):
                return True
        except Exception:
            continue
    return False


def wrap_untrusted(cfg: dict, text: str, source: str) -> str:
    if not C.get(cfg, "hygiene.untrusted_tagging", True):
        return text
    return f'<untrusted source="{source}">\n{text}\n</untrusted>'
