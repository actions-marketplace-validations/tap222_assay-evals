"""Assay SDK: record what your AI system does, and how it went. Standard library only.

    import assay_sdk as assay

    assay.init("https://assay.example.com", key="ak_...")   # or ASSAY_URL / ASSAY_KEY
    assay.init()                                             # no server: record to .assay/events.jsonl

    with assay.run("refund_request", input=message, version={"prompt": "support@v5"}) as run:
        run.llm(model="claude-sonnet-5", tokens_in=620, tokens_out=180, cost_usd=0.0024)
        order = run.call("get_order", get_order, order_id="O-17")   # runs it, records result or error
        run.state("refund:O-17", "create", {"amount": 27.61})
        run.answer("Refunded $27.61.")

    assay.feedback(run.id, "thumbs_up")                       # later, from your UI
    assay.check("nightly-0924", "case-17", "pass", run_id=run.id, field="answer")   # from your tests

Events follow the Assay event schema v1 (docs/event-schema.md) and stream to
POST /v1/ingest in the background: a run that crashes still shows every step up
to that point. With no server configured they go to a local file instead, one
event per line, for `assay load` (or a later upload) to pick up. Every event has an id, so retries never duplicate anything. The
SDK never raises into your code (pass strict=True to init while developing).
"""
from __future__ import annotations

import atexit
import json
import logging
import os
import random
import threading
import time
import urllib.error
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional

__all__ = ["init", "run", "feedback", "check", "correction", "expect", "flush", "shutdown", "Run"]
__version__ = "0.2.0"

log = logging.getLogger("assay_sdk")
SCHEMA = 1


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _ts(t: Optional[datetime]) -> Optional[str]:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _json_safe(v):
    """Make a value JSON-serializable without failing: unknown objects become their repr."""
    try:
        json.dumps(v)
        return v
    except (TypeError, ValueError):
        if isinstance(v, dict):
            return {str(k): _json_safe(x) for k, x in v.items()}
        if isinstance(v, (list, tuple, set)):
            return [_json_safe(x) for x in v]
        if isinstance(v, datetime):
            return _ts(v)
        return repr(v)


class _Client:
    def __init__(self, url: str, key: Optional[str], tenant: Optional[str], redact: Optional[Callable],
                 sample: float, flush_interval: float, batch_size: int, max_queue: int, strict: bool,
                 transport: Optional[Callable[[List[dict]], None]], enabled: bool):
        self.url, self.key, self.tenant = url.rstrip("/"), key, tenant
        self.redact, self.sample, self.strict, self.enabled = redact, sample, strict, enabled
        self.batch_size, self.max_queue = batch_size, max_queue
        self._send = transport or self._http
        self._queue: List[dict] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = False
        self.dropped = 0
        self._thread = threading.Thread(target=self._loop, args=(flush_interval,), name="assay-evals", daemon=True)
        self._thread.start()

    def emit(self, event: dict) -> None:
        if not self.enabled:
            return
        event = {"v": SCHEMA, "id": uuid.uuid4().hex, "ts": _now(), **event}
        with self._lock:
            if len(self._queue) >= self.max_queue:
                self._queue.pop(0)
                self.dropped += 1
                if self.dropped in (1, 100, 10000):
                    log.warning("Assay: queue full, dropped %d events so far (is the server reachable?)", self.dropped)
            self._queue.append({k: v for k, v in event.items() if v is not None})
            full = len(self._queue) >= self.batch_size
        if full:
            self._wake.set()

    def clean(self, v):
        """Redact (if configured), then make JSON-safe."""
        if v is None:
            return None
        if self.redact:
            try:
                v = self.redact(v)
            except Exception:
                log.exception("Assay: redact() failed; dropping the value rather than sending it unredacted")
                return "<redaction failed>"
        return _json_safe(v)

    def flush(self) -> bool:
        """Send everything queued. True if the queue is empty afterwards."""
        while True:
            with self._lock:
                batch, self._queue = self._queue[:self.batch_size], self._queue[self.batch_size:]
            if not batch:
                return True
            try:
                self._send(batch)
            except Exception:
                with self._lock:  # keep them, in order, for the next try
                    self._queue = batch + self._queue
                if self.strict:
                    raise
                log.warning("Assay: couldn't send %d events; will retry", len(batch), exc_info=True)
                return False

    def _loop(self, interval: float) -> None:
        backoff = interval
        while not self._stop:
            self._wake.wait(backoff)
            self._wake.clear()
            ok = self.flush() if not self.strict else self._flush_quiet()
            backoff = interval if ok else min(backoff * 2, 60.0)

    def _flush_quiet(self) -> bool:
        try:
            return self.flush()
        except Exception:
            return False

    def close(self) -> None:
        self._stop = True
        self._wake.set()
        # Let a send in progress finish first: at exit the daemon thread is killed, and a batch it
        # has taken off the queue would be lost.
        if self._thread is not threading.current_thread():
            self._thread.join(timeout=10)
        self.flush()

    def _http(self, batch: List[dict]) -> None:
        headers = {"Content-Type": "application/json", "User-Agent": f"assay-evals-python/{__version__}"}
        if self.key:
            headers["Authorization"] = f"Bearer {self.key}"
        if self.tenant:
            headers["X-Tenant"] = self.tenant
        req = urllib.request.Request(self.url + "/v1/ingest", data=json.dumps({"events": batch}).encode(),
                                     headers=headers, method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=10) as r:
                    r.read()
                return
            except urllib.error.HTTPError as e:
                if e.code == 422:  # a malformed event won't improve with retries: log it and drop the batch
                    log.error("Assay rejected events: %s", e.read().decode()[:1000])
                    return
                if e.code < 500 and e.code != 429 or attempt == 2:
                    raise
            except OSError:
                if attempt == 2:
                    raise
            time.sleep(0.5 * 2 ** attempt)


