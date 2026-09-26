"""Run your own evaluator (an LLM judge, a metric) and get a result whose validity is explicit.

    from assay_sdk import evaluate

    result = evaluate(my_judge, question, answer, schema=VERDICT, threshold=0.7, run=run, field="helpful")
    result.status            # PASS, FAIL, INVALID, ERROR, TIMEOUT or RATE_LIMITED
    result.score, result.reason, result.error, result.attempts, result.raw_judge_output
    result.category          # the kind of failure, when the judge names one ("category" in its verdict)
    result.judge_model       # which model judged: from its Response, or judge_model=

judge_model and judge_prompt ("rubric@3") say which judge this was; a Judge's answer gives its
model on its own. They're recorded with the check, so a result judged by another model or
prompt isn't compared with its baseline as if only the AI had changed.

A judge that answered but not with a verdict is INVALID, not a score of 0: an unparseable
answer, one that doesn't fit `schema`, a score that is None, NaN, infinite or outside
`score_range`. INVALID, TIMEOUT, RATE_LIMITED and an unavailable service (connection error,
5xx) are tried again, `retries` times, with a growing pause; any other exception is an ERROR
at once (a bug isn't fixed by asking again). With `run=` (a test case's run), the result is
recorded as a check: PASS and FAIL as pass and fail, the rest as an error with its kind, so it
is never counted against the AI.

The judge is called as judge(*args, **kwargs, **judge_kwargs). evaluate()'s own keywords
(schema, threshold, run, field, ...) are its own: a judge that takes one of those names gets it
only through judge_kwargs, and evaluate() warns when that looks like what was meant. It may return:
  - a bool: whether it passed;
  - a number: the score, compared with `threshold`;
  - a dict: {"score": ..., "passed" or "pass": ..., "reason": ...} (also as JSON text);
  - an object with those as attributes (e.g. a pydantic model);
  - an assay_sdk.Response (from Judge.ask or normalize()): its error keeps its kind (TIMEOUT,
    RATE_LIMITED, INVALID, ...), and its structured answer or text is the verdict.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import math
import re
import time
import warnings
from dataclasses import dataclass, field as dc_field
from typing import Any, Callable, Dict, Optional, Tuple

PASS, FAIL, INVALID, ERROR, TIMEOUT, RATE_LIMITED = "PASS", "FAIL", "INVALID", "ERROR", "TIMEOUT", "RATE_LIMITED"
_TRANSIENT = ("invalid", "timeout", "rate_limited", "unavailable")  # worth asking again


@dataclass
class Result:
    status: str  # PASS | FAIL | INVALID | ERROR | TIMEOUT | RATE_LIMITED
    score: Optional[float] = None  # only for PASS and FAIL: an invalid result has no score
    reason: Optional[str] = None  # the judge's explanation
    error: Optional[str] = None  # why it couldn't be judged
    error_kind: Optional[str] = None  # invalid | timeout | rate_limited | unavailable | error
    attempts: int = 0  # how many times the judge was called
    raw_judge_output: Any = None  # what it returned last, as it returned it
    history: list = dc_field(default_factory=list)  # (status, error) of each attempt
    category: Optional[str] = None  # the judge's name for the kind of failure (grounding, policy_refusal, ...)
    judge_model: Optional[str] = None  # the model that judged
    judge_prompt: Optional[str] = None  # the judge's prompt or rubric, id@version

    @property
    def valid(self) -> bool:
        """It's a verdict: PASS or FAIL. Anything else says nothing about what was judged."""
        return self.status in (PASS, FAIL)

    @property
    def passed(self) -> Optional[bool]:
        return None if not self.valid else self.status == PASS


# ---------- telling an evaluator failure from a verdict ----------

def classify_exception(exc: BaseException) -> Tuple[str, str]:
    """(error_kind, reason) for an evaluator that raised, from its type, its status code, or its
    message: timeout, rate_limited, unavailable (connection, 5xx) or error."""
    name = type(exc).__name__
    code = getattr(exc, "status_code", None) or getattr(getattr(exc, "response", None), "status_code", None)
    text = f"{name}: {exc}"[:500]
    if isinstance(exc, (TimeoutError, asyncio.TimeoutError)) or "Timeout" in name:
        return "timeout", text
    if code == 429 or "RateLimit" in name:
        return "rate_limited", text
    if (isinstance(code, int) and (code >= 500 or code == 529)) or isinstance(exc, ConnectionError) \
            or "Connection" in name or "Unavailable" in name or "Overloaded" in name:
        return "unavailable", text
    msg = str(exc).lower()
    if re.search(r"\b429\b|rate.?limit|too many requests", msg):
        return "rate_limited", text
    if re.search(r"timed? ?out|timeout", msg):
        return "timeout", text
    if re.search(r"\b5\d\d\b|connection|unavailable|overloaded", msg):
        return "unavailable", text
    return "error", text


