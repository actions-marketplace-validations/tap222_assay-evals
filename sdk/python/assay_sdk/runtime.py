"""Run an evaluator over many samples within limits, and see what the run cost.

    import assay_sdk as assay

    rt = assay.EvalRuntime(concurrency=10, retries=3, timeout=30, rate_limit=50,
                           retry_on=[429, 500, 502, 503], max_time=600, budget_usd=5,
                           prices={"claude-opus-5": (5, 25)})
    report = rt.run(judge, samples, schema=VERDICT, threshold=0.7)   # or: await rt.arun(...)
    print(report)

    50 samples in 1m12s

    46 judged (41 passed, 5 failed)
     2 invalid judge outputs
     1 rate limited
     1 timed out

    LLM calls:      57
    Retries:        7
    Tokens:         68,400 in, 4,560 out
    Estimated cost: $0.46

`judge` is anything evaluate() takes (a function, an async function, an assay_sdk.Judge), and
each result is an evaluate() Result: INVALID, TIMEOUT and RATE_LIMITED are never scores. A
sample is passed to the judge as its one argument; a tuple is spread over its arguments; a
Sample(*args, id=, run=, inputs=, **kwargs) says exactly what goes where, and records its
result as a check on its own test case's run.

The limits:

  concurrency   samples judged at once
  rate_limit    model calls a minute, spaced evenly and shared by every sample; a 429 pauses
                them all for as long as the provider asked (Retry-After)
  timeout       seconds for one model call (for a judge that isn't a Judge, for one attempt)
  retries       asks again per sample, in all: after a timeout, a connection error, an HTTP
                error whose status is in retry_on, and an answer that isn't a verdict
                (retry_invalid). Anything else, a bug in the judge, is final at once.
  max_time      seconds for the whole run; budget_usd, dollars. When one is reached no more
                samples start, and those left are reported as not run, never as failures.

One retry layer, counted: calls made through Judge have the provider SDK's own retries switched
off, so every request is one LLM call in the report and its tokens are counted once. Another
judge's attempt counts as one call, unless its calls are seen: through Judge, or with
assay.instrument() on. An attempt that made more than one call is reported, because that is
where doubled costs hide.

Cost is the provider's own figure when it gives one (LiteLLM, OpenRouter), and otherwise
tokens times `prices`: {model or model prefix: (input, output[, cached]) dollars per million
tokens}, or ASSAY_PRICES as JSON. Nothing is guessed: a model with no price is reported as such.

One sample's failure is its own: an exception, a timeout, even a CancelledError a library raises
on a 429, is that sample's result and never stops the others. Cancelling the run itself stops
it: rt.report holds what was judged so far (and run() returns it after Ctrl-C).

rt.map(fn, items) runs your own function per item under the same limits and accounting.
"""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import email.utils
import functools
import hashlib
import inspect
import json
import os
import random
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from assay_sdk.evaluation import INVALID, PASS, FAIL, Result, _failed, _record, _step

RETRY_ON = (408, 429, 500, 502, 503, 504, 529)
NOT_RUN, DONE = "NOT_RUN", "DONE"
_TRANSPORT = ("timeout", "rate_limited", "unavailable")

_current: "contextvars.ContextVar[Optional[_Scope]]" = contextvars.ContextVar("assay_eval_scope", default=None)
_in_judge: "contextvars.ContextVar[bool]" = contextvars.ContextVar("assay_in_judge", default=False)


def current() -> Optional["_Scope"]:
    """The sample being judged, while an EvalRuntime runs one (None otherwise)."""
    return _current.get()


class Sample:
    """What to pass the judge for one sample: Sample(question, answer, id="q7", run=run)."""

    def __init__(self, *args, id: Any = None, run: Any = None, inputs: Optional[dict] = None, **kwargs):
        self.args, self.kwargs, self.id, self.run, self.inputs = args, kwargs, id, run, inputs

    def __repr__(self):
        return f"Sample(id={self.id!r}, args={self.args!r}, kwargs={self.kwargs!r})"


def _as_sample(x: Any, i: int, spread: bool = True) -> Sample:
    if isinstance(x, Sample):
        if x.id is None:
            x.id = i
        return x
    return Sample(*x, id=i) if spread and isinstance(x, tuple) else Sample(x, id=i)


# ---------- prices ----------