def _file_transport(path: str) -> Callable[[List[dict]], None]:
    """Append each batch to a JSON Lines file, one event per line, in the body /v1/ingest takes.
    Each batch is one O_APPEND write, so processes recording to the same file (pytest -n) don't
    split each other's lines."""
    path = os.path.abspath(path)  # fixed now, so a later chdir can't scatter events
    lock = threading.Lock()

    def write(batch: List[dict]) -> None:
        data = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in batch).encode()
        with lock:
            folder = os.path.dirname(path)
            if not os.path.isdir(folder):
                os.makedirs(folder, exist_ok=True)
                with open(os.path.join(folder, ".gitignore"), "w") as f:  # recorded inputs stay out of git
                    f.write("*\n")
            fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
            try:
                while data:
                    data = data[os.write(fd, data):]
            finally:
                os.close(fd)
    return write


def _tool_names(tools: Optional[List[Any]]) -> Optional[List[str]]:
    """Names from what a model is given: "get_order", {"name": ...}, or {"function": {"name": ...}}."""
    if tools is None:
        return None
    out = []
    for t in tools:
        if isinstance(t, dict):
            t = t.get("name") or (t.get("function") or {}).get("name")
        out.append(str(t)[:128])
    return out


def _test(test: Any) -> Optional[Dict[str, Any]]:
    """test="case-17" or {"case": ...}; `assay test` fills in the run and the attempt."""
    if test is None:
        return None
    test = {"case": test} if isinstance(test, str) else dict(test)
    test.setdefault("run", os.environ.get("ASSAY_TEST_RUN") or "local")
    if "attempt" not in test and os.environ.get("ASSAY_TEST_ATTEMPT"):
        test["attempt"] = int(os.environ["ASSAY_TEST_ATTEMPT"])
    return test


_client: Optional[_Client] = None


def init(url: Optional[str] = None, key: Optional[str] = None, *, tenant: Optional[str] = None,
         redact: Optional[Callable[[Any], Any]] = None, sample: float = 1.0, flush_interval: float = 1.0,
         batch_size: int = 500, max_queue: int = 100_000, strict: bool = False,
         transport: Optional[Callable[[List[dict]], None]] = None, enabled: bool = True,
         path: Optional[str] = None) -> None:
    """Configure the SDK once, at startup.

    url / key      default to the ASSAY_URL / ASSAY_KEY environment variables
    path           with no url, where events are recorded instead: ASSAY_PATH, else .assay/events.jsonl
    redact         applied to inputs, arguments, results, text and outputs before they leave the process
    sample         share of runs to record (0.1 = one in ten); outcomes are always sent
    strict         raise send errors instead of logging them (for development)
    enabled        False turns the SDK into a no-op (e.g. in unit tests)
    """
    global _client
    if _client is not None:
        _client.close()
    url = url or os.environ.get("ASSAY_URL")
    if not url and transport is None:
        path = path or os.environ.get("ASSAY_PATH") or os.path.join(".assay", "events.jsonl")
        transport = _file_transport(path)
        log.info("Assay: no server set (ASSAY_URL); recording to %s", path)
    _client = _Client(url or "http://localhost", key or os.environ.get("ASSAY_KEY"), tenant, redact, sample,
                      flush_interval, batch_size, max_queue, strict, transport, enabled)