def _check_schema(value: Any, schema: dict, path: str = "") -> Optional[str]:
    """The first way `value` doesn't fit a JSON Schema subset (type, properties, required, enum,
    minimum, maximum, items), or None."""
    where = path or "the verdict"
    t = schema.get("type")
    types = {"object": dict, "array": list, "string": str, "boolean": bool, "null": type(None)}
    if t == "number":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return f"{where} isn't a number"
    elif t == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            return f"{where} isn't a whole number"
    elif t in types and not isinstance(value, types[t]):
        return f"{where} isn't a {t}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{where} is {value!r}, not one of {schema['enum']}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{where} is {value}, below {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{where} is {value}, above {schema['maximum']}"
    if isinstance(value, dict):
        for k in schema.get("required", []):
            if k not in value:
                return f"{path + '.' if path else ''}{k} is missing"
        for k, sub in (schema.get("properties") or {}).items():
            if k in value:
                p = _check_schema(value[k], sub, f"{path + '.' if path else ''}{k}")
                if p:
                    return p
    if isinstance(value, list) and schema.get("items"):
        for i, x in enumerate(value):
            p = _check_schema(x, schema["items"], f"{where}[{i}]")
            if p:
                return p
    return None


def category_of(out: Any) -> Optional[str]:
    """The kind of failure a verdict names: category (or failure_category, failure_mode)."""
    try:
        v = _as_dict(out)
    except ValueError:
        return None
    if not isinstance(v, dict):
        return None
    return next((v[k].strip() for k in ("category", "failure_category", "failure_mode")
                 if isinstance(v.get(k), str) and v[k].strip()), None)


def _as_dict(out: Any) -> Any:
    if isinstance(out, str):
        text = out.strip()
        fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)  # a judge that wrapped its JSON
        return json.loads(fenced.group(1) if fenced else text)
    if hasattr(out, "model_dump"):
        return out.model_dump()
    if hasattr(out, "__dict__") and not isinstance(out, (dict, list, bool, int, float)):
        return {k: v for k, v in vars(out).items() if not k.startswith("_")}
    return out


def verdict_of(out: Any, schema: Optional[dict], threshold: Optional[float],
               score_range: Tuple[float, float]) -> Tuple[str, Optional[float], Optional[str], Optional[str]]:
    """(status, score, reason, problem) for what a judge returned; status INVALID when it isn't a verdict."""
    if out is None or (isinstance(out, str) and not out.strip()):
        return INVALID, None, None, "the judge returned nothing"
    try:
        v = _as_dict(out)
    except ValueError:
        return INVALID, None, None, "the judge's answer wasn't valid JSON"
    if schema:
        p = _check_schema(v, schema)
        if p:
            return INVALID, None, None, f"the judge's verdict doesn't fit its schema: {p}"
    if isinstance(v, bool):
        return (PASS if v else FAIL), None, None, None
    reason, passed, score = None, None, v
    if isinstance(v, dict):
        reason = v.get("reason") or v.get("explanation")
        passed = next((v[k] for k in ("passed", "pass", "verdict") if isinstance(v.get(k), bool)), None)
        score = v.get("score")
        if passed is None and score is None:
            return INVALID, None, reason, "the verdict has neither a score nor passed/pass"
    if score is not None:
        if isinstance(score, bool) or not isinstance(score, (int, float)):
            return INVALID, None, reason, f"the score is {score!r}, not a number"
        if math.isnan(score) or math.isinf(score):
            return INVALID, None, reason, f"the score is {score}"  # e.g. a metric that divided by zero
        lo, hi = score_range
        if not lo <= score <= hi:
            return INVALID, None, reason, f"the score is {score}, outside {lo}-{hi}"
    if passed is None:
        if threshold is None:
            return INVALID, score, reason, "a score but no threshold to decide pass or fail"
        passed = score >= threshold
    return (PASS if passed else FAIL), (None if score is None else float(score)), reason, None


# ---------- evaluate ----------

def _record(result: Result, run, field: Optional[str], evaluator: Optional[str], inputs: Optional[dict]) -> None:
    if run is None:
        return
    status = {PASS: "pass", FAIL: "fail"}.get(result.status, "error")
    raw = result.raw_judge_output
    raw = raw if raw is None or isinstance(raw, str) else json.dumps(raw, default=str)
    run.check(field or "evaluation", status, score=result.score if result.valid else None,
              reason=result.reason if result.valid else result.error, evaluator=evaluator, inputs=inputs,
              error_kind=None if result.valid else result.error_kind, tries=result.attempts, raw_output=raw,
              **({"category": result.category} if result.category else {}),
              **{k: getattr(result, k) for k in ("judge_model", "judge_prompt") if getattr(result, k)})


