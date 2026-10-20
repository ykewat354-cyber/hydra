"""Termux:API wrappers — thin, non-fatal. Everything is also reachable via bash."""
from __future__ import annotations

import shutil
import subprocess

from . import Tool


def _run(cmd: list, timeout: float = 10) -> str:
    binary = cmd[0]
    if not shutil.which(binary):
        return f"ERROR: {binary} not found (pkg install termux-api + Termux:API app)"
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0:
            return f"ERROR: {binary} exit {proc.returncode}: {(err or out)[:300]}"
        return out or "(ok)"
    except Exception as exc:
        return f"ERROR: {type(exc).__name__}: {exc}"


def _tool_notify(args, ctx):
    cmd = ["termux-notification"]
    if args.get("title"):
        cmd += ["-t", str(args["title"])]
    if args.get("content"):
        cmd += ["-c", str(args["content"])]
    return _run(cmd)


def _tool_toast(args, ctx):
    return _run(["termux-toast", str(args.get("text") or "")])


def _tool_clipboard_get(args, ctx):
    return _run(["termux-clipboard-get"])


def _tool_clipboard_set(args, ctx):
    return _run(["termux-clipboard-set", str(args.get("text") or "")])


def _tool_battery(args, ctx):
    return _run(["termux-battery-status"])


def _tool_location(args, ctx):
    provider = str(args.get("provider") or "network")
    return _run(["termux-location", "-p", provider], timeout=20)


def _tool_tts(args, ctx):
    return _run(["termux-tts-speak", str(args.get("text") or "")], timeout=30)


def _tool_vibrate(args, ctx):
    return _run(["termux-vibrate", "-d", str(int(args.get("ms") or 500))])


def _tool_open_url(args, ctx):
    return _run(["termux-open-url", str(args.get("url") or "")])


def _tool_share(args, ctx):
    path = str(args.get("path") or "")
    return _run(["termux-share", "-a", "send", path])


def _tool_sms(args, ctx):
    number = str(args.get("number") or "")
    text = str(args.get("text") or "")
    return _run(["termux-sms-send", "-n", number, text], timeout=20)


def _tool_call(args, ctx):
    return _run(["termux-telephony-call", str(args.get("number") or "")])


def _tool_camera(args, ctx):
    import os
    import time
    path = str(args.get("path") or f"{os.environ.get('TMPDIR', '/tmp')}/hydra_photo_{int(time.time())}.jpg")
    out = _run(["termux-camera-photo", "-c", "0", path], timeout=30)
    return out if out.startswith("ERROR") else f"photo saved: {path}"


def _tool_wifi(args, ctx):
    return _run(["termux-wifi-connectioninfo"])


_ACTIONS = {
    "notify": _tool_notify, "toast": _tool_toast,
    "clipboard_get": _tool_clipboard_get, "clipboard_set": _tool_clipboard_set,
    "battery": _tool_battery, "location": _tool_location, "tts": _tool_tts,
    "vibrate": _tool_vibrate, "open_url": _tool_open_url, "share": _tool_share,
    "sms_send": _tool_sms, "call": _tool_call, "camera_photo": _tool_camera,
    "wifi": _tool_wifi,
}


def _tool_termux(args, ctx):
    """One schema for every Android action (B2 prompt footprint)."""
    action = str((args or {}).get("action") or "").strip().lower()
    fn = _ACTIONS.get(action)
    if fn is None:
        return ("ERROR: unknown termux action; use one of: "
                + ", ".join(sorted(_ACTIONS)))
    return fn(args, ctx)


def register(reg) -> None:
    reg.register(Tool(
        "termux",
        "Android actions: notify, toast, clipboard, battery, location, tts, "
        "vibrate, open_url, share, sms_send, call, camera_photo, wifi.",
        {"type": "object",
         "properties": {
             "action": {"type": "string", "enum": sorted(_ACTIONS)},
             "title": {"type": "string"},
             "content": {"type": "string"},
             "text": {"type": "string"},
             "provider": {"type": "string"},
             "ms": {"type": "integer"},
             "url": {"type": "string"},
             "path": {"type": "string"},
             "number": {"type": "string"},
         },
         "required": ["action"]},
        _tool_termux))
    # Keep the old names dispatchable, but out of the model's prompt.
    reg.register(Tool("termux_notify", "Android notification.",
                      {"type": "object", "properties": {"title": {"type": "string"},
                                                        "content": {"type": "string"}}},
                      _tool_notify, hide_schema=True))
    reg.register(Tool("termux_toast", "Android toast message.",
                      {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"]}, _tool_toast))
    reg.register(Tool("termux_clipboard_get", "Read the Android clipboard.",
                      {"type": "object", "properties": {}}, _tool_clipboard_get))
    reg.register(Tool("termux_clipboard_set", "Write the Android clipboard.",
                      {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"]}, _tool_clipboard_set))
    reg.register(Tool("termux_battery", "Battery status as JSON.",
                      {"type": "object", "properties": {}}, _tool_battery))
    reg.register(Tool("termux_location", "Device location as JSON.",
                      {"type": "object", "properties": {"provider": {"type": "string"}}},
                      _tool_location, hide_schema=True))
    reg.register(Tool("termux_tts", "Speak text out loud.",
                      {"type": "object", "properties": {"text": {"type": "string"}},
                       "required": ["text"]}, _tool_tts))
    reg.register(Tool("termux_vibrate", "Vibrate the phone.",
                      {"type": "object", "properties": {"ms": {"type": "integer"}}},
                      _tool_vibrate, hide_schema=True))
    reg.register(Tool("termux_open_url", "Open a URL in the default app/browser.",
                      {"type": "object", "properties": {"url": {"type": "string"}},
                       "required": ["url"]}, _tool_open_url))
    reg.register(Tool("termux_share", "Share a file via the Android share sheet.",
                      {"type": "object", "properties": {"path": {"type": "string"}},
                       "required": ["path"]}, _tool_share))
    reg.register(Tool("termux_sms_send", "Send an SMS (needs the permission).",
                      {"type": "object", "properties": {"number": {"type": "string"},
                                                        "text": {"type": "string"}},
                       "required": ["number", "text"]}, _tool_sms))
    reg.register(Tool("termux_call", "Place a phone call.",
                      {"type": "object", "properties": {"number": {"type": "string"}},
                       "required": ["number"]}, _tool_call))
    reg.register(Tool("termux_camera_photo", "Take a photo with the rear camera.",
                      {"type": "object", "properties": {"path": {"type": "string"}}},
                      _tool_camera, hide_schema=True))
    reg.register(Tool("termux_wifi", "Wi-Fi connection info as JSON.",
                      {"type": "object", "properties": {}}, _tool_wifi))
    for t in reg.tools.values():
        if t.name.startswith("termux_") and t.name != "termux":
            t.hide_schema = True
