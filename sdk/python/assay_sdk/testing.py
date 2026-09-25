"""Assertions on what an agent run did, for pytest (or any test runner).

    from assay_sdk.testing import assert_called, assert_not_called, assert_max_steps

    def test_refund(assay_case):
        reply = my_agent("Refund O-17", run=assay_case)
        assert_called(assay_case, "get_order", order_id="O-17")
        assert_not_called(assay_case, "delete_order")
        assert_max_steps(assay_case, 6)

Each reads the steps the run recorded, and fails with what actually happened.
"""
from __future__ import annotations

import json
from typing import Any, List

__all__ = ["tool_calls", "assert_called", "assert_not_called", "assert_called_before", "assert_max_steps",
           "assert_answer_contains", "assert_no_pii"]


def tool_calls(run) -> List[dict]:
    """The run's tool calls, in order: [{"name", "args", "result", "error", "seq"}]."""
    return [s for s in run.steps if s["kind"] == "tool"]


def _call(c: dict) -> str:
    args = ", ".join(f"{k}={v!r}" for k, v in (c.get("args") or {}).items())
    return f"{c['name']}({args}){' → error' if c.get('error') else ''}"


def _calls_text(run) -> str:
    calls = tool_calls(run)
    return "; ".join(_call(c) for c in calls) if calls else "no tool calls"


def _matches(c: dict, args: dict) -> bool:
    got = c.get("args") or {}
    return all(k in got and got[k] == v for k, v in args.items())


def assert_called(run, tool: str, **args: Any) -> dict:
    """`tool` was called, with at least these arguments. Returns the first matching call."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    for c in tool_calls(run):
        if c["name"] == tool and _matches(c, args):
            return c
    want = f"{tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})"
    same = [c for c in tool_calls(run) if c["name"] == tool]
    raise AssertionError(f"Expected a call to {want}; "
                         + (f"{tool} was called with {'; '.join(_call(c) for c in same)}" if same else
                            f"{tool} was never called") + f". The run made: {_calls_text(run)}.")


def assert_not_called(run, tool: str, **args: Any) -> None:
    """`tool` wasn't called (with these arguments, if any are given)."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    hits = [c for c in tool_calls(run) if c["name"] == tool and _matches(c, args)]
    if hits:
        raise AssertionError(f"{tool} shouldn't have been called, but was: {'; '.join(_call(c) for c in hits)} "
                             f"(step {hits[0]['seq']}).")


def assert_called_before(run, first: str, then: str) -> None:
    """Every call to `then` comes after a call to `first`, e.g. get_order before refund."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    seen = False
    for c in tool_calls(run):
        seen = seen or c["name"] == first
        if c["name"] == then and not seen:
            raise AssertionError(f"{then} was called at step {c['seq']} before any {first}. "
                                 f"The run made: {_calls_text(run)}.")


def assert_max_steps(run, n: int) -> None:
    """The run took at most `n` steps (model calls, tool calls, state changes and the answer)."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    if len(run.steps) > n:
        kinds = ", ".join(f"{s['kind']}{' ' + s['name'] if s.get('name') else ''}" for s in run.steps)
        raise AssertionError(f"Took {len(run.steps)} steps; at most {n} expected: {kinds}.")


def assert_answer_contains(run, text: str) -> None:
    """The run's answer contains `text` (ignoring case)."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    answer = run.answer_text
    if answer is None:
        raise AssertionError(f"The run gave no answer (expected one containing {text!r}).")
    if text.lower() not in answer.lower():
        raise AssertionError(f"The answer doesn't contain {text!r}: {answer!r}.")


def assert_no_pii(run, allow: Any = ()) -> None:
    """No personal data (email, card, IBAN, SSN, phone) in any tool's arguments, except kinds in
    `allow`. Uses the same scanner as `assay test`, so assay-server must be installed."""
    __tracebackhide__ = True  # pytest: show the test line, not this helper
    try:
        from assay.learn import pii_scan
    except ImportError:
        raise ImportError("assert_no_pii uses Assay's PII scanner: pip install assay-server") from None
    found = []
    for c in tool_calls(run):
        for hit in pii_scan(json.dumps(c.get("args") or {}, default=str)):
            if hit["kind"] not in allow:
                found.append(f"{hit['kind']} ({hit['sample']}) sent to {c['name']} (step {c['seq']})")
    if found:
        raise AssertionError("Personal data in tool arguments: " + "; ".join(found) + ".")
