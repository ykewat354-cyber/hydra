"""Phone-friendly stream rendering: one line per event, narrow screens handled."""
from __future__ import annotations

import json
import os
import shutil


def _one_line(obj, cap: int = 90) -> str:
    if isinstance(obj, str):
        s = obj
    else:
        try:
            s = json.dumps(obj, ensure_ascii=False)
        except Exception:
            s = str(obj)
    s = " ".join(s.split())
    return s if len(s) <= cap else s[: cap - 1] + "…"


class Tui:
    def __init__(self, quiet=False, json_out=False):
        self.quiet = bool(quiet) or os.environ.get("HYDRA_QUIET") == "1"
        self.json_out = bool(json_out)
        try:
            self.cols = shutil.get_terminal_size((80, 20)).columns
        except Exception:
            self.cols = 80
        self.ascii = os.environ.get("HYDRA_ASCII") == "1"

    # -------------------------------------------------- primitives
    def _out(self, text: str) -> None:
        print(text, flush=True)

    def info(self, text: str) -> None:
        if not self.quiet and not self.json_out:
            self._out(text)

    def _agent_tag(self, ev) -> str:
        a = str(ev.get("agent") or "run")
        return f"[{a:<11}]"

    def event(self, ev: dict) -> None:
        if self.json_out:
            self._out(json.dumps(ev, ensure_ascii=False))
            return
        if self.quiet:
            return
        t = ev.get("type")
        tag = self._agent_tag(ev)
        if t == "run_start":
            self._out(f">> {ev.get('run')}  task={_one_line(ev.get('task'), 70)}")
        elif t == "run_resume":
            self._out(f">> resume {ev.get('run')}")
        elif t == "tool_call":
            self._out(f"{tag} tool {ev.get('tool')} {_one_line(ev.get('args'), 80)}")
        elif t == "tool_result":
            prev = _one_line(ev.get("preview"), 70)
            mark = "!" if prev.startswith("ERROR") else "ok"
            self._out(f"{tag}   {mark} {ev.get('tool')}: {prev}")
        elif t == "model_response":
            calls = ev.get("tool_calls") or []
            extra = f" calls={','.join(str(c) for c in calls)}" if calls else ""
            self._out(f"{tag} model {ev.get('model')} "
                      f"in={ev.get('in_tokens')} out={ev.get('out_tokens')}{extra}")
        elif t == "report":
            rep = ev.get("report") or {}
            self._out(f"{tag} report {rep.get('status')}: {_one_line(rep.get('summary'), 70)}")
        elif t == "park":
            self._out(f"{tag} parked ({ev.get('reason')})")
        elif t == "stall":
            self._out(f"{tag} stall detected (x{ev.get('repeated')})")
        elif t == "error":
            self._out(f"{tag} ERROR {_one_line(ev.get('error'), 80)}")
