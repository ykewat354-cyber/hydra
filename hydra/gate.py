"""The LLM gate (§8): one global concurrency gate over model calls.

* capacity = plan slots (free 1 / pro 3 / max 10) or a config override
* priority = deeper agents first (a blocked parent is waiting on them), then FIFO
* held only while a stream is in flight; released before tools run
* AIMD: on rejection the capacity drops by 1 (min 1) and stays down for a
  cooldown window, then ramps +1 per check back up to the plan cap.
"""
from __future__ import annotations

import heapq
import threading
import time


class _Slot:
    def __init__(self, gate: "LLMGate", priority: int):
        self.gate = gate
        self.priority = priority

    def __enter__(self):
        self.gate.acquire(self.priority)
        return self

    def __exit__(self, *exc):
        self.gate.release()
        return False


class LLMGate:
    def __init__(self, capacity: int = 1, shrink_secs: float = 300.0,
                 semantics: str = ""):
        self.plan_cap = max(1, int(capacity))
        self.cap = self.plan_cap
        self.shrink_secs = float(shrink_secs)
        self.semantics = str(semantics or "")   # F5: queue | reject:<status>
        self.last_rejection = None              # last status note_rejection saw
        self.shrink_until = 0.0
        self.inflight = 0
        self.max_observed = 0
        self.granted = 0
        self._seq = 0
        self._waiters: list = []
        self._cond = threading.Condition()

    # -------------------------------------------------- internals
    @staticmethod
    def _now() -> float:
        return time.monotonic()

    def _cap_now(self) -> int:
        if self.shrink_until and self._now() >= self.shrink_until:
            self.shrink_until = 0.0
            if self.cap < self.plan_cap:
                self.cap += 1  # ramp back one step per check
        return self.cap

    # -------------------------------------------------- api
    @property
    def waiting(self) -> int:
        with self._cond:
            return len(self._waiters)

    def acquire(self, priority: int = 0, timeout: float = None) -> None:
        deadline = None if timeout is None else self._now() + float(timeout)
        with self._cond:
            self._seq += 1
            me = (-int(priority), self._seq)
            heapq.heappush(self._waiters, me)
            try:
                while True:
                    if (self.inflight < self._cap_now()
                            and self._waiters and self._waiters[0] == me):
                        heapq.heappop(self._waiters)
                        self.inflight += 1
                        self.granted += 1
                        self.max_observed = max(self.max_observed, self.inflight)
                        return
                    if deadline is not None:
                        remaining = deadline - self._now()
                        if remaining <= 0:
                            raise TimeoutError("LLM gate wait timed out")
                        self._cond.wait(remaining)
                    else:
                        self._cond.wait(0.25)  # periodic wake for the AIMD window
            except BaseException:
                try:
                    if me in self._waiters:
                        self._waiters.remove(me)
                        heapq.heapify(self._waiters)
                except Exception:
                    pass
                raise

    def release(self) -> None:
        with self._cond:
            if self.inflight > 0:
                self.inflight -= 1
            self._cond.notify_all()

    def slot(self, priority: int = 0) -> _Slot:
        return _Slot(self, priority)

    def shrink(self) -> None:
        """AIMD decrease: -1 (min 1) and hold the cap down for shrink_secs."""
        with self._cond:
            if self.cap > 1:
                self.cap -= 1
            self.shrink_until = self._now() + self.shrink_secs
            self._cond.notify_all()

    def note_rejection(self, status: int = None) -> None:
        """F5: react to a rejection using the calibrated semantics.

        ``queue`` means concurrency is not what the server refuses, so running
        fewer calls in parallel will not help — keep the width.  Uncalibrated
        (or ``reject:*``) keeps the classic AIMD shrink.
        """
        self.last_rejection = status
        if self.semantics.startswith("queue"):
            return
        self.shrink()
