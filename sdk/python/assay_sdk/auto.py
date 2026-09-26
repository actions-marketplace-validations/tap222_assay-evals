"""Attach Assay to code with a few lines, not a rewrite.

    import assay_sdk as assay
    assay.init()
    assay.instrument()                       # Anthropic and OpenAI calls are recorded

    @assay.step("classification")            # a step of the pipeline
    def classify(doc): ...

    @assay.step("process", id_from="document_id")   # the outermost step starts the run
    def process(document_id, pages): ...

    @assay.tool                              # a tool an agent calls
    def get_order(order_id): ...

    @assay.pipeline("invoice", id_from="document_id")   # one run of the pipeline, around the entry
    def handle(document_id, pdf): return graph.invoke(...)

A step records itself as a stage of the run in progress; the outermost decorated call starts a
run of its own when there is none (kind "pipeline", named after the step; `id_from` names the
argument whose value is the run's id, e.g. the document id). A step's return value is kept as
its outputs when it's a dict (or a model with .model_dump()), so checks on fields can find
the step that produced them. A tool records its arguments, its result or error, and its timing,
into the run in progress; with no run, the function just runs.

instrument() records every model call made inside a run, as a model call of the step it's in:
model, tokens (in, out, cached, reasoning), the tools offered and the tool calls asked for, the
answer's text, why it stopped, and errors, the same way for every provider. It never changes
what the call returns, and never breaks it: anything the recording can't read is left out.
Works on sync and async functions.
"""
from __future__ import annotations

import contextvars
import functools
import inspect
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, List, Optional

log = logging.getLogger("assay_sdk")
_stage: "contextvars.ContextVar[Optional[str]]" = contextvars.ContextVar("assay_stage", default=None)
MAX_TEXT = 2000


def _sdk():
    import assay_sdk
    return assay_sdk


def _arguments(fn: Callable, args: tuple, kwargs: dict) -> dict:
    try:
        bound = inspect.signature(fn).bind_partial(*args, **kwargs)
    except (TypeError, ValueError):
        return {f"arg{i}": a for i, a in enumerate(args)} | kwargs
    return {k: v for k, v in bound.arguments.items() if k not in ("self", "cls")}


def _outputs(handle, out: Any) -> None:
    if hasattr(out, "model_dump"):
        try:
            out = out.model_dump()
        except Exception:
            return
    if isinstance(out, dict):
        handle.outputs.update({str(k): (v[:MAX_TEXT] if isinstance(v, str) else v) for k, v in out.items()})
    elif isinstance(out, (str, int, float, bool)):
        handle.outputs["result"] = out[:MAX_TEXT] if isinstance(out, str) else out


def step(name: Any = None, *, id_from: Optional[str] = None, task: Optional[str] = None):
    """@assay.step or @assay.step("name"): record the function as a step of the pipeline."""
    def deco(fn: Callable) -> Callable:
        label = name if isinstance(name, str) else fn.__name__

        def run_for(args, kwargs):
            a = _sdk()
            rid = None
            if id_from:
                v = _arguments(fn, args, kwargs).get(id_from)
                rid = None if v is None else str(v)[:128]
            return a.run(task or label, run_id=rid or uuid.uuid4().hex, kind="pipeline")

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                a = _sdk()
                async def inside(run):
                    token = _stage.set(label)
                    try:
                        with run.stage(label) as s:
                            out = await fn(*args, **kwargs)
                            _outputs(s, out)
                            return out
                    finally:
                        _stage.reset(token)
                run = a.current()
                if run is not None:
                    return await inside(run)
                with run_for(args, kwargs) as run:
                    return await inside(run)
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            a = _sdk()
            def inside(run):
                token = _stage.set(label)
                try:
                    with run.stage(label) as s:
                        out = fn(*args, **kwargs)
                        _outputs(s, out)
                        return out
                finally:
                    _stage.reset(token)
            run = a.current()
            if run is not None:
                return inside(run)
            with run_for(args, kwargs) as run:
                return inside(run)
        return wrapper
    return deco(name) if callable(name) else deco