def _c() -> _Client:
    if _client is None:
        init()
    return _client


class Run:
    """One run of your system on one input. Use through `assay.run(...)`."""

    def __init__(self, client: _Client, run_id: str, recorded: bool):
        self.id, self._c, self._on = run_id, client, recorded
        self._seq, self._lock, self._parents = 0, threading.Lock(), []
        self.answer_text: Optional[str] = None
        self.test: Optional[Dict[str, Any]] = None  # {"run", "case", "attempt"} for a test-case run
        self.steps: List[Dict[str, Any]] = []  # what was recorded, in order: for assertions (assay_sdk.testing)
        self.expected: Optional[Dict[str, Any]] = None  # what run.expect() said
        self.outcome_value: Optional[str] = None  # run.outcome()
        self.started, self.ended = time.monotonic(), None  # for the run's latency
        self.expectations: List[Any] = []  # assay_sdk.testing.expect(run): checked when the test ends

    def _case(self) -> Dict[str, Any]:
        if not self.test:
            raise ValueError("This run isn't a test case: start it with assay.run(..., test=\"<case>\").")
        return self.test

    def expect(self, **kwargs) -> None:
        """assay.expect() for this run's test case: calls=, answer=, state=, allow_extra=, max_steps=."""
        expect(self._case()["case"], **kwargs)
        self.expected = kwargs

    def check(self, field: str, status: str, **kwargs) -> None:
        """assay.check() for this run's test case, e.g. run.check("total", "fail", expected=..., actual=...)."""
        t = self._case()
        check(t["run"], t["case"], status, attempt=t.get("attempt"), run_id=self.id, field=field, **kwargs)

    def _step(self, kind: str, started: Optional[datetime] = None, **fields) -> int:
        with self._lock:
            seq, self._seq = self._seq, self._seq + 1
            parent = self._parents[-1] if self._parents else None
        step = {"seq": seq, "kind": kind, "parent_seq": parent, "ts": _ts(started) or _now(), **fields}
        self.steps.append(step)
        if self._on:
            self._c.emit({"type": "step", "run_id": self.id, **step})
        return seq

    def llm(self, model: Optional[str] = None, tokens_in: Optional[int] = None, tokens_out: Optional[int] = None,
            cost_usd: Optional[float] = None, prompt: Optional[str] = None, text: Optional[str] = None,
            started: Optional[datetime] = None, ended: Optional[datetime] = None, error: Optional[str] = None,
            tools: Optional[List[Any]] = None) -> None:
        """A model call. prompt is "id@version"; text is the output (or a summary of it); tools are the
        tools the model was offered (names, or the tool definitions you passed the model)."""
        self._step("llm", started, model=model, tokens_in=tokens_in, tokens_out=tokens_out, cost_usd=cost_usd,
                   prompt=prompt, text=self._c.clean(text), ended_at=_ts(ended),
                   status="error" if error else "ok", error=error, tools=_tool_names(tools))

    def approval(self, action: str, decision: str = "approved", by: Optional[str] = None,
                 reason: Optional[str] = None) -> None:
        """A decision to allow an action, e.g. approval("refund", "approved", by="manager"). decision:
        approved, rejected or pending. Contracts (requires_approval) and expect(run) check it."""
        self._step("approval", name=action, decision=decision, by=by, text=self._c.clean(reason))

    def outcome(self, value: str) -> None:
        """Whether the run did what was asked: resolved, unresolved, or escalated (handed to a person)."""
        self.outcome_value = value

    def tool(self, name: str, args: Optional[Dict[str, Any]] = None, result: Any = None, error: Optional[str] = None,
             started: Optional[datetime] = None, ended: Optional[datetime] = None, server: Optional[str] = None) -> None:
        """A tool call you've already made. server: the MCP server it went to, if any."""
        self._step("tool", started, name=name, args=self._c.clean(args or {}), result=self._c.clean(result),
                   ended_at=_ts(ended), status="error" if error else "ok", error=error, server=server)

    def resource(self, uri: str, contents: Any = None, server: Optional[str] = None, error: Optional[str] = None,
                 started: Optional[datetime] = None, ended: Optional[datetime] = None) -> None:
        """An MCP resource the agent read, e.g. resource("file:///policies/refunds.md", text, server="docs").
        Its contents count as what the agent retrieved, for checking a judge's context."""
        self._step("resource", started, uri=uri, result=self._c.clean(contents), server=server,
                   ended_at=_ts(ended), status="error" if error else "ok", error=error)

    def plan(self, steps: List[Any], text: Optional[str] = None) -> None:
        """What the agent means to do, before doing it: the tools it will call, in order, each a name
        or {"tool": name, "args": {...}}. The run is checked against it (plan adherence). Call it
        again to replan: the new plan replaces what was left of the old one."""
        self._step("plan", plan=[s if isinstance(s, str) else {**s, "args": self._c.clean(s.get("args"))}
                                 if s.get("args") else s for s in steps], text=self._c.clean(text))

    def mcp_prompt(self, name: str, args: Optional[Dict[str, Any]] = None, messages: Any = None,
                   server: Optional[str] = None, error: Optional[str] = None) -> None:
        """An MCP prompt the agent fetched: its name, the arguments, and the messages it returned."""
        self._step("mcp_prompt", name=name, args=self._c.clean(args or {}), result=self._c.clean(messages),
                   server=server, status="error" if error else "ok", error=error)

    def call(self, name: str, fn: Callable, *positional, **args):
        """Call fn(*positional, **args), record it as a tool call (result or error, and timing), and
        return its result. Exceptions are recorded, then re-raised."""
        started = datetime.now(timezone.utc)
        try:
            out = fn(*positional, **args)
        except Exception as e:
            self.tool(name, args, error=f"{type(e).__name__}: {e}"[:2000], started=started,
                      ended=datetime.now(timezone.utc))
            raise
        self.tool(name, args, out, started=started, ended=datetime.now(timezone.utc))
        return out

    def state(self, obj: str, op: str = "update", value: Any = None) -> None:
        """A change to the world, e.g. state("order:17", "update", {"qty": 3}). op: create, update, delete."""
        self._step("state", name=obj, op=op, value=self._c.clean(value))

    def answer(self, text: str) -> None:
        self.answer_text = text
        self._step("answer", text=self._c.clean(text))

    @contextmanager
    def stage(self, name: str, prompt: Optional[str] = None):
        """A pipeline stage. Record what it produced in .outputs; llm/tool calls inside are nested under it.

            with run.stage("extract") as s:
                s.outputs.update(extract(text))
        """
        handle = _Stage()
        started = datetime.now(timezone.utc)
        with self._lock:
            seq, self._seq = self._seq, self._seq + 1
            self._parents.append(seq)
        error = None
        try:
            yield handle
        except Exception as e:
            error = f"{type(e).__name__}: {e}"[:2000]
            raise
        finally:
            with self._lock:
                self._parents.remove(seq)
            if self._on:
                self._c.emit({"type": "step", "run_id": self.id, "seq": seq, "kind": "stage", "name": name,
                              "ts": _ts(started), "ended_at": _now(), "status": "error" if error else "ok",
                              "error": error, "outputs": self._c.clean(handle.outputs) or None,
                              "did_work": handle.did_work, "prompt": prompt})


