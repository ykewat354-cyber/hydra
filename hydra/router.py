"""Model router: role chains, failover, circuit breakers, error matrix, ledger.

Model names appear only in config defaults and models.json; this module treats
them as data. The optional gate (§8) and registry (§7.2) plug in later phases —
everything must work with them set to None.
"""
from __future__ import annotations

import contextlib
import random
import threading
import time

from . import config as C
from .client import (ApiError, AuthError, BadRequest, ContextOverflow, EmptyReply,
                     ModelMissing, PaidModel, RateLimited, ServerError, StreamStalled)
from .models import DEFAULT_THINK_STYLE  # re-exported; probe results override it


class AllModelsFailed(Exception):
    def __init__(self, errors, all_paid=False):
        self.errors = list(errors)
        self.all_paid = bool(all_paid)
        msg = "; ".join(str(e) for e in self.errors[-3:]) or "no models tried"
        super().__init__(msg)


class Router:
    def __init__(self, cfg: dict, client, prices=None, journal=None, gate=None, registry=None):
        self.cfg = cfg
        self.client = client
        self.prices = prices or {}
        self.journal = journal
        self.gate = gate
        self.registry = registry
        self.sleep = time.sleep
        self._lock = threading.Lock()
        self._breakers = {}   # model -> {"fails": int, "open_until": ts, "cooldown": sec}
        self._base_cooldown = float(C.get(cfg, "routing.breaker_cooldown", 300) or 300)
        self._breaker_fails = int(C.get(cfg, "routing.breaker_fails", 3) or 3)
        self._retries = max(1, int(C.get(cfg, "api.retries", 5) or 5))

    # ------------------------------------------------------------ chains
    def chain_for(self, role: str, exclude=(), need_tools: bool = False) -> list:
        chain = list(C.get(self.cfg, f"roles.{role}.chain") or
                     C.get(self.cfg, "roles.lead.chain") or [])
        if self.registry is not None:
            ok, other = [], []
            for m in chain:
                st = self.registry.status_of(m)
                if st in ("paid", "missing"):
                    continue
                if need_tools and st == "ok" and not self.registry.is_tool_capable(m):
                    continue
                (ok if st == "ok" else other).append(m)
            ranked = ok + other
            if not ranked:
                return []  # all pruned: chat() decides park vs failure
            chain = ranked
        if exclude:
            filtered = [m for m in chain if m not in exclude]
            if filtered:
                chain = filtered
        return chain

    def _raw_chain(self, role: str) -> list:
        return list(C.get(self.cfg, f"roles.{role}.chain") or
                    C.get(self.cfg, "roles.lead.chain") or [])

    def _all_paid(self, role: str) -> bool:
        if self.registry is None:
            return False
        statuses = [self.registry.status_of(m) for m in self._raw_chain(role)]
        return bool(statuses) and all(s == "paid" for s in statuses)

    def think_setting(self, role: str, model: str, override=None):
        setting = override if override is not None else C.get(self.cfg, f"roles.{role}.think")
        setting = "off" if setting is None else str(setting)
        if self.registry is not None:
            style = self.registry.think_style(model)
        else:
            style = DEFAULT_THINK_STYLE.get(model, "bool")
        if style == "none":
            return None  # this model rejects the param entirely
        if style == "levels":
            return {"off": "low", "low": "low", "medium": "medium", "high": "high"}.get(setting, setting)
        return setting.lower() not in ("off", "false", "0", "")

    def options_for(self, role: str):
        opts = {}
        temp = C.get(self.cfg, f"roles.{role}.temperature")
        if temp is not None:
            opts["temperature"] = float(temp)
        npred = C.get(self.cfg, f"roles.{role}.num_predict")
        if npred:
            opts["num_predict"] = int(npred)
        return opts

    # ------------------------------------------------------------ breakers
    def _breaker_open(self, model: str) -> bool:
        with self._lock:
            b = self._breakers.get(model)
            if not b:
                return False
            if b["open_until"] > time.time():
                return True
            if b["open_until"]:
                b["open_until"] = 0
            return False

    def _breaker_fail(self, model: str) -> None:
        with self._lock:
            b = self._breakers.setdefault(model, {"fails": 0, "open_until": 0,
                                                  "cooldown": self._base_cooldown})
            b["fails"] += 1
            if b["fails"] >= self._breaker_fails:
                b["open_until"] = time.time() + b["cooldown"]
                b["cooldown"] = min(3600.0, b["cooldown"] * 2)
                b["fails"] = 0

    def _breaker_ok(self, model: str) -> None:
        with self._lock:
            b = self._breakers.get(model)
            if b:
                b["fails"] = 0

    # ------------------------------------------------------------ misc helpers
    def _backoff(self, attempt: int) -> float:
        return min(30.0, float(2 ** min(attempt, 5))) + random.random()

    def _mark(self, model: str, status: str) -> None:
        if self.registry is not None:
            try:
                self.registry.mark(model, status)
            except Exception:
                pass

    def _gate_shrink(self) -> None:
        if self.gate is not None:
            try:
                self.gate.shrink()
            except Exception:
                pass

    def _slot(self, priority: int):
        if self.gate is None:
            return contextlib.nullcontext()
        return self.gate.slot(priority)

    def _event(self, type_, agent, **fields):
        if self.journal:
            try:
                self.journal.event(type_, getattr(agent, "id", None) if agent else None, **fields)
            except Exception:
                pass

    def _ledger(self, agent, role, model, reply):
        prices = self.prices.get(model, {}) or {}
        est = (reply.prompt_eval_count * float(prices.get("in", 0)) +
               reply.eval_count * float(prices.get("out", 0))) / 1e6
        if agent is not None:
            agent.tokens_in += reply.prompt_eval_count
            agent.tokens_out += reply.eval_count
        if self.journal:
            try:
                self.journal.usage(agent=getattr(agent, "id", None), role=role, model=model,
                                   tin=reply.prompt_eval_count, tout=reply.eval_count,
                                   ms=reply.ms, est=est)
                self.journal.event("model_response", getattr(agent, "id", None),
                                   model=model, role=role, in_tokens=reply.prompt_eval_count,
                                   out_tokens=reply.eval_count, ms=reply.ms,
                                   tool_calls=[(c.get("function") or {}).get("name")
                                               for c in reply.tool_calls])
            except Exception:
                pass

    @staticmethod
    def _is_empty(reply) -> bool:
        return not (reply.content.strip() or reply.tool_calls or reply.thinking.strip())

    # ------------------------------------------------------------ chat
    def chat(self, messages: list, role: str = "lead", tools=None, think=None,
             agent=None, exclude=(), mode=None, on_delta=None) -> "client.Reply":
        chain = self.chain_for(role, exclude, need_tools=bool(tools))
        errors = []
        all_paid = True
        if not chain:
            raise AllModelsFailed(
                [RuntimeError("no usable models: all candidates are paid/missing "
                              "or filtered out")],
                all_paid=self._all_paid(role))
        priority = getattr(agent, "depth", 0) if agent else 0
        for model in chain:
            if self._breaker_open(model):
                continue
            attempt = 0
            repair_left = True
            think_repaired = False
            while True:
                attempt += 1
                self._event("model_request", agent, role=role, model=model,
                            tools=[t.get("function", {}).get("name") for t in (tools or [])],
                            n_messages=len(messages), attempt=attempt)
                try:
                    with self._slot(priority):
                        reply = self.client.chat(
                            model, messages,
                            tools=tools or None,
                            think=self.think_setting(role, model, think),
                            options=self.options_for(role) or None,
                            stream=True, on_delta=on_delta)
                    if self._is_empty(reply):
                        if attempt == 1:
                            messages = list(messages) + [{
                                "role": "user",
                                "content": "Your previous reply was empty. Reply with tool calls or a complete final answer."}]
                            continue
                        raise EmptyReply("empty reply twice")
                    self._breaker_ok(model)
                    self._mark(model, "ok")
                    self._ledger(agent, role, model, reply)
                    return reply
                except AuthError:
                    raise
                except ContextOverflow:
                    raise
                except PaidModel as e:
                    self._mark(model, "paid")
                    errors.append(e)
                    break
                except ModelMissing as e:
                    self._mark(model, "missing")
                    all_paid = False
                    errors.append(e)
                    break
                except RateLimited as e:
                    all_paid = False
                    self._gate_shrink()
                    self.sleep(e.retry_after if e.retry_after is not None else self._backoff(attempt))
                    if attempt < self._retries:
                        continue
                    errors.append(e)
                    break
                except (ServerError, StreamStalled, ConnectionError, TimeoutError,
                        OSError, EmptyReply) as e:
                    all_paid = False
                    self._breaker_fail(model)
                    if attempt < self._retries and not self._breaker_open(model):
                        self.sleep(self._backoff(attempt))
                        continue
                    errors.append(e)
                    break
                except BadRequest as e:
                    all_paid = False
                    body = str(e).lower()
                    if not think_repaired and "think" in body:
                        think_repaired = True
                        if self.registry is not None:
                            try:
                                self.registry.set_think_style(model, "none")
                            except Exception:
                                pass
                        continue
                    if repair_left:
                        repair_left = False
                        messages = list(messages) + [{
                            "role": "user",
                            "content": f"The API rejected the previous request: {e}. "
                                       f"Fix the tool-call format and try again."}]
                        continue
                    errors.append(e)
                    break
        if not errors:
            errors.append(RuntimeError("all candidate models skipped (breakers open)"))
            all_paid = False
        raise AllModelsFailed(errors, all_paid=all_paid)


# re-export for convenience
from .client import Reply  # noqa: E402,F401
