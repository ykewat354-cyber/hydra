"""WO1 T24 — tool robustness.

Covers: bash timeout kills the process group (partial output, no orphans);
background job start/status/output/kill round-trip; >cap output spills to a
file that read_file can page; unicode round-trip keeps events.jsonl valid
JSON; read_file on a binary file is a friendly error; edit_file reports the
occurrence count and still has the whitespace-normalized fallback; four
parallel bash calls append cleanly and the journal stays line-JSON.

Most items already had partial coverage (tests/test_phase1.py: non-unique
edit; tests/test_phase3.py: spill readable; tests/test_phase9.py:
whitespace fallback). This file adds the missing 200,000-char, binary,
background-job, timeout/orphan and parallel-journal checks.
"""
from __future__ import annotations

import json
import os
import re
import time
import unittest
from pathlib import Path

from tests.test_phase1 import Base

JOB = re.compile(r"started job (\S+) \(pid (\d+)\)")


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return False


def _procs_with_marker(marker: str) -> list:
    """[(pid, cmdline)] for every /proc entry whose cmdline contains marker."""
    out = []
    proc = Path("/proc")
    if not proc.is_dir():
        return out
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if marker.encode() in raw:
            out.append((entry.name, raw.decode("utf-8", "replace").replace("\x00", " ")))
    return out


def _wait_for(pred, timeout: float = 5.0, step: float = 0.1):
    end = time.time() + timeout
    while True:
        val = pred()
        if val or time.time() >= end:
            return val
        time.sleep(step)