class _Stage:
    def __init__(self):
        self.outputs: Dict[str, Any] = {}
        self.did_work: Optional[bool] = True


@contextmanager
def run(task: Optional[str] = None, *, run_id: Optional[str] = None, kind: str = "agent", input: Any = None,
        input_ref: Optional[str] = None, version: Optional[Dict[str, str]] = None, segment: Optional[str] = None,
        test: Any = None, parent: Optional[Run] = None, tags: Optional[Dict[str, Any]] = None,
        conversation: Optional[str] = None, turn: Optional[int] = None):
    """Record one run. kind is "agent" (llm/tool/state/answer steps) or "pipeline" (stages).
    conversation="c-1", turn=2: this run is one turn of a conversation (turns from 0).
    test="case-17" (or {"run": "nightly-0924", "case": "case-17", "attempt": 0}) marks a test-case run;
    under `assay test`, the run and attempt are filled in.
    An exception inside the block ends the run as failed (and is re-raised)."""
    c = _c()
    rid = run_id or uuid.uuid4().hex
    recorded = c.sample >= 1 or random.random() < c.sample
    r = Run(c, rid, recorded)
    r.test = _test(test)
    if recorded:
        c.emit({"type": "run.start", "run_id": rid, "kind": kind, "task": task, "segment": segment,
                "input": c.clean(input), "input_ref": input_ref, "version": version, "test": r.test,
                "parent_run_id": parent.id if parent else None, "tags": tags, "conversation_id": conversation,
                "turn": turn})
    status, error = "completed", None
    try:
        yield r
    except GeneratorExit:
        # Not from inside a `with` block: the run was dropped without being exited, so how it ended
        # is unknown. Leave it open; the server marks it abandoned once it goes quiet.
        status = None
        raise
    except BaseException as e:
        status, error = "failed", f"{type(e).__name__}: {e}"[:2000]
        raise
    finally:
        r.ended = time.monotonic()
        if recorded and status:
            c.emit({"type": "run.end", "run_id": rid, "status": status, "error": error, "outcome": r.outcome_value})