def _prices(given: Optional[dict]) -> Dict[str, tuple]:
    if given is None:
        try:
            given = json.loads(os.environ.get("ASSAY_PRICES") or "{}")
        except ValueError:
            raise ValueError("ASSAY_PRICES isn't JSON: {\"model\": [input, output, cached], ...} (dollars per "
                             "million tokens)")
    out = {}
    for model, p in (given or {}).items():
        p = tuple(float(x) for x in (p if isinstance(p, (list, tuple)) else [p]))
        if len(p) not in (2, 3):
            raise ValueError(f"The price of {model}: (input, output) or (input, output, cached), dollars per "
                             f"million tokens")
        out[model] = p
    return out


_env_prices: Tuple[Optional[str], Dict[str, tuple]] = (None, {})


def env_prices() -> Dict[str, tuple]:
    """ASSAY_PRICES, read once per value; {} when unset or unreadable (recording never fails on it)."""
    global _env_prices
    raw = os.environ.get("ASSAY_PRICES")
    if raw != _env_prices[0]:
        try:
            _env_prices = (raw, _prices(None))
        except ValueError:
            _env_prices = (raw, {})
    return _env_prices[1]


def price_of(prices: Dict[str, tuple], model: Optional[str]) -> Optional[tuple]:
    """The price for a model: its own, or the longest model prefix that has one."""
    if not model:
        return None
    keys = [k for k in prices if model == k or model.startswith(k)]
    return prices[max(keys, key=len)] if keys else None


def cost_of(resp, prices: Dict[str, tuple]) -> Optional[float]:
    """Dollars for one call: the provider's figure, or tokens times the price. None if unknown."""
    if getattr(resp, "cost", None) is not None:
        return float(resp.cost)
    u = resp.usage or {}
    if getattr(resp, "error", None) and u.get("input") is None and u.get("output") is None:
        return 0.0  # a request that failed before any tokens (a 429, a 5xx): nothing to pay
    p = price_of(prices, resp.model)
    if p is None or u.get("input") is None:
        return None
    cached = u.get("cached") or 0
    fresh = u["input"] if resp.provider == "anthropic" else max(0, u["input"] - cached)  # Anthropic counts them apart
    out = (u.get("output") or 0) + ((u.get("reasoning") or 0) if resp.provider == "gemini" else 0)
    per_cached = p[2] if len(p) == 3 else p[0]  # no cached price: the input price, an upper bound
    return (fresh * p[0] + cached * per_cached + out * p[1]) / 1e6


# ---------- limits ----------

class _Limiter:
    """Calls a minute, evenly spaced, for every thread and task of a run; hold() pauses them all."""

    def __init__(self, per_minute: Optional[float]):
        self.interval = 60.0 / per_minute if per_minute else 0.0
        self._next = self._held = 0.0
        self._lock = threading.Lock()

    def reserve(self) -> float:
        with self._lock:
            now = time.monotonic()
            slot = max(now, self._next, self._held)
            self._next = slot + self.interval
            return slot - now

    def hold(self, seconds: float) -> None:
        with self._lock:
            self._held = max(self._held, time.monotonic() + seconds)