class ToolRobustnessTests(Base):
    def _rt(self):
        from hydra.runtime import Runtime
        rt = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        return rt, rt.start_run("wo1 t24")

    @staticmethod
    def _exec(rt, agent, tool: str, args: dict) -> str:
        call = {"function": {"name": tool, "arguments": args}}
        return rt.executor.run([call], agent)[0]["content"]

    # ---------------------------------------------------------------- timeout
    def test_bash_timeout_partial_output_no_orphan(self):
        rt, lead = self._rt()
        marker = "846013"  # unique sleep duration = our orphan fingerprint
        try:
            text = self._exec(rt, lead, "bash", {
                "cmd": f'echo before-timeout; echo "leader=$$"; sleep {marker}',
                "timeout": 5})
            self.assertIn("before-timeout", text)
            self.assertIn("[timeout after 5", text)
            leader = int(re.search(r"leader=(\d+)", text).group(1))
            self.assertTrue(_wait_for(lambda: not _pid_alive(leader)),
                            f"shell pid {leader} survived the timeout")
            self.assertTrue(_wait_for(lambda: not _procs_with_marker(marker)),
                            f"orphan sleep left behind: {_procs_with_marker(marker)}")
        finally:
            rt.close()

    # ---------------------------------------------------------------- jobs
    def test_background_job_roundtrip(self):
        rt, lead = self._rt()
        try:
            import gc
            import warnings
            with warnings.catch_warnings(record=True) as caught:
                warnings.simplefilter("always")
                out = self._exec(rt, lead, "bash", {
                    "cmd": "echo job-started; sleep 1.0; echo job-done",
                    "background": True})
                gc.collect()
            leaked = [str(w.message) for w in caught
                      if issubclass(w.category, ResourceWarning)]
            self.assertFalse(leaked, leaked)
            m = JOB.search(out)
            self.assertIsNotNone(m, out)
            jid, pid = m.group(1), int(m.group(2))
            status = self._exec(rt, lead, "job_status", {"id": jid})
            self.assertIn(jid, status)
            self.assertIn("running", status)

            got = ""
            def _appeared():
                nonlocal got
                got = self._exec(rt, lead, "job_output", {"id": jid, "tail": 10})
                return "job-done" in got
            self.assertTrue(_wait_for(_appeared, timeout=8), got)
            self.assertIn("job-started", got)
            self.assertIn("exited(0)", self._exec(rt, lead, "job_status", {"id": jid}))

            # kill round-trip on a fresh long job
            out = self._exec(rt, lead, "bash",
                             {"cmd": "sleep 30", "background": True})
            jid2, pid2 = JOB.search(out).group(1), int(JOB.search(out).group(2))
            self.assertIn("killed job", self._exec(rt, lead, "job_kill", {"id": jid2}))
            self.assertTrue(_wait_for(
                lambda: "exited" in self._exec(rt, lead, "job_status", {"id": jid2})))
            self.assertTrue(_wait_for(lambda: not _pid_alive(pid2)))

            self.assertIn("ERROR", self._exec(rt, lead, "job_output", {"id": "nope"}))
            self.assertIn("ERROR", self._exec(rt, lead, "job_kill", {"id": "nope"}))
        finally:
            rt.close()

    def test_background_job_reattached_after_resume(self):
        """A job journaled before the crash must not vanish on resume.

        Regression for T14: job_status used to report "no background jobs",
        so the resumed agent re-ran the same long command and duplicated its
        side effects.
        """
        from hydra.runtime import Runtime
        rt, lead = self._rt()
        try:
            out = self._exec(rt, lead, "bash", {
                "cmd": "echo job-start; sleep 2; echo job-done",
                "background": True})
            m = JOB.search(out)
            self.assertIsNotNone(m, out)
            jid, pid = m.group(1), int(m.group(2))
            proc = rt.jobs[jid]["proc"]
            run_id = rt.run_id
        finally:
            rt.close()

        rt2 = Runtime(self.cfg(), cwd=self.cwd.name, quiet=True)
        try:
            lead2 = rt2.resume(run_id)
            status = self._exec(rt2, lead2, "job_status", {"id": jid})
            self.assertIn(jid, status)
            self.assertNotIn("unknown", status)
            got = ""

            def _appeared():
                nonlocal got
                got = self._exec(rt2, lead2, "job_output", {"id": jid, "tail": 10})
                return "job-done" in got

            self.assertTrue(_wait_for(_appeared, timeout=8), got)
            self.assertTrue(_wait_for(
                lambda: "exited" in self._exec(rt2, lead2, "job_status", {"id": jid})))
            self.assertTrue(_wait_for(lambda: proc.poll() is not None))
            self.assertNotEqual(rt2.next_job_id(), jid)
        finally:
            rt2.close()

    # ---------------------------------------------------------------- spill
    def test_200k_chars_capped_and_spill_paged(self):
        rt, lead = self._rt()
        try:
            text = self._exec(rt, lead, "bash", {
                "cmd": "head -c 200000 /dev/zero | tr '\\0' '\\n'"})
            self.assertIn("chars omitted", text)
            self.assertLess(len(text), 20000)
            m = re.search(r"full output: ([^\]\n]+)\]", text)
            self.assertIsNotNone(m, text)
            spill = Path(m.group(1).strip())
            self.assertTrue(spill.is_file(), spill)
            self.assertEqual(len(spill.read_text(encoding="utf-8")), 200000)

            page = self._exec(rt, lead, "read_file",
                              {"path": str(spill), "start": 1, "end": 50})
            self.assertIn("lines 1-50", page)
            self.assertIn("continue with start=51", page)
            self.assertIn("50: ", page)
        finally:
            rt.close()

    # ---------------------------------------------------------------- unicode
    def test_unicode_roundtrip_and_journal_valid_json(self):
        from hydra import config as C
        rt, lead = self._rt()
        try:
            content = "नमस्ते 🙂\n"
            self.assertIn("wrote", self._exec(rt, lead, "write_file",
                                              {"path": "uni.txt", "content": content}))
            read = self._exec(rt, lead, "read_file", {"path": "uni.txt"})
            self.assertIn("नमस्ते 🙂", read)
            self.assertEqual((Path(self.cwd.name) / "uni.txt").read_text(
                encoding="utf-8"), content)

            events_file = C.run_dir(rt.run_id) / "events.jsonl"
            raw = events_file.read_text(encoding="utf-8")
            self.assertIn("नमस्ते 🙂", raw)  # stored raw, not \uXXXX escapes
            events = [json.loads(line) for line in raw.splitlines() if line.strip()]
            writes = [e for e in events if e.get("type") == "tool_call"
                      and e.get("tool") == "write_file"]
            self.assertTrue(writes)
            self.assertEqual(writes[0]["args"]["content"], content)
            previews = [e.get("preview", "") for e in events
                        if e.get("type") == "tool_result"]
            self.assertTrue(any("नमस्ते 🙂" in p for p in previews), previews)
        finally:
            rt.close()

    # ---------------------------------------------------------------- binary
    def test_read_file_binary_friendly_error(self):
        p = Path(self.cwd.name) / "img.dat"
        p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 64)
        rt, lead = self._rt()
        try:
            out = self._exec(rt, lead, "read_file", {"path": "img.dat"})
            self.assertTrue(out.startswith("ERROR:"), out)
            self.assertIn("binary", out.lower())
            self.assertNotIn("Traceback", out)
            out2 = self._exec(rt, lead, "read_file",
                              {"path": "img.dat", "start": 1, "end": 10})
            self.assertTrue(out2.startswith("ERROR:"), out2)
        finally:
            rt.close()

    # ---------------------------------------------------------------- edit
    def test_edit_count_and_whitespace_fallback(self):
        d = Path(self.cwd.name)
        rt, lead = self._rt()
        try:
            dup = d / "dup.txt"
            dup.write_text("alpha = 1\nbeta = 2\nalpha = 1\n", encoding="utf-8")
            out = self._exec(rt, lead, "edit_file",
                             {"path": "dup.txt", "old": "alpha = 1",
                              "new": "alpha = 3"})
            self.assertIn("ERROR", out)
            self.assertIn("occurs 2 times", out)
            self.assertIn("count=2", out)
            self.assertEqual(dup.read_text(encoding="utf-8"),
                             "alpha = 1\nbeta = 2\nalpha = 1\n")
            out = self._exec(rt, lead, "edit_file",
                             {"path": "dup.txt", "old": "alpha = 1",
                              "new": "alpha = 3", "count": 2})
            self.assertIn("edited", out)
            self.assertEqual(dup.read_text(encoding="utf-8"),
                             "alpha = 3\nbeta = 2\nalpha = 3\n")

            ws = d / "ws.txt"
            ws.write_text("def f():\n    return 1\n", encoding="utf-8")
            out = self._exec(rt, lead, "edit_file",
                             {"path": "ws.txt", "old": "def f():\n  return 1",
                              "new": "def f():\n    return 2"})
            self.assertIn("edited", out)
            self.assertEqual(ws.read_text(encoding="utf-8"),
                             "def f():\n    return 2\n")

            amb = d / "amb.txt"
            amb.write_text("x =  1\ny = 2\nx =\t1\n", encoding="utf-8")
            out = self._exec(rt, lead, "edit_file",
                             {"path": "amb.txt", "old": "x = 1", "new": "x = 9"})
            self.assertIn("ERROR", out)
            self.assertIn("whitespace", out)
            self.assertIn("2 places", out)
            self.assertEqual(amb.read_text(encoding="utf-8"),
                             "x =  1\ny = 2\nx =\t1\n")
        finally:
            rt.close()

    # ---------------------------------------------------------------- parallel
    def test_four_parallel_bash_appends_and_journal_is_jsonl(self):
        from hydra import config as C
        rt, lead = self._rt()
        try:
            calls = []
            for c in range(1, 5):
                cmd = ("for i in $(seq 1 25); do printf '{{\"call\": {c}, "
                       "\"i\": %s}}\\n' \"$i\" >> events.jsonl; done").format(c=c)
                calls.append({"function": {"name": "bash",
                                           "arguments": {"cmd": cmd}}})
            results = rt.executor.run(calls, lead)
            self.assertEqual(len(results), 4)

            # the four parallel appenders share one file, line by line
            lines = [l for l in (Path(self.cwd.name) / "events.jsonl")
                     .read_text(encoding="utf-8").splitlines() if l.strip()]
            self.assertEqual(len(lines), 100)
            rows = [json.loads(l) for l in lines]
            for c in range(1, 5):
                self.assertEqual(sum(1 for r in rows if r["call"] == c), 25)
                self.assertEqual(sorted(r["i"] for r in rows if r["call"] == c),
                                 list(range(1, 26)))

            # the run journal (also events.jsonl) must stay line-JSON while
            # four tool threads log concurrently
            events_file = C.run_dir(rt.run_id) / "events.jsonl"
            events = [json.loads(line) for line
                      in events_file.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
            types = [e["type"] for e in events]
            self.assertGreaterEqual(types.count("tool_call"), 4)
            self.assertGreaterEqual(types.count("tool_result"), 4)
        finally:
            rt.close()


if __name__ == "__main__":
    unittest.main()