def tool(name: Any = None, *, server: Optional[str] = None):
    """@assay.tool or @assay.tool("name"): record each call of the function as a tool call."""
    def deco(fn: Callable) -> Callable:
        label = name if isinstance(name, str) else fn.__name__

        def record(run, args, kwargs, started, out=None, exc=None):
            try:
                run.tool(label, _arguments(fn, args, kwargs), None if exc else out,
                         error=f"{type(exc).__name__}: {exc}"[:2000] if exc else None, started=started,
                         ended=datetime.now(timezone.utc), server=server)
            except Exception:  # recording must never break the tool
                log.debug("Assay couldn't record tool %s", label, exc_info=True)

        if inspect.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def awrapper(*args, **kwargs):
                run = _sdk().current()
                if run is None:
                    return await fn(*args, **kwargs)
                started = datetime.now(timezone.utc)
                try:
                    out = await fn(*args, **kwargs)
                except Exception as exc:
                    record(run, args, kwargs, started, exc=exc)
                    raise
                record(run, args, kwargs, started, out)
                return out
            return awrapper

        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            run = _sdk().current()
            if run is None:
                return fn(*args, **kwargs)
            started = datetime.now(timezone.utc)
            try:
                out = fn(*args, **kwargs)
            except Exception as exc:
                record(run, args, kwargs, started, exc=exc)
                raise
            record(run, args, kwargs, started, out)
            return out
        return wrapper
    return deco(name) if callable(name) else deco


def _runs(kind: str):
    def maker(task: Any = None, *, id_from: Optional[str] = None):
        """@assay.pipeline / @assay.agent: each call of the function is one run; the steps, tools and
        model calls inside it are recorded into it. Inside a run already, it just runs."""
        def deco(fn: Callable) -> Callable:
            label = task if isinstance(task, str) else fn.__name__

            def opened(args, kwargs):
                rid = None
                if id_from:
                    v = _arguments(fn, args, kwargs).get(id_from)
                    rid = None if v is None else str(v)[:128]
                return _sdk().run(label, run_id=rid or uuid.uuid4().hex, kind=kind)

            if inspect.iscoroutinefunction(fn):
                @functools.wraps(fn)
                async def awrapper(*args, **kwargs):
                    if _sdk().current() is not None:
                        return await fn(*args, **kwargs)
                    with opened(args, kwargs) as run:
                        out = await fn(*args, **kwargs)
                        if kind == "agent" and isinstance(out, str):
                            run.answer(out)
                        return out
                return awrapper

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                if _sdk().current() is not None:
                    return fn(*args, **kwargs)
                with opened(args, kwargs) as run:
                    out = fn(*args, **kwargs)
                    if kind == "agent" and isinstance(out, str):
                        run.answer(out)
                    return out
            return wrapper
        return deco(task) if callable(task) else deco
    return maker


pipeline = _runs("pipeline")
agent = _runs("agent")


# ---------- model calls ----------

def _reading(provider: str):
    """How to read one provider's response into a model-call step (assay_sdk/llm.py normalizes it)."""
    def read(kwargs: dict, resp: Any) -> dict:
        from assay_sdk.llm import normalize
        r = normalize(resp, provider)
        offered = []
        for t in kwargs.get("tools") or []:
            if isinstance(t, dict):
                offered.append(t.get("name") or (t.get("function") or {}).get("name"))
        return {"model": r.model or kwargs.get("model"), "tokens_in": r.usage.get("input"),
                "tokens_out": r.usage.get("output"), "tokens_cached": r.usage.get("cached"),
                "tokens_reasoning": r.usage.get("reasoning"), "text": (r.text or "")[:MAX_TEXT] or None,
                "tools": [t for t in offered if t] or None, "finish_reason": r.finish_reason,
                "tool_calls": [{**c, "arguments": c["arguments"]} for c in r.tool_calls] or None}
    read.provider = provider
    return read


