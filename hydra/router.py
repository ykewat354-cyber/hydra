"""Model router: role chains, failover, circuit breakers, error matrix, ledger.

Model names appear only in config defaults and models.json; this module treats
them as data. The optional gate (§8) and registry (§7.2) plug in later phases —
everything must work with them set to None.
"""
from __future__ import annotations

import contextlib
import json
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
    def chain_for(self, role: str, exclude=(), need_tools: bool = False,
                  model_hint: str = None, need_json: bool = False) -> list:
        chain = list(C.get(self.cfg, f"roles.{role}.chain") or
                     C.get(self.cfg, "roles.lead.chain") or [])
        if self.registry is not None:
            ok, other, last = [], [], []
            for m in chain:
                st = self.registry.status_of(m)
                if st in ("paid", "missing"):
                    continue
                if need_tools and st == "ok" and not self.registry.is_tool_capable(m):
                    continue
                if st == "ok" and need_json and not self.registry.is_json_capable(m):
                    last.append(m)   # format= calls prefer json-capable models
                elif st == "ok":
                    ok.append(m)
                else:
                    other.append(m)
            ranked = ok + other + last
            if not ranked:
                return []  # all pruned: chat() decides park vs failure
            chain = ranked
        if model_hint:
            allowed = True
            if self.registry is not None and \
                    self.registry.status_of(model_hint) in ("paid", "missing"):
                allowed = False
            if allowed:
                chain = [model_hint] + [m for m in chain if m != model_hint]
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
             agent=None, exclude=(), mode=None, on_delta=None,
             model: str = None, format=None) -> "client.Reply":
        hint = model or (getattr(agent, "model_override", None)
                         if agent is not None else None)
        if tools:
            format = None  # §19.10: never combine format with tools
        chain = self.chain_for(role, exclude, need_tools=bool(tools),
                               model_hint=hint, need_json=bool(format))
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
                            stream=True, on_delta=on_delta, format=format)
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

    # ------------------------------------------------------------ multi-model (§7.5)
    def _capacity(self) -> int:
        if self.gate is None:
            return 1
        return max(1, int(getattr(self.gate, "plan_cap", 1) or 1))

    def _default_models(self, role: str, n: int = None, mode: str = None) -> list:
        default_n = {"race": 2, "vote": 3, "moa": 2, "critic": 1, "cascade": 2}.get(
            str(mode or "").lower(), 1)
        n = max(1, int(n or default_n))
        chain = self.chain_for(role, need_tools=False) or self.chain_for("lead")
        return list(chain[:n])

    def _judge_model(self, producers) -> str:
        producers = set(producers or [])
        chain = self.chain_for("critic", need_tools=False) or self.chain_for("lead")
        for m in chain:
            if m not in producers:
                return m
        return chain[0] if chain else None

    def _one(self, messages, role, model, think=None, agent=None, on_delta=None):
        return self.chat(messages, role=role, think=think, agent=agent,
                         model=model, on_delta=on_delta)

    def _gather(self, messages, models, role, think=None, agent=None) -> dict:
        """Call every model; truly parallel only when the gate has room."""
        out = {}
        cap = self._capacity()
        if len(models) > 1 and cap >= len(models):
            lock = threading.Lock()

            def call(m):
                try:
                    r = self._one(messages, role, m, think, agent)
                except Exception as exc:
                    r = exc
                with lock:
                    out[m] = r

            threads = [threading.Thread(target=call, args=(m,), daemon=True)
                       for m in models]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        else:
            for m in models:  # free plan: serial, still one call per model
                try:
                    out[m] = self._one(messages, role, m, think, agent)
                except Exception as exc:
                    out[m] = exc
        return out

    @staticmethod
    def _structured_choice(text: str):
        from .agent import parse_json_blob  # tolerant JSON extractor
        obj = parse_json_blob(text or "")
        if not isinstance(obj, dict):
            return None
        for key in ("choice", "answer", "result", "verdict", "value"):
            if key in obj:
                return json.dumps(obj[key], sort_keys=True, ensure_ascii=False)
        return json.dumps(obj, sort_keys=True, ensure_ascii=False)[:200]

    def race(self, messages, models, role="lead", think=None, agent=None,
             timeout=None) -> dict:
        models = list(models)
        errors = []
        cap = self._capacity()
        if len(models) > 1 and cap >= len(models):
            # true race: fire together; the first valid reply wins
            done = threading.Event()
            winner = {}
            lock = threading.Lock()

            def call(m):
                try:
                    r = self._one(messages, role, m, think, agent)
                except Exception as exc:
                    with lock:
                        errors.append(exc)
                    return
                with lock:
                    if not winner and not self._is_empty(r):
                        winner["model"], winner["reply"] = m, r
                        done.set()

            threads = [threading.Thread(target=call, args=(m,), daemon=True)
                       for m in models]
            for t in threads:
                t.start()
            deadline = None if timeout is None else time.time() + float(timeout)
            while not done.is_set():
                if deadline is not None and time.time() >= deadline:
                    break
                if all(not t.is_alive() for t in threads):
                    break
                time.sleep(0.02)
            if winner:
                return {"mode": "race", "model": winner["model"],
                        "reply": winner["reply"], "text": winner["reply"].content}
        else:
            # free plan / no room: degrade to sequential first-valid
            for m in models:
                try:
                    r = self._one(messages, role, m, think, agent)
                except Exception as exc:
                    errors.append(exc)
                    continue
                if not self._is_empty(r):
                    return {"mode": "race-seq", "model": m, "reply": r,
                            "text": r.content}
        if errors:
            raise AllModelsFailed(errors, all_paid=False)
        raise AllModelsFailed([RuntimeError("race: no model produced a reply")])

    def vote(self, messages, models, role="lead", think=None, agent=None) -> dict:
        replies = self._gather(messages, models, role, think, agent)
        good = {m: r for m, r in replies.items()
                if isinstance(r, Reply) and not self._is_empty(r)}
        if not good:
            errs = [r for r in replies.values() if isinstance(r, Exception)]
            raise AllModelsFailed(errs or [RuntimeError("vote: no answers")],
                                  all_paid=False)
        choices = {}
        for m, r in good.items():
            c = self._structured_choice(r.content)
            if c is None:
                choices = None
                break
            choices[m] = c
        lines = "\n".join(f"({i + 1}) {m}: {r.content[:600]}"
                          for i, (m, r) in enumerate(good.items()))
        if choices:
            counts = {}
            for m, c in choices.items():
                counts.setdefault(c, []).append(m)
            ranked = sorted(counts.items(), key=lambda kv: -len(kv[1]))
            if len(ranked) == 1 or len(ranked[0][1]) > len(ranked[1][1]):
                top = ranked[0][0]
                win_model = counts[top][0]
                return {"mode": "vote", "model": win_model, "votes": choices,
                        "text": good[win_model].content,
                        "journal_note": f"majority {top} ({len(counts[top])}/{len(choices)})"}
        judge = self._judge_model(list(good))
        prompt = ("Pick the best answer to the question below. Answer with JSON only: "
                  '{"choice": <number>, "reason": "..."}.\n\n'
                  f"QUESTION:\n{messages[-1].get('content', '')[:1500]}\n\n"
                  f"ANSWERS:\n{lines}")
        jr = self._one([{"role": "user", "content": prompt}], "critic", judge,
                       think, agent)
        idx = 0
        from .agent import parse_json_blob
        obj = parse_json_blob(jr.content or "")
        if isinstance(obj, dict):
            try:
                idx = max(1, min(len(good), int(obj.get("choice") or 1))) - 1
            except Exception:
                idx = 0
        win_model = list(good)[idx]
        return {"mode": "vote+judge", "model": win_model, "judge": judge,
                "votes": choices, "text": good[win_model].content}

    def moa(self, messages, models, role="lead", think=None, agent=None) -> dict:
        replies = self._gather(messages, models, role, think, agent)
        good = {m: r for m, r in replies.items()
                if isinstance(r, Reply) and not self._is_empty(r)}
        if not good:
            raise AllModelsFailed([RuntimeError("moa: no proposals")], all_paid=False)
        if len(good) == 1:
            m = list(good)[0]
            return {"mode": "moa", "model": m, "text": good[m].content}
        drafts = "\n\n".join(f"--- draft from {m} ---\n{r.content[:2500]}"
                             for m, r in good.items())
        agg = self._judge_model(list(good))
        prompt = ("You are the aggregator in a mixture-of-agents run. Synthesize the "
                  "single best final answer from the drafts below. Keep every correct "
                  "fact, drop errors, be concise.\n\n"
                  f"TASK:\n{messages[-1].get('content', '')[:1500]}\n\n{drafts}")
        r = self._one([{"role": "user", "content": prompt}], "lead", agg, think, agent)
        return {"mode": "moa", "model": agg, "aggregator": agg,
                "proposers": list(good), "text": r.content}

    def critic_mode(self, messages, models, role="lead", think=None, agent=None,
                    rounds=2) -> dict:
        models = list(models) or self._default_models(role, 1)
        producer = models[0]
        crit = self._judge_model([producer]) or producer
        draft = self._one(messages, role, producer, think, agent)
        done_rounds = 0
        for i in range(max(1, int(rounds))):
            done_rounds = i + 1
            review = self._one(
                [{"role": "user", "content":
                  "Review this draft against the task. Reply with JSON only: "
                  '{"issues": [{"what": "...", "fix": "..."}]} (empty list if good).\n\n'
                  f"TASK:\n{messages[-1].get('content', '')[:1500]}\n\n"
                  f"DRAFT:\n{draft.content[:3000]}"}],
                "critic", crit, think, agent)
            from .agent import parse_json_blob
            obj = parse_json_blob(review.content or "")
            issues = []
            if isinstance(obj, dict):
                raw = obj.get("issues") or []
                if isinstance(raw, list):
                    issues = [("; ".join(str(v) for v in it.values())
                               if isinstance(it, dict) else str(it)) for it in raw]
            if not issues:
                break
            draft = self._one(
                list(messages) + [
                    {"role": "assistant", "content": draft.content[:3000]},
                    {"role": "user", "content":
                     "A critic reviewed your draft:\n- " + "\n- ".join(issues)
                     + "\n\nRevise and return the improved final answer only."}],
                role, producer, think, agent)
        return {"mode": "critic", "model": producer, "critic": crit,
                "rounds": done_rounds, "text": draft.content}

    @staticmethod
    def accept_ok(text: str, accept) -> bool:
        text = text or ""
        accept = str(accept or "nonempty").lower()
        if accept in ("", "nonempty", "any"):
            return bool(text.strip())
        if accept == "json":
            from .agent import parse_json_blob
            return parse_json_blob(text) is not None
        if accept.startswith("regex:"):
            import re as _re
            try:
                return _re.search(accept.split(":", 1)[1], text, _re.S) is not None
            except Exception:
                return bool(text.strip())
        return bool(text.strip())

    def cascade(self, messages, models=None, role="worker", accept="nonempty",
                think=None, agent=None) -> dict:
        models = list(models or self._default_models(role, 2, mode="cascade"))
        errors = []
        last = None
        last_model = None
        for m in models:
            try:
                r = self._one(messages, role, m, think, agent)
            except Exception as exc:
                errors.append(exc)
                continue
            last, last_model = r, m
            if self.accept_ok(r.content, accept):
                return {"mode": "cascade", "model": m, "accepted": True,
                        "text": r.content}
        if last is not None:
            return {"mode": "cascade", "model": last_model, "accepted": False,
                    "text": last.content}
        raise AllModelsFailed(errors or [RuntimeError("cascade: no models")],
                              all_paid=False)

    def multi(self, prompt: str = None, mode: str = "vote", role: str = "lead",
              models=None, n: int = None, messages=None, accept=None,
              rounds: int = 2, think=None, agent=None) -> dict:
        """Dispatcher behind the ask_models tool (§7.5). Returns a dict with text."""
        msgs = messages or [{"role": "user", "content": str(prompt or "")}]
        mode = str(mode or "vote").lower()
        models = [str(m) for m in (models or [])] or \
            self._default_models(role, n, mode=mode)
        if mode in ("fallback", "single"):
            r = self._one(msgs, role, models[0] if models else None, think, agent)
            return {"mode": "fallback", "model": r.model, "text": r.content}
        if mode == "race":
            return self.race(msgs, models, role, think, agent)
        if mode == "vote":
            return self.vote(msgs, models, role, think, agent)
        if mode == "moa":
            return self.moa(msgs, models, role, think, agent)
        if mode == "critic":
            return self.critic_mode(msgs, models, role, think, agent, rounds=rounds)
        if mode == "cascade":
            return self.cascade(msgs, models, role, accept=accept, think=think,
                                agent=agent)
        raise ValueError(f"unknown mode {mode!r} "
                         f"(fallback|race|vote|moa|critic|cascade)")


# re-export for convenience
from .client import Reply  # noqa: E402,F401
