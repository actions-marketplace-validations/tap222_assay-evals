"""assay_sdk.evaluate: your own evaluators, with results whose validity is explicit."""
import json
import math

import pytest
from fastapi.testclient import TestClient

from assay_sdk import Result, aevaluate, evaluate
from assay_sdk.evaluation import classify_exception

VERDICT = {"type": "object", "required": ["score", "reason"],
           "properties": {"score": {"type": "number", "minimum": 0, "maximum": 1}, "reason": {"type": "string"}}}


def answers(*outs):
    """A judge that returns (or raises) each of these in turn."""
    it = iter(outs)

    def judge(*_a, **_k):
        x = next(it)
        if isinstance(x, BaseException):
            raise x
        return x
    return judge


def test_verdicts_of_every_shape():
    assert evaluate(answers(True)).status == "PASS"
    r = evaluate(answers(0.3), threshold=0.5)
    assert (r.status, r.score, r.valid, r.passed) == ("FAIL", 0.3, True, False)
    r = evaluate(answers('```json\n{"score": 0.9, "reason": "Grounded in step 2."}\n```'), schema=VERDICT)
    assert (r.status, r.score, r.reason, r.attempts) == ("PASS", 0.9, "Grounded in step 2.", 1)
    assert evaluate(answers({"passed": False, "reason": "no"})).status == "FAIL"


@pytest.mark.parametrize("out, problem", [
    ("I think it's pretty good!", "wasn't valid JSON"),
    ({"score": "high", "reason": "x"}, "score isn't a number"),
    ({"score": 1.4, "reason": "x"}, "score is 1.4, above 1"),
    ({"reason": "forgot the score"}, "score is missing"),
    (math.nan, "the score is nan"),  # a metric that divided by zero
    (None, "the judge returned nothing"),
    ({"reason": "no score, no verdict"}, "neither a score nor passed"),
])
def test_what_isnt_a_verdict_is_invalid_never_zero(out, problem):
    schema = VERDICT if isinstance(out, (str, dict)) and "no score" not in str(out) else None
    r = evaluate(answers(out, out, out), schema=schema, backoff=0)
    assert (r.status, r.score, r.valid, r.passed, r.attempts, r.error_kind) == ("INVALID", None, False, None, 3, "invalid")
    assert problem in r.error
    assert r.raw_judge_output is out  # what it said, kept as it said it


def test_an_invalid_answer_is_asked_again():
    r = evaluate(answers("not json", {"score": 0.8, "reason": "ok"}), schema=VERDICT, backoff=0)
    assert (r.status, r.attempts, [s for s, _ in r.history]) == ("PASS", 2, ["INVALID", "PASS"])


class RateLimitError(Exception):
    status_code = 429


def test_failures_are_classified_and_only_transient_ones_retried():
    r = evaluate(answers(TimeoutError("read timed out"), RateLimitError("slow down"), 0.9), backoff=0)
    assert (r.status, r.attempts) == ("PASS", 3)
    r = evaluate(answers(RateLimitError("slow down")), retries=0)
    assert (r.status, r.error_kind, r.score) == ("RATE_LIMITED", "rate_limited", None)
    assert evaluate(answers(TimeoutError()), retries=0).status == "TIMEOUT"
    boom = evaluate(answers(KeyError("score"), 0.9), backoff=0)  # a bug: not asked again
    assert (boom.status, boom.error_kind, boom.attempts) == ("ERROR", "error", 1)
    assert classify_exception(ConnectionResetError()) [0] == "unavailable"
    assert classify_exception(RuntimeError("HTTP 503 from the judge"))[0] == "unavailable"


def test_async_judges():
    import asyncio

    async def judge(q):
        return {"score": 0.2, "reason": q}
    r = asyncio.run(aevaluate(judge, "why", schema=VERDICT))
    assert (r.status, r.reason) == ("FAIL", "why")


def test_results_are_recorded_so_an_invalid_one_is_never_a_failure(tmp_path, monkeypatch):
    import assay_sdk
    from assay.api import create_app
    from assay.config import Settings
    path = tmp_path / "events.jsonl"
    monkeypatch.setenv("ASSAY_PATH", str(path))
    monkeypatch.delenv("ASSAY_URL", raising=False)
    assay_sdk.init()
    with assay_sdk.run("support", test={"run": "nightly", "case": "c1"}) as run:
        run.answer("It has shipped.")
        evaluate(answers("garbage", "garbage"), schema=VERDICT, retries=1, backoff=0, run=run, field="helpful",
                 evaluator="helpful@2")
        evaluate(answers({"score": 0.9, "reason": "ok"}), schema=VERDICT, run=run, field="grounded",
                 evaluator="grounded@1")
    assay_sdk.shutdown()
    client = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'e.db'}")))
    events = [json.loads(x) for x in path.read_text().splitlines()]
    assert client.post("/v1/ingest", json=events, headers={"X-Tenant": "t"}).status_code == 200
    v = client.get("/v1/evals/runs/nightly/verdicts", params={"source": "events:t"}).json()
    by = {c["field"]: c for c in v["checks"]}
    assert by["helpful"]["verdict"] == "INVALID" and by["helpful"]["tries"] == 2
    assert by["helpful"]["raw_output"] == "garbage" and "wasn't valid JSON" in by["helpful"]["reason"]
    assert by["grounded"]["verdict"] == "PASS" and v["counts"]["FAIL"] == 0


def test_ingest_refuses_a_nan_score_and_a_misplaced_error_kind(tmp_path):
    from assay.api import create_app
    from assay.config import Settings
    client = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'n.db'}")))
    base = {"v": 1, "id": "c", "ts": "2026-09-25T10:00:00Z", "type": "check", "test": {"run": "r", "case": "c"}}
    r = client.post("/v1/ingest", content=json.dumps([{**base, "status": "fail", "score": float("nan")}]),
                    headers={"X-Tenant": "t", "Content-Type": "application/json"})
    assert r.status_code == 422 and "NaN" in r.text
    r = client.post("/v1/ingest", json=[{**base, "status": "fail", "error_kind": "invalid"}], headers={"X-Tenant": "t"})
    assert r.status_code == 422 and "error_kind is for status error" in r.text