def _code(exc: Optional[BaseException]) -> Optional[int]:
    if exc is None:
        return None
    code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def retry_after(exc: Optional[BaseException]) -> Optional[float]:
    """Seconds the provider asked to wait (Retry-After, retry-after-ms), if it said."""
    headers = getattr(exc, "headers", None) or getattr(getattr(exc, "response", None), "headers", None)
    if not headers or not hasattr(headers, "get"):
        return None
    try:
        ms = headers.get("retry-after-ms")
        if ms:
            return float(ms) / 1000
        v = headers.get("retry-after")
        if not v:
            return None
        try:
            return max(0.0, float(v))
        except ValueError:
            when = email.utils.parsedate_to_datetime(v)
            return max(0.0, when.timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class _State:
    """A run's totals, shared by its samples (and the threads sync judges run on)."""

    def __init__(self, rt: "EvalRuntime"):
        self.rt, self.lock = rt, threading.Lock()
        self.started = time.monotonic()
        self.deadline = self.started + rt.max_time if rt.max_time else None
        self.calls = self.retries = self.unpriced = self.repeated = 0
        self.tokens = {"input": 0, "output": 0, "cached": 0}
        self.cost, self.priced = 0.0, 0
        self.models: Counter = Counter()
        self.unpriced_models: Counter = Counter()
        self.stopping = False
        self.stopped: Optional[str] = None

    def remaining(self) -> Optional[float]:
        return None if self.deadline is None else self.deadline - time.monotonic()

    def why_stop(self) -> Optional[str]:
        if self.stopped:
            return self.stopped
        rem = self.remaining()
        if rem is not None and rem <= 0:
            self.stopped = f"the run's time limit (max_time {self.rt.max_time:g}s) was reached"
        elif self.rt.budget_usd is not None and self.cost >= self.rt.budget_usd:
            self.stopped = f"the run's budget (${self.rt.budget_usd:g}) was reached"
        return self.stopped


class _Scope:
    """One sample being judged: its calls, retries and cost, and the run's limits as they apply to it."""

    def __init__(self, state: _State):
        self.state, self.rt = state, state.rt
        self.calls = self.retries = 0
        self.cost: Optional[float] = None
        self._attempt()

    def _attempt(self):
        self.attempt_calls = self.attempt_judge_calls = self.attempt_inner_retries = 0
        self.prepaid = False

    # --- a model call ---

    def _wait(self) -> Optional[float]:
        """Seconds to wait for a slot, or None: the run's time is up."""
        if self.prepaid:  # the runtime took this attempt's slot already
            self.prepaid = False
            return 0.0
        wait = self.rt._limiter.reserve()
        rem = self.state.remaining()
        return None if rem is not None and wait >= rem else wait

    def gate(self) -> Optional[str]:
        wait = self._wait()
        if wait is None:
            return f"the run's time limit (max_time {self.rt.max_time:g}s) was reached"
        if wait:
            time.sleep(wait)
        return None

    async def agate(self) -> Optional[str]:
        wait = self._wait()
        if wait is None:
            return f"the run's time limit (max_time {self.rt.max_time:g}s) was reached"
        if wait:
            await asyncio.sleep(wait)
        return None

    def call_timeout(self) -> Optional[float]:
        """Seconds one call may take: timeout, or what's left of max_time if that's less."""
        rem = self.state.remaining()
        ts = [t for t in (self.rt.timeout, rem) if t is not None]
        return max(0.001, min(ts)) if ts else None

    def counted(self, resp, judge: bool = False) -> None:
        """One call made, and what it used."""
        cost = cost_of(resp, self.rt.prices)
        u = resp.usage or {}
        with self.state.lock:
            s = self.state
            s.calls += 1
            s.models[resp.model or "unknown"] += 1
            for k in ("input", "output", "cached"):
                s.tokens[k] += u.get(k) or 0
            if cost is None:
                s.unpriced += 1
                s.unpriced_models[resp.model or "unknown"] += 1
            else:
                s.cost += cost
                s.priced += 1
        self.calls += 1
        self.attempt_calls += 1
        self.attempt_judge_calls += judge
        if cost is not None:
            self.cost = (self.cost or 0.0) + cost

    # --- asking again ---

    def delay(self, i: int, exc: Optional[BaseException], kind: Optional[str]) -> Optional[float]:
        """Seconds to wait before asking again, or None: no retries left, or no time or money."""
        if self.retries >= self.rt.retries or self.state.why_stop():
            return None
        wait = min(self.rt.max_backoff, self.rt.backoff * 2 ** i) * random.uniform(0.5, 1.0) if self.rt.backoff else 0.0
        asked = retry_after(exc)
        if asked is not None:
            if asked > self.rt.max_backoff:  # the provider wants longer than we'll wait
                return None
            wait = max(wait, asked)
        rem = self.state.remaining()
        if rem is not None and wait >= rem:
            return None
        if kind == "rate_limited":  # every sample waits, not just this one
            self.rt._limiter.hold(wait)
        return wait

    def _retried(self, inner: bool = False) -> None:
        self.retries += 1
        self.attempt_inner_retries += inner
        with self.state.lock:
            self.state.retries += 1

    def retry_call(self, resp, i: int) -> bool:
        """After a failed call made through Judge: wait and return True to ask again."""
        if resp.error_kind not in _TRANSPORT or not self.rt.retryable(resp.error_kind, _code(resp.exception)):
            return False
        wait = self.delay(i, resp.exception, resp.error_kind)
        if wait is None:
            return False
        time.sleep(wait)
        self._retried(inner=True)
        return True

    def end_attempt(self, out: Any) -> None:
        from assay_sdk.llm import Response
        if self.attempt_calls == 0:  # no call was seen: the attempt was one
            self.counted(out if isinstance(out, Response) else _Unseen())
        if self.attempt_calls - self.attempt_inner_retries > 1:
            with self.state.lock:
                self.state.repeated += 1


UNSEEN = "(calls not seen)"


class _Unseen:
    usage, model, provider, cost = {}, UNSEEN, None, None


def observe(provider: str, resp: Any = None, exc: Optional[BaseException] = None) -> None:
    """A provider call seen by assay.instrument(): counted for the sample being judged."""
    scope = _current.get()
    if scope is None or _in_judge.get():
        return
    from assay_sdk.llm import Response, normalize
    try:
        r = normalize(resp, provider) if exc is None else Response(provider=provider, error=str(exc))
    except Exception:
        r = _Unseen()
    scope.counted(r)


def note_retry() -> None:
    """An evaluator asking again on its own (a verdict that wasn't valid): counted as a retry."""
    scope = _current.get()
    if scope is not None:
        scope._retried(inner=True)


# ---------- the report ----------

@dataclass
class SampleResult:
    index: int
    id: Any
    status: str  # PASS | FAIL | INVALID | ERROR | TIMEOUT | RATE_LIMITED | NOT_RUN (map: DONE, or an error)
    result: Optional[Result] = None  # run(): the evaluate() Result
    value: Any = None  # map(): what the function returned
    error: Optional[str] = None
    error_kind: Optional[str] = None
    calls: int = 0
    retries: int = 0
    seconds: float = 0.0
    cost_usd: Optional[float] = None
    cached: bool = False  # the same as an earlier sample, judged once

    @property
    def kind(self) -> str:
        """pass | fail | done | invalid | rate_limited | timeout | unavailable | error | not_run"""
        if self.status in (PASS, FAIL, DONE, NOT_RUN):
            return self.status.lower()
        return self.error_kind or self.status.lower()


BUCKETS = (("invalid", "invalid judge output"), ("rate_limited", "rate limited"), ("timeout", "timed out"),
           ("unavailable", "service unavailable"), ("error", "evaluator error"), ("not_run", "not run"))


def _n(n: int, word: str) -> str:
    return f"{n:,} {word}{'' if n == 1 else 's'}"


def _duration(s: float) -> str:
    if s < 60:
        return f"{s:.1f}s"
    m, s = divmod(int(round(s)), 60)
    return f"{m}m{s:02d}s" if m < 60 else f"{m // 60}h{m % 60:02d}m"


@dataclass
class Report:
    results: List[SampleResult]
    seconds: float = 0.0
    llm_calls: int = 0
    retries: int = 0
    tokens: Dict[str, int] = field(default_factory=dict)
    cost_usd: Optional[float] = None  # priced calls only; see unpriced_calls
    unpriced_calls: int = 0
    unpriced_models: Dict[str, int] = field(default_factory=dict)
    repeated_calls: int = 0  # attempts that called the model more than once
    cached: int = 0
    stopped: Optional[str] = None  # why samples were left: max_time, budget, cancelled
    budget_usd: Optional[float] = None
    mode: str = "evaluate"

    @property
    def counts(self) -> Counter:
        return Counter(r.kind for r in self.results)

    @property
    def passed(self) -> int:
        return self.counts["pass"]

    @property
    def failed(self) -> int:
        return self.counts["fail"]

    @property
    def not_judged(self) -> List[SampleResult]:
        """Samples with no verdict: invalid, an evaluator or infrastructure error, not run."""
        return [r for r in self.results if r.kind not in ("pass", "fail", "done")]

    def to_dict(self) -> dict:
        return {"samples": len(self.results), "seconds": round(self.seconds, 3), "counts": dict(self.counts),
                "llm_calls": self.llm_calls, "retries": self.retries, "tokens": self.tokens,
                "cost_usd": None if self.cost_usd is None else round(self.cost_usd, 6),
                "unpriced_calls": self.unpriced_calls, "unpriced_models": self.unpriced_models,
                "repeated_calls": self.repeated_calls, "cached": self.cached, "stopped": self.stopped}

    def __str__(self) -> str:
        c = self.counts
        out = [f"{_n(len(self.results), 'sample')} in {_duration(self.seconds)}", ""]
        rows = []
        if self.mode == "evaluate":
            rows.append((c["pass"] + c["fail"], f"judged ({c['pass']:,} passed, {c['fail']:,} failed)"))
        else:
            rows.append((c["done"], "done"))
        for key, label in BUCKETS:
            if c[key]:
                rows.append((c[key], label + ("s" if key == "invalid" and c[key] != 1 else "")
                             + (f": {self.stopped}" if key == "not_run" and self.stopped else "")))
        w = max(len(f"{n:,}") for n, _ in rows)
        out += [f"{n:>{w},} {label}" for n, label in rows]
        if self.cached:
            out.append(f"{self.cached:>{w},} the same as an earlier sample, judged once")
        out.append("")
        t = self.tokens or {}
        tok = f"{t.get('input', 0):,} in, {t.get('output', 0):,} out" + \
              (f" ({t['cached']:,} cached)" if t.get("cached") else "")
        out += [f"LLM calls:      {self.llm_calls:,}", f"Retries:        {self.retries:,}", f"Tokens:         {tok}"]
        named = sorted(m for m in self.unpriced_models if m != UNSEEN)
        why = (f"no price for {', '.join(named)} (pass prices=, or set ASSAY_PRICES)" if named else "") + \
              ("; " if named and UNSEEN in self.unpriced_models else "") + \
              ("the judge's calls aren't seen (ask through assay_sdk.Judge, or turn on assay.instrument())"
               if UNSEEN in self.unpriced_models else "")
        if self.cost_usd is None:
            cost = f"unknown: {why}" if why else "$0.00"
        else:
            cost = f"${self.cost_usd:,.2f}" + (f", and {_n(self.unpriced_calls, 'call')} not priced: {why}"
                                               if self.unpriced_calls else "")
        out.append(f"Estimated cost: {cost}")
        if self.budget_usd is not None and self.unpriced_calls:
            out.append(f"The budget (${self.budget_usd:g}) counts priced calls only.")
        if self.repeated_calls:
            out.append(f"{_n(self.repeated_calls, 'attempt')} called the model more than once: a judge that asks "
                       f"twice, or a library retrying out of sight, doubles the cost.")
        return "\n".join(out)


# ---------- the runtime ----------

def _is_async(fn: Callable) -> bool:
    return inspect.iscoroutinefunction(fn) or inspect.iscoroutinefunction(getattr(fn, "__call__", None))


def _key(fn: Callable, s: Sample) -> str:
    text = json.dumps([getattr(fn, "__qualname__", repr(fn)), s.args, s.kwargs], default=repr, sort_keys=True)
    return hashlib.sha256(text.encode()).hexdigest()


class EvalRuntime:
    def __init__(self, concurrency: int = 10, retries: int = 3, timeout: Optional[float] = 60.0,
                 rate_limit: Optional[float] = None, retry_on: Iterable[int] = RETRY_ON,
                 retry_invalid: bool = True, max_time: Optional[float] = None, budget_usd: Optional[float] = None,
                 prices: Optional[dict] = None, backoff: float = 1.0, max_backoff: float = 60.0,
                 cache: bool = False):
        if concurrency < 1:
            raise ValueError("concurrency: at least 1")
        self.concurrency, self.retries, self.timeout = int(concurrency), int(retries), timeout
        self.rate_limit, self.retry_on, self.retry_invalid = rate_limit, set(retry_on), retry_invalid
        self.max_time, self.budget_usd, self.prices = max_time, budget_usd, _prices(prices)
        self.backoff, self.max_backoff, self.cache = backoff, max_backoff, cache
        self.report: Optional[Report] = None
        self._limiter = _Limiter(rate_limit)

    def retryable(self, kind: Optional[str], code: Optional[int]) -> bool:
        """An HTTP error by its status (retry_on); a timeout or a dropped connection, always."""
        if code is not None:
            return code in self.retry_on
        if kind == "rate_limited":
            return 429 in self.retry_on
        return kind in ("timeout", "unavailable")

    # --- entry points ---

    def run(self, judge: Callable, samples: Iterable, **options) -> Report:
        """Judge every sample; options are evaluate()'s: schema, threshold, score_range, field,
        evaluator, judge_kwargs, run (each sample's check is recorded as field:id on it)."""
        return self._sync(self.arun(judge, samples, **options))

    async def arun(self, judge: Callable, samples: Iterable, *, schema: Optional[dict] = None,
                   threshold: Optional[float] = 0.5, score_range: Tuple[float, float] = (0.0, 1.0),
                   run=None, field: Optional[str] = None, evaluator: Optional[str] = None,
                   judge_kwargs: Optional[Dict[str, Any]] = None) -> Report:
        from assay_sdk.llm import Judge
        is_judge = isinstance(judge, Judge)
        call = judge.ask if is_judge else judge
        extra = dict(judge_kwargs or {})
        if is_judge and schema is not None:
            extra.setdefault("schema", schema)  # the provider is asked for it, and the answer checked

        def begin():
            return Result(status=INVALID)

        def take(res: Result, out: Any) -> bool:
            return _step(res, out, schema, threshold, score_range)

        def finish(res: Result, s: Sample) -> None:
            target = s.run if s.run is not None else run
            name = field if s.run is not None else (f"{field or 'evaluation'}:{s.id}" if run is not None else field)
            _record(res, target, name, evaluator, s.inputs)
        return await self._drive(call, samples, extra, begin, take, finish, is_judge, "evaluate", True)

    def map(self, fn: Callable, items: Iterable, **kwargs) -> Report:
        """fn(item) for every item under the same limits and accounting; each result's value is what
        fn returned. Each item is fn's one argument (a Sample to say otherwise). fn does its own retrying; the runtime asks again only when it raised a timeout,
        a dropped connection or an HTTP error in retry_on without a call being seen."""
        return self._sync(self.amap(fn, items, **kwargs))

    async def amap(self, fn: Callable, items: Iterable, **kwargs) -> Report:
        def begin():
            return {"value": None, "status": DONE, "error": None, "error_kind": None}

        def take(res: dict, out: Any) -> bool:
            res.update(value=out, status=DONE, error=None, error_kind=None)
            return True
        return await self._drive(fn, items, kwargs, begin, take, lambda res, s: None, False, "map", False)

    # --- running ---

    def _sync(self, coro) -> Report:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            try:
                return asyncio.run(coro)
            except KeyboardInterrupt:
                if self.report is not None:  # what was judged before Ctrl-C
                    return self.report
                raise
        with ThreadPoolExecutor(1) as ex:  # called from a running loop (a notebook): run on its own
            return ex.submit(asyncio.run, coro).result()

    async def _drive(self, call, samples, extra, begin, take, finish, is_judge, mode, spread) -> Report:
        state = _State(self)
        pool = ThreadPoolExecutor(max_workers=self.concurrency + 4, thread_name_prefix="assay-eval")
        out: List[SampleResult] = []
        cache: Dict[str, "asyncio.Future"] = {}
        items = enumerate(samples)
        sync = not _is_async(call)

        async def one(i: int, s: Sample) -> SampleResult:
            key = _key(call, s) if self.cache else None
            if key is not None and key in cache:
                first: SampleResult = await asyncio.shield(cache[key])
                copy = dataclasses.replace(first, index=i, id=s.id, calls=0, retries=0, seconds=0.0,
                                           cost_usd=None, cached=True,
                                           result=dataclasses.replace(first.result) if first.result else None)
                if copy.result is not None:
                    finish(copy.result, s)
                return copy
            fut = asyncio.get_running_loop().create_future() if key is not None else None
            if fut is not None:
                cache[key] = fut
            try:
                r = await self._one(call, s, i, state, pool, sync, extra, begin, take, finish, is_judge)
            except BaseException:
                if fut is not None:
                    fut.cancel()
                raise
            if fut is not None:
                fut.set_result(r)
            return r

        async def worker():
            for i, x in items:  # shared: each worker takes the next sample
                s = _as_sample(x, i, spread)
                why = state.why_stop()
                if why:
                    out.append(SampleResult(i, s.id, NOT_RUN, error=why, error_kind="not_run"))
                    continue
                try:
                    out.append(await one(i, s))
                except asyncio.CancelledError:
                    out.append(SampleResult(i, s.id, NOT_RUN, error="the run was stopped", error_kind="not_run"))
                    raise

        tasks = [asyncio.ensure_future(worker()) for _ in range(self.concurrency)]
        try:
            await asyncio.wait(tasks)  # unlike gather, doesn't cancel the samples when this is cancelled
            for t in tasks:
                if t.exception() is not None:
                    raise t.exception()
        except asyncio.CancelledError:
            state.stopping, state.stopped = True, "the run was cancelled"
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            for i, x in items:
                out.append(SampleResult(i, getattr(x, "id", None) or i, NOT_RUN, error=state.stopped,
                                        error_kind="not_run"))
            self.report = self._report(state, out, mode)
            raise
        finally:
            pool.shutdown(wait=False)  # a sync judge past its timeout is left to finish on its own
        self.report = self._report(state, out, mode)
        return self.report

    async def _one(self, call, s: Sample, i: int, state: _State, pool, sync, extra, begin, take, finish,
                   is_judge) -> SampleResult:
        scope = _Scope(state)
        token = _current.set(scope)
        started = time.monotonic()
        res = begin()
        kwargs = {**s.kwargs, **extra}
        attempt, started_any = 0, False
        try:
            while True:
                scope._attempt()
                if isinstance(res, Result):
                    res.attempts = attempt + 1
                if not is_judge:  # a judge whose calls may be unseen: its attempt takes a slot
                    why = await scope.agate()
                    if why:
                        break
                    scope.prepaid = True
                started_any = True
                limit = scope.call_timeout() if not is_judge else state.remaining()
                exc, got = None, None
                try:
                    if sync:
                        ctx = contextvars.copy_context()
                        fut = asyncio.get_running_loop().run_in_executor(
                            pool, functools.partial(ctx.run, call, *s.args, **kwargs))
                    else:
                        fut = call(*s.args, **kwargs)
                    got = await asyncio.wait_for(fut, None if limit is None else max(limit, 0.001))
                    done = take(res, got)
                except asyncio.TimeoutError:
                    exc = TimeoutError(f"no answer within {limit:g}s" + (" (the run's max_time)" if
                                       self.timeout is None or (limit or 0) < self.timeout else ""))
                    done = self._failed(res, exc)
                except asyncio.CancelledError:
                    if state.stopping:
                        raise
                    # A library that gave up (often on a 429) by cancelling: this sample's failure, not the run's.
                    exc = ConnectionError("the judge was cancelled (CancelledError) though the run wasn't: "
                                          "often a client giving up on a rate limit or a timeout")
                    done = self._failed(res, exc)
                except Exception as e:
                    exc = e
                    done = self._failed(res, e)
                scope.end_attempt(got)
                if done:
                    break
                kind = res.error_kind if isinstance(res, Result) else res["error_kind"]
                resp_exc = getattr(got, "exception", None)
                if kind == "invalid":
                    again = self.retry_invalid
                elif scope.attempt_judge_calls:  # Judge asked again already, as far as it should
                    again = False
                else:
                    again = self.retryable(kind, _code(exc or resp_exc))
                wait = scope.delay(attempt, exc or resp_exc, kind) if again else None
                if wait is None:
                    break
                await asyncio.sleep(wait)
                scope._retried()
                attempt += 1
        finally:
            _current.reset(token)
        if not started_any:  # the run's time was up before its first call
            return SampleResult(i, s.id, NOT_RUN, error=state.why_stop() or "the run's time limit was reached",
                                error_kind="not_run")
        if isinstance(res, Result):
            finish(res, s)
            return SampleResult(i, s.id, res.status, result=res, error=res.error, error_kind=res.error_kind,
                                calls=scope.calls, retries=scope.retries, seconds=time.monotonic() - started,
                                cost_usd=scope.cost)
        return SampleResult(i, s.id, res["status"], value=res["value"], error=res["error"],
                            error_kind=res["error_kind"], calls=scope.calls, retries=scope.retries,
                            seconds=time.monotonic() - started, cost_usd=scope.cost)

    @staticmethod
    def _failed(res, exc: BaseException) -> bool:
        if isinstance(res, Result):
            return _failed(res, exc)
        from assay_sdk.evaluation import _TRANSIENT, classify_exception
        kind, text = classify_exception(exc)
        res.update(status="ERROR", error=text, error_kind=kind)
        return kind not in _TRANSIENT

    def _report(self, state: _State, out: List[SampleResult], mode: str) -> Report:
        out.sort(key=lambda r: r.index)
        return Report(results=out, seconds=time.monotonic() - state.started, llm_calls=state.calls,
                      retries=state.retries, tokens=dict(state.tokens),
                      cost_usd=state.cost if state.priced else None, unpriced_calls=state.unpriced,
                      unpriced_models=dict(state.unpriced_models), repeated_calls=state.repeated,
                      cached=sum(1 for r in out if r.cached), stopped=state.stopped, budget_usd=self.budget_usd,
                      mode=mode)