def _step(result: Result, out: Any, schema, threshold, score_range) -> bool:
    """Take one answer; True if it's final."""
    from assay_sdk.llm import Response
    if isinstance(out, Response):  # a provider's answer, already read: its error keeps its kind
        result.judge_model = result.judge_model or out.model
        if out.error:
            status = {"timeout": TIMEOUT, "rate_limited": RATE_LIMITED, "invalid": INVALID}.get(out.error_kind, ERROR)
            result.status, result.score, result.error, result.error_kind = status, None, out.error, out.error_kind or "error"
            result.raw_judge_output, result.history = out.text, result.history + [(status, out.error)]
            return (out.error_kind or "error") not in _TRANSIENT
        out = out.structured if out.structured is not None else out.text
    status, score, reason, problem = verdict_of(out, schema, threshold, score_range)
    result.category = category_of(out) if status == FAIL else None
    result.raw_judge_output, result.history = out, result.history + [(status, problem)]
    if status == INVALID:
        result.status, result.score, result.reason, result.error, result.error_kind = INVALID, None, reason, problem, "invalid"
        return False
    result.status, result.score, result.reason, result.error, result.error_kind = status, score, reason, None, None
    return True


def _failed(result: Result, exc: BaseException) -> bool:
    """Take one exception; True if it's final (not worth asking again)."""
    kind, text = classify_exception(exc)
    result.status = {"timeout": TIMEOUT, "rate_limited": RATE_LIMITED}.get(kind, ERROR)
    result.score, result.error, result.error_kind = None, text, kind
    result.history = result.history + [(result.status, text)]
    return kind not in _TRANSIENT


def model_of(judge) -> Optional[str]:
    """The model an assay_sdk.Judge (or its bound ask) judges with; None for a function of your own."""
    from assay_sdk.llm import Judge
    for j in (judge, getattr(judge, "__self__", None)):
        if isinstance(j, Judge):
            return j.model
    return None


OWN = ("schema", "threshold", "score_range", "retries", "backoff", "run", "field", "evaluator", "inputs",
       "judge_model", "judge_prompt")


def _collisions(judge: Callable, given: Dict[str, Any]) -> None:
    """Warn when a keyword evaluate() keeps for itself is also one the judge takes."""
    try:
        params = inspect.signature(judge).parameters
    except (TypeError, ValueError):
        return
    clash = [k for k in OWN if k in params and given.get(k) is not None]
    if clash:
        warnings.warn(f"{', '.join(clash)} went to evaluate(), not to {getattr(judge, '__name__', 'the judge')}, "
                      f"which also takes {'it' if len(clash) == 1 else 'them'}. To pass {'it' if len(clash) == 1 else 'them'} "
                      f"to the judge, use judge_kwargs={{...}}.", stacklevel=3)


def evaluate(judge: Callable, *args, schema: Optional[dict] = None, threshold: Optional[float] = 0.5,
             score_range: Tuple[float, float] = (0.0, 1.0), retries: int = 2, backoff: float = 1.0,
             run=None, field: Optional[str] = None, evaluator: Optional[str] = None,
             inputs: Optional[Dict[str, Any]] = None, judge_kwargs: Optional[Dict[str, Any]] = None,
             judge_model: Optional[str] = None, judge_prompt: Optional[str] = None,
             **kwargs) -> Result:
    """Call `judge`, and say whether what came back is a verdict (see the module docstring)."""
    _collisions(judge, {"schema": schema, "run": run, "field": field, "evaluator": evaluator, "inputs": inputs})
    kwargs = {**kwargs, **(judge_kwargs or {})}
    result = Result(status=INVALID, judge_model=judge_model or model_of(judge), judge_prompt=judge_prompt)
    for i in range(retries + 1):
        result.attempts = i + 1
        try:
            done = _step(result, judge(*args, **kwargs), schema, threshold, score_range)
        except Exception as exc:
            done = _failed(result, exc)
        if done:
            break
        if i < retries and backoff:
            time.sleep(backoff * 2 ** i)
    _record(result, run, field, evaluator, inputs)
    return result


async def aevaluate(judge: Callable, *args, schema: Optional[dict] = None, threshold: Optional[float] = 0.5,
                    score_range: Tuple[float, float] = (0.0, 1.0), retries: int = 2, backoff: float = 1.0,
                    run=None, field: Optional[str] = None, evaluator: Optional[str] = None,
                    inputs: Optional[Dict[str, Any]] = None, judge_kwargs: Optional[Dict[str, Any]] = None,
             judge_model: Optional[str] = None, judge_prompt: Optional[str] = None,
                    **kwargs) -> Result:
    """evaluate() for an async judge."""
    _collisions(judge, {"schema": schema, "run": run, "field": field, "evaluator": evaluator, "inputs": inputs})
    kwargs = {**kwargs, **(judge_kwargs or {})}
    result = Result(status=INVALID, judge_model=judge_model or model_of(judge), judge_prompt=judge_prompt)
    for i in range(retries + 1):
        result.attempts = i + 1
        try:
            done = _step(result, await judge(*args, **kwargs), schema, threshold, score_range)
        except Exception as exc:
            done = _failed(result, exc)
        if done:
            break
        if i < retries and backoff:
            await asyncio.sleep(backoff * 2 ** i)
    _record(result, run, field, evaluator, inputs)
    return result
