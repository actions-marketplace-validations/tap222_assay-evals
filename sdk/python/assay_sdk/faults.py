"""Breaking tools on purpose, to test how an agent handles it.

    with assay.faults(get_order="empty", search="error", calendar="timeout", pay="error:1"):
        reply = my_agent("Cancel O-17", run=assay_case)
    expect(assay_case).handles_failure()

For each named tool (recorded with @assay.tool or run.call), the fault replaces the call:
  "empty"      returns an empty result ([]), without calling the tool
  "error"      raises ToolFault, recorded as the tool's error
  "timeout"    raises TimeoutError
  "error:N"    fails the first N calls, then calls the tool for real: does the agent retry?
  {"return": value}   returns value instead, e.g. {"return": {"status": "unknown"}}
Recorded steps say which fault they were (fault="error"), so evaluation knows it was injected.
"""
from __future__ import annotations

import contextvars
from contextlib import contextmanager
from typing import Any, Callable, Dict, Optional, Tuple


class ToolFault(Exception):
    """An error injected by assay.faults()."""


_active: "contextvars.ContextVar[Optional[dict]]" = contextvars.ContextVar("assay_faults", default=None)


@contextmanager
def faults(**spec):
    for name, v in spec.items():
        ok = v in ("empty", "error", "timeout") or (isinstance(v, str) and v.startswith("error:")
                                                    and v[6:].isdigit()) or (isinstance(v, dict) and "return" in v)
        if not ok:
            raise ValueError(f"faults({name}=...): empty, error, timeout, error:N, or {{'return': value}}")
    state = {"spec": dict(spec), "calls": {}}
    token = _active.set(state)
    try:
        yield state
    finally:
        _active.reset(token)


def call(name: str, fn: Callable, args: tuple, kwargs: dict) -> Tuple[Any, Optional[str]]:
    """(result, the fault applied or None). Raises what the fault raises."""
    state = _active.get()
    spec = state["spec"].get(name) if state else None
    if spec is None:
        return fn(*args, **kwargs), None
    n = state["calls"][name] = state["calls"].get(name, 0) + 1
    if isinstance(spec, dict):
        return spec["return"], "return"
    if spec == "empty":
        return [], "empty"
    if spec == "timeout":
        raise _tag(TimeoutError(f"{name} timed out (injected by assay.faults)"), "timeout")
    if spec == "error" or (spec.startswith("error:") and n <= int(spec[6:])):
        raise _tag(ToolFault(f"{name} failed (injected by assay.faults)"), "error")
    return fn(*args, **kwargs), None


def _tag(exc: BaseException, kind: str) -> BaseException:
    exc.assay_fault = kind
    return exc


def of(exc: Optional[BaseException]) -> Optional[str]:
    return getattr(exc, "assay_fault", None) if exc is not None else None
