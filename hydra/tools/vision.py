"""view_image (§19.7): see an image by delegating to the `vision` role.

Reads png/jpg/webp (≤ 5 MB; larger files are downscaled with ffmpeg when
installed), base64-encodes, and makes a one-shot call to the vision role with
``images: [<b64>]`` in the user message. Returns **text**, so non-vision agents
can "see" by delegation.
"""
from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

from . import Tool

MAX_BYTES = 5 * 1024 * 1024
OK_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".heic"}


def _resolve(ctx, raw):
    if not raw:
        return None
    p = Path(str(raw)).expanduser()
    if not p.is_absolute():
        p = ctx.cwd / p
    return p


def _downscale(path: Path):
    """Return a path ≤ MAX_BYTES, using ffmpeg; None when not possible."""
    if not shutil.which("ffmpeg"):
        return None
    out = Path(tempfile.gettempdir()) / f"hydra_img_{os.getpid()}_{int(time.time() * 1000)}.jpg"
    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(path),
           "-vf", "scale='min(1600,iw)':-1", "-q:v", "5", str(out)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        if proc.returncode == 0 and out.is_file() and out.stat().st_size <= MAX_BYTES:
            return out
    except Exception:
        pass
    try:
        out.unlink()
    except Exception:
        pass
    return None


def _tool_view_image(args, ctx) -> str:
    path = _resolve(ctx, args.get("path"))
    if path is None or not str(path):
        return "ERROR: path is required"
    if not path.is_file():
        return f"ERROR: not a file: {path}"
    if path.suffix.lower() not in OK_EXT:
        return f"ERROR: unsupported image type {path.suffix!r} (png/jpg/webp)"
    tmp = None
    try:
        if path.stat().st_size > MAX_BYTES:
            tmp = _downscale(path)
            if tmp is None:
                return (f"ERROR: {path} is larger than 5 MB and ffmpeg is not "
                        f"available to downscale it (install: pkg install ffmpeg)")
            path = tmp
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    except Exception as exc:
        return f"ERROR: cannot read image: {type(exc).__name__}: {exc}"
    question = str(args.get("question") or
                   "Describe this image precisely: what it shows, any visible "
                   "text (verbatim), UI elements, errors, and anything notable.")
    msg = {"role": "user", "content": question, "images": [b64]}
    try:
        reply = ctx.rt.router.chat([msg], role="vision", tools=None)
    except Exception as exc:
        return f"ERROR: vision call failed: {type(exc).__name__}: {exc}"
    finally:
        if tmp is not None:
            try:
                tmp.unlink()
            except Exception:
                pass
    text = (reply.content or "").strip()
    return text or "(the vision model returned nothing)"


def register(reg) -> None:
    reg.register(Tool(
        "view_image",
        "Look at an image (png/jpg/webp, ≤ 5 MB; bigger is downscaled when ffmpeg "
        "is installed). Delegates to the vision role and returns its text answer. "
        "Couples with termux_camera_photo / screenshots.",
        {"type": "object", "properties": {
            "path": {"type": "string"},
            "question": {"type": "string",
                         "description": "what to look for (default: describe)"}},
         "required": ["path"]},
        _tool_view_image))
