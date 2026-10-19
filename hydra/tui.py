"""Plain stream renderer used by ``hydra "task"`` and the non-TUI fallback.

The old renderer printed raw tool JSON and, worse, never printed the model's
assistant text at all — the actual answer was invisible. This one keeps the
same one-line-per-event feel but:

* renders assistant prose in a readable ``│`` block (markdown-lite),
* collapses tool calls/results to one line (``--verbose`` shows the details),
* prints the finish report (summary/findings/files) instead of only a JSON blob,
* never dumps raw argument dicts for known tools.
"""
from __future__ import annotations

import json
import os
import shutil
import sys

from . import ui_format as U


class Tui:
    def __init__(self, quiet=False, json_out=False, verbose=False):
        self.quiet = bool(quiet) or os.environ.get("HYDRA_QUIET") == "1"
        self.json_out = bool(json_out)
        self.verbose = bool(verbose) or os.environ.get("HYDRA_VERBOSE") == "1"
        try:
            self.cols = shutil.get_terminal_size((80, 20)).columns
        except Exception:
            self.cols = 80
        self.ascii = os.environ.get("HYDRA_ASCII") == "1"
        self.narrow = self.cols < 60
        self.color = (not self.json_out and sys.stdout.isatty()
                      and os.environ.get("NO_COLOR") is None
                      and os.environ.get("TERM", "") != "dumb")
        self.printed_report = False
        self.sym = ({"ok": "+", "bad": "x", "tool": "*", "bullet": "-",
                     "spawn": "+", "rule": "-", "dot": "*"}
                    if self.ascii else
                    {"ok": "✓", "bad": "✗", "tool": "●", "bullet": "•",
                     "spawn": "◆", "rule": "─", "dot": "●"})

    # -------------------------------------------------- primitives
    def _c(self, text: str, code: str = "") -> str:
        if not self.color or not code:
            return str(text)
        return f"\x1b[{code}m{text}\x1b[0m"

    def _out(self, text: str) -> None:
        print(text, flush=True)

    def _tag(self, ev) -> str:
        a = str(ev.get("agent") or "run")
        return f"[{a}]" if self.narrow else f"[{a:<11}]"

    def _wrap(self, text: str, width: int = None) -> list:
        return U.wrap_display(str(text or ""), width or self.cols, "", "")

    def info(self, text: str) -> None:
        if not self.quiet and not self.json_out:
            for row in self._wrap(str(text)):
                self._out(self._c(row, "2"))

    def delta(self, agent, kind, text):
        # Streaming is only worth it in the full-screen UI; the journal's
        # ``assistant`` event prints the final text once, without duplication.
        return

    # -------------------------------------------------- blocks
    def _assistant(self, ev, text: str):
        tag = self._tag(ev)
        width = max(20, self.cols - len(tag) - 4)
        rows = U.markdown_rows(text, width)
        if not rows:
            return
        for i, (row, style) in enumerate(rows):
            gutter = "│ " if i == 0 else "│ "
            code = {"head": "1;36", "code": "2", "border": "2"}.get(style, "")
            self._out(self._c(tag + " ", "2") + self._c(gutter, "2")
                      + self._c(row, code))

    def _tool_result_lines(self, ev, name: str, brief: str, ok: bool,
                           preview: str, width: int):
        mark = self.sym["ok"] if ok else self.sym["bad"]
        code = "32" if ok else "31"
        label = f"{mark} {name}"
        prefix = self._tag(ev) + "   " + label + " · "
        brief = U.clip_display(brief, max(4, self.cols - U.display_width(prefix)))
        self._out(self._c(prefix, code) + self._c(brief, code if not ok else ""))
        if self.verbose and preview:
            for row in U.wrap_display(preview, self.cols, "        ", "        ")[:12]:
                self._out(self._c(row, "2"))

    def _report(self, ev):
        rep = ev.get("report") if isinstance(ev.get("report"), dict) else {}
        aid = str(ev.get("agent") or "a0")
        if aid not in ("a0", "run", "None"):
            summary = U.short(rep.get("summary") or "", max(30, self.cols - 24))
            self._out(self._c(self._tag(ev) + " report " + str(rep.get("status")),
                              "32" if rep.get("status") == "done" else "33")
                      + " " + summary)
            return
        status = str(rep.get("status") or "done")
        code = {"done": "32", "partial": "33", "failed": "31",
                "killed": "31"}.get(status, "33")
        rule = self.sym["rule"] * min(self.cols, 64)
        self._out(self._c(rule, "2"))
        mark = self.sym["ok"] if status == "done" else self.sym["bad"]
        self._out(self._c(f"{mark} {status.upper()}", code)
                  + self._c(f" · {ev.get('run') or ''}", "2"))
        summary = str(rep.get("summary") or "").strip()
        if summary:
            self._out("")
            self._assistant({"agent": ev.get("agent") or "a0"}, summary)
        findings = rep.get("findings") or []
        if findings:
            self._out("")
            self._out(self._c("findings", "2"))
            for f in findings[:20]:
                for row in U.wrap_display(str(f), self.cols, "  • ", "    "):
                    self._out(row)
        files = rep.get("files_changed") or []
        if files:
            self._out("")
            self._out(self._c("files     " + ", ".join(str(f) for f in files[:20]), "2"))
        arts = rep.get("artifacts") or []
        if arts:
            self._out(self._c("artifacts " + ", ".join(str(a) for a in arts[:20]), "36"))
        issues = rep.get("open_issues") or []
        if issues:
            self._out("")
            self._out(self._c("open issues", "33"))
            for it in issues[:10]:
                for row in U.wrap_display(U.issue_text(it), self.cols,
                                          "  • ", "    "):
                    self._out(self._c(row, "33"))
        self._out(self._c(rule, "2"))
        self.printed_report = True

    # -------------------------------------------------- event sink
    def event(self, ev: dict) -> None:
        if self.json_out:
            self._out(json.dumps(ev, ensure_ascii=False))
            return
        if self.quiet:
            return
        t = ev.get("type")
        tag = self._tag(ev)

        if t == "run_start":
            self.printed_report = False
            task = U.short(ev.get("task") or "", max(30, self.cols - 16))
            self._out(self._c(f">> {ev.get('run')}  ", "2") + task)
        elif t == "run_resume":
            self._out(self._c(f">> resume {ev.get('run')}", "2"))

        elif t == "assistant":
            text = str(ev.get("text") or "").strip("\n")
            if text:
                self._out("")
                self._assistant(ev, text)
                self._out("")

        elif t == "tool_call":
            name = str(ev.get("tool") or "tool")
            summary = U.tool_summary(name, ev.get("args"))
            head = f"{tag} {self.sym['tool']} {name}"
            room = max(12, self.cols - U.display_width(head) - 2)
            self._out(self._c(head, "36") + "  " + U.clip_display(summary, room))

        elif t == "tool_result":
            name = str(ev.get("tool") or "tool")
            preview = str(ev.get("preview") or "")
            ok, brief = U.result_brief(
                name, preview, chars=ev.get("chars"), ms=ev.get("ms"),
                lines=ev.get("lines"), width=max(20, self.cols - 14))
            self._tool_result_lines(ev, name, brief, ok, preview, self.cols)

        elif t == "model_response":
            if self.verbose:
                calls = ev.get("tool_calls") or []
                extra = " · " + ",".join(str(c) for c in calls) if calls else ""
                self._out(self._c(
                    f"{tag} model {ev.get('model')} · "
                    f"{U.fmt_tokens(ev.get('in_tokens'))}→"
                    f"{U.fmt_tokens(ev.get('out_tokens'))} · "
                    f"{(int(ev.get('ms') or 0) / 1000):.1f}s{extra}", "2"))

        elif t == "agent_spawn":
            aid = str(ev.get("agent") or "?")
            role = ev.get("role") or "worker"
            task = U.short(ev.get("task") or "", max(20, self.cols - 20))
            self._out(self._c(f"{self.sym['spawn']} {aid} {role}", "35")
                      + self._c(f" · {task}", "2"))

        elif t == "agent_end":
            if self.verbose:
                self._out(self._c(f"{tag} end {ev.get('status')}", "2"))

        elif t == "todos":
            if self.verbose:
                items = ev.get("items") or []
                done = sum(1 for x in items if str(x).strip().lower().startswith("x "))
                self._out(self._c(f"{tag} todo {done}/{len(items)}", "2"))

        elif t == "verify":
            verdict = str(ev.get("verdict") or "pass")
            if verdict == "skipped":
                self._out(self._c(f"{tag} verifier skipped", "2"))
            elif verdict == "pass":
                self._out(self._c(f"{tag} verifier pass · {ev.get('model') or '?'}", "32"))
            else:
                self._out(self._c(f"{tag} verifier FAIL · {ev.get('model') or '?'}", "31"))
                for it in (ev.get("issues") or [])[:5]:
                    for row in U.wrap_display(U.issue_text(it), self.cols,
                                              "    • ", "      "):
                        self._out(self._c(row, "33"))

        elif t == "report":
            self._report(ev)

        elif t == "park":
            self._out(self._c(f"{tag} parked · {ev.get('reason') or ''}", "33"))
        elif t == "stall":
            self._out(self._c(f"{tag} stall (x{ev.get('repeated')})"
                              + (" — switching model" if ev.get("escalate") else ""),
                              "33"))
        elif t == "compact":
            self._out(self._c(f"{tag} compacted · {ev.get('retired') or 0} messages retired",
                              "2"))
        elif t == "sigint":
            self._out(self._c(f"{tag} cancelled in-flight work — the run continues", "33"))
        elif t == "error":
            self._out(self._c(f"{tag} ERROR ", "31")
                      + U.short(ev.get("error"), max(30, self.cols - 12)))
        elif t in ("snapshot", "hook", "parsed_from_text", "swarm_reattach",
                   "agent_reattach"):
            if self.verbose:
                self._out(self._c(f"{tag} {t}", "2"))
        elif t == "user_msg":
            if self.verbose:
                self._out(self._c(f"{tag} steer: "
                                  + U.short(ev.get("text"), max(20, self.cols - 14)), "2"))
