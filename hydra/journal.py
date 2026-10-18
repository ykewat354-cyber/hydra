"""Run journal: append-only events, usage ledger, transcripts, atomic run.json.

Durability: append + flush per event; fsync per ``journal.fsync`` policy
(event | turn | never). Every state transition is written before it takes effect
so that any ``kill -9`` stays resumable.
"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from . import config as C


def now() -> float:
    return time.time()


def new_run_id() -> str:
    base = time.strftime("r_%m%d_%H%M")
    i = 1
    rid = base
    while C.run_dir(rid).exists():
        i += 1
        rid = f"{base}-{i}"
    return rid


def run_ids() -> list:
    d = C.home() / "runs"
    if not d.is_dir():
        return []
    return sorted((p.name for p in d.iterdir() if p.is_dir()), reverse=True)


def load_run(run_id: str):
    p = C.run_dir(run_id) / "run.json"
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_run_atomic(run_id: str, data: dict) -> None:
    p = C.run_dir(run_id) / "run.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name("run.json.tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, p)


def read_events(run_id: str, agent: str = None) -> list:
    p = C.run_dir(run_id) / "events.jsonl"
    out = []
    if not p.is_file():
        return out
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except json.JSONDecodeError:
                continue
            if agent is None or ev.get("agent") == agent:
                out.append(ev)
    return out


def read_usage(run_id: str = None) -> list:
    files = []
    if run_id:
        files = [C.run_dir(run_id) / "usage.jsonl"]
    else:
        d = C.home() / "runs"
        if d.is_dir():
            files = sorted(d.glob("*/usage.jsonl"))
    out = []
    for f in files:
        if not f.is_file():
            continue
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
    return out


def last_run_in(cwd: str):
    best = None
    for rid in run_ids():
        data = load_run(rid) or {}
        if str(data.get("cwd") or "") == str(cwd):
            best = rid
            break
    return best


def find_run(name_or_id: str):
    if not name_or_id:
        return None
    if (C.run_dir(name_or_id) / "run.json").is_file():
        return name_or_id
    for rid in run_ids():
        data = load_run(rid) or {}
        if data.get("name") == name_or_id:
            return rid
    return None


class Journal:
    """One run's journal. Thread-safe append-only writer."""

    def __init__(self, run_id: str, cfg: dict, sink=None, redact=None, seq0: int = 0):
        self.run_id = run_id
        self.cfg = cfg
        self.dir = C.run_dir(run_id)
        for sub in ("agents", "spill", "artifacts", "jobs"):
            (self.dir / sub).mkdir(parents=True, exist_ok=True)
        self.sink = sink
        self.redact = redact
        self.lock = threading.RLock()
        self.seq = int(seq0)
        self.fsync_policy = str(C.get(cfg, "journal.fsync", "turn"))
        self._events = open(self.dir / "events.jsonl", "a", encoding="utf-8")
        self._usage = open(self.dir / "usage.jsonl", "a", encoding="utf-8")
        self._bb = open(self.dir / "blackboard.jsonl", "a", encoding="utf-8")

    # -------------------------------------------------- redaction
    def _clean(self, obj):
        if self.redact is None:
            return obj
        if isinstance(obj, str):
            return self.redact(obj)
        if isinstance(obj, dict):
            return {k: self._clean(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self._clean(v) for v in obj]
        return obj

    # -------------------------------------------------- events
    def event(self, type_: str, agent=None, **fields) -> dict:
        with self.lock:
            self.seq += 1
            ev = {"ts": round(now(), 3), "run": self.run_id, "agent": agent,
                  "seq": self.seq, "type": type_}
            ev.update(fields)
            ev = self._clean(ev)
            self._events.write(json.dumps(ev, ensure_ascii=False) + "\n")
            self._events.flush()
            if self.fsync_policy == "event":
                self._fsync_file(self._events)
        if self.sink:
            try:
                self.sink(ev)
            except Exception:
                pass
        return ev

    def usage(self, agent=None, role: str = "", model: str = "", tin: int = 0,
              tout: int = 0, ms: int = 0, est: float = 0.0) -> None:
        rec = {"ts": round(now(), 3), "run": self.run_id, "agent": agent, "role": role,
               "model": model, "in": int(tin), "out": int(tout), "ms": int(ms),
               "est_usd": round(float(est), 6)}
        with self.lock:
            self._usage.write(json.dumps(rec, ensure_ascii=False) + "\n")
            self._usage.flush()

    def transcript(self, agent_id: str, record: dict) -> None:
        p = self.dir / "agents" / str(agent_id)
        p.mkdir(parents=True, exist_ok=True)
        rec = self._clean(record)
        with self.lock:
            with open(p / "transcript.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def read_transcript(self, agent_id: str) -> list:
        p = self.dir / "agents" / str(agent_id) / "transcript.jsonl"
        out = []
        if p.is_file():
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            out.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        return out

    # -------------------------------------------------- blackboard
    def blackboard_append(self, key: str, value: str, agent=None, persist: bool = False) -> None:
        rec = {"ts": round(now(), 3), "run": self.run_id, "agent": agent,
               "key": str(key), "value": str(value)}
        with self.lock:
            self._bb.write(json.dumps(self._clean(rec), ensure_ascii=False) + "\n")
            self._bb.flush()
        if persist:
            mem = C.home() / "memory"
            mem.mkdir(parents=True, exist_ok=True)
            safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in str(key))
            with open(mem / f"{safe}.md", "a", encoding="utf-8") as fh:
                fh.write(f"- {time.strftime('%Y-%m-%d %H:%M')} {key}: {value}\n")

    def blackboard_read(self, key: str = None) -> list:
        p = self.dir / "blackboard.jsonl"
        out = []
        if p.is_file():
            with open(p, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if key is None or rec.get("key") == key:
                        out.append(rec)
        return out

    # -------------------------------------------------- durability
    @staticmethod
    def _fsync_file(fh) -> None:
        try:
            fh.flush()
            os.fsync(fh.fileno())
        except Exception:
            pass

    def turn_boundary(self) -> None:
        with self.lock:
            if self.fsync_policy in ("event", "turn"):
                for fh in (self._events, self._usage, self._bb):
                    self._fsync_file(fh)

    def checkpoint(self, data: dict) -> None:
        save_run_atomic(self.run_id, data)
        self.turn_boundary()

    def close(self) -> None:
        with self.lock:
            for fh in (self._events, self._usage, self._bb):
                try:
                    fh.flush()
                    os.fsync(fh.fileno())
                    fh.close()
                except Exception:
                    pass