def _observe(reading: Callable, kwargs: dict, resp: Any = None, exc: Optional[BaseException] = None) -> None:
    """Count the call for the sample an EvalRuntime is judging, if it is judging one."""
    if kwargs.get("stream"):
        return
    try:
        from assay_sdk import runtime
        runtime.observe(getattr(reading, "provider", None), resp, exc)
    except Exception:  # counting must never break the call
        log.debug("Assay couldn't count a model call", exc_info=True)


def _record_call(reading: Callable, kwargs: dict, started: datetime, resp: Any = None,
                 exc: Optional[BaseException] = None) -> None:
    run = _sdk().current()
    if run is None or kwargs.get("stream"):  # a stream is read by the caller; there's nothing whole to record
        return
    try:
        fields = reading(kwargs, resp) if exc is None else {"model": kwargs.get("model"), "finish_reason": "error"}
        run.llm(**fields, started=started, ended=datetime.now(timezone.utc), name=_stage.get(),
                error=f"{type(exc).__name__}: {exc}"[:2000] if exc else None)
    except Exception:  # recording must never break the call
        log.debug("Assay couldn't record a model call", exc_info=True)


def _wrap(original: Callable, reading: Callable, bound: bool) -> Callable:
    if inspect.iscoroutinefunction(original):
        @functools.wraps(original)
        async def patched(*args, **kwargs):
            started = datetime.now(timezone.utc)
            try:
                resp = await original(*args, **kwargs)
            except Exception as exc:
                _observe(reading, kwargs, exc=exc)
                _record_call(reading, kwargs, started, exc=exc)
                raise
            _observe(reading, kwargs, resp)
            _record_call(reading, kwargs, started, resp)
            return resp
    else:
        @functools.wraps(original)
        def patched(*args, **kwargs):
            started = datetime.now(timezone.utc)
            try:
                resp = original(*args, **kwargs)
            except Exception as exc:
                _observe(reading, kwargs, exc=exc)
                _record_call(reading, kwargs, started, exc=exc)
                raise
            _observe(reading, kwargs, resp)
            _record_call(reading, kwargs, started, resp)
            return resp
    patched._assay = True
    return patched


def _patch(owner, name: str, reading: Callable) -> bool:
    original = getattr(owner, name, None)
    if original is None or getattr(original, "_assay", False):
        return False
    setattr(owner, name, _wrap(original, reading, bound=not isinstance(owner, type)))
    return True


TARGETS = [  # (module, class or None for a module function, method, provider)
    ("anthropic.resources.messages", "Messages", "create", "anthropic"),
    ("anthropic.resources.messages", "AsyncMessages", "create", "anthropic"),
    ("openai.resources.chat.completions", "Completions", "create", "openai"),
    ("openai.resources.chat.completions", "AsyncCompletions", "create", "openai"),
    ("openai.resources.responses", "Responses", "create", "openai"),
    ("openai.resources.responses", "AsyncResponses", "create", "openai"),
    ("google.genai.models", "Models", "generate_content", "gemini"),
    ("google.genai.models", "AsyncModels", "generate_content", "gemini"),
    ("ollama", "Client", "chat", "ollama"),
    ("ollama", "AsyncClient", "chat", "ollama"),
    ("ollama", None, "chat", "ollama"),  # ollama.chat(...), bound to its default client
    ("litellm", None, "completion", "openai"),
    ("litellm", None, "acompletion", "openai"),
]


def instrument() -> List[str]:
    """Record the model calls the installed SDKs make: Anthropic, OpenAI (chat and responses),
    Gemini (google-genai), Ollama and LiteLLM. Each is recorded the same way (assay_sdk/llm.py):
    text, usage, the tool calls the model asked for, and why it stopped. Returns what was
    instrumented (e.g. ["anthropic Messages.create"]); calling it twice changes nothing."""
    import importlib
    done = []
    for module, cls_name, method, provider in TARGETS:
        try:
            mod = importlib.import_module(module)
        except ImportError:
            continue
        owner = mod if cls_name is None else getattr(mod, cls_name, None)
        if owner is not None and _patch(owner, method, _reading(provider)):
            done.append(f"{module.split('.')[0]} {cls_name + '.' if cls_name else ''}{method}")
    return done