def feedback(run_id: str, kind: str, note: Optional[str] = None) -> None:
    """What a user did: thumbs_up, thumbs_down, retry, escalation or complaint."""
    _c().emit({"type": "feedback", "run_id": run_id, "kind": kind, "note": note})


def check(test_run: Optional[str], case: str, status: str, *, attempt: Optional[int] = None, run_id: Optional[str] = None,
          field: Optional[str] = None, expected: Any = None, actual: Any = None, evaluator: Optional[str] = None,
          score: Optional[float] = None, reason: Optional[str] = None,
          version: Optional[Dict[str, str]] = None, inputs: Optional[Dict[str, Any]] = None) -> None:
    """One result from a test run: status pass, fail, or error (the check couldn't run). Send passes too.
    test_run None: the run `assay test` is doing (else "local").
    inputs: what the evaluator saw, e.g. {"query": ..., "output": ..., "context": ...}, so Assay can
    check a judge got the right data (the output really is the run's answer, and so on)."""
    s = lambda v: None if v is None else v if isinstance(v, str) else json.dumps(_json_safe(v))
    t = _test({"case": case, **({"run": test_run} if test_run else {}),
               **({"attempt": attempt} if attempt is not None else {})})
    _c().emit({"type": "check", "test": t,
               "status": status, "run_id": run_id, "field": field, "expected": s(expected), "actual": s(actual),
               "evaluator": evaluator, "score": score, "reason": reason, "version": version,
               "inputs": _c().clean(inputs)})


def correction(run_id: str, field: str, expected: Any = None, observed: Any = None, kind: str = "wrong",
               reporter: Optional[str] = None) -> None:
    """Someone found a wrong value: Assay traces it to the step it started at."""
    s = lambda v: None if v is None else str(v)
    _c().emit({"type": "correction", "run_id": run_id, "field": field, "expected": s(expected),
               "observed": s(observed), "kind": kind, "reporter": reporter})


def expect(case: str, *, calls: Optional[List[Dict[str, Any]]] = None, answer: Optional[str] = None,
           state: Optional[List[Dict[str, Any]]] = None, allow_extra: Optional[List[str]] = None,
           max_steps: Optional[int] = None, answer_match: str = "contains") -> None:
    """What a test case should do: the tool calls, the answer, and the end state."""
    _c().emit({"type": "expect", "case": case, "calls": calls or [], "answer": answer, "answer_match": answer_match,
               "state": state or [], "allow_extra": allow_extra or [], "max_steps": max_steps})


def flush() -> bool:
    """Send everything now (e.g. before a short-lived process exits). True if nothing is left."""
    return _c().flush() if _client else True


def shutdown() -> None:
    if _client:
        _client.close()


atexit.register(lambda: _client and _client.close())
