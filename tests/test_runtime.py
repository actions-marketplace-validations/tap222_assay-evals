"""EvalRuntime (assay_sdk/runtime.py): many samples within limits, one retry layer, and what it cost."""
import asyncio
import threading
import time
from types import SimpleNamespace as N

import pytest

from assay_sdk import EvalRuntime, Judge, Sample
from assay_sdk.llm import normalize
from assay_sdk.runtime import cost_of, retry_after

VERDICT = {"type": "object", "required": ["score"], "properties": {"score": {"type": "number"}}}


def fast(**kw):
    return EvalRuntime(**{"backoff": 0, "timeout": 5, **kw})


class HTTPError(Exception):
    def __init__(self, code, headers=None):
        super().__init__(f"HTTP {code}")
        self.status_code, self.headers = code, headers or {}


# ---------- limits ----------

def test_concurrency_is_a_limit_for_async_and_sync_judges():
    for is_async in (True, False):
        live, most, lock = [0], [0], threading.Lock()

        def enter():
            with lock:
                live[0] += 1
                most[0] = max(most[0], live[0])

        def leave():
            with lock:
                live[0] -= 1

        async def ajudge(x):
            enter()
            await asyncio.sleep(0.02)
            leave()
            return {"score": x / 20}

        def sjudge(x):
            enter()
            time.sleep(0.02)
            leave()
            return {"score": x / 20}
        report = fast(concurrency=4).run(ajudge if is_async else sjudge, range(20), threshold=0.5)
        assert most[0] <= 4 and most[0] >= 2
        assert (report.passed, report.failed, len(report.results)) == (10, 10, 20)
        assert [r.id for r in report.results] == list(range(20))  # in the order given


def test_one_sample_that_breaks_is_its_own_result():
    def judge(x):
        if x == 3:
            raise ValueError("a bug in the judge")
        return True
    report = fast(concurrency=3, retries=3).run(judge, range(6))
    bad = report.results[3]
    assert (bad.status, bad.error_kind, bad.calls, bad.retries) == ("ERROR", "error", 1, 0)  # a bug isn't asked again
    assert report.passed == 5 and report.llm_calls == 6


def test_a_429_is_asked_again_after_retry_after_and_pauses_everyone():
    seen = []

    def judge(x):
        seen.append((x, time.monotonic()))
        if len([s for s in seen if s[0] == x]) == 1 and x == 0:
            raise HTTPError(429, {"retry-after": "0.3"})
        return True
    t0 = time.monotonic()
    report = fast(concurrency=1, retries=2).run(judge, range(2))
    assert report.passed == 2 and report.retries == 1 and report.llm_calls == 3
    assert seen[1][1] - t0 >= 0.29  # waited as long as the provider asked
    assert report.results[0].retries == 1 and report.results[0].result.attempts == 2


def test_retry_on_decides_which_http_errors_are_asked_again():
    def judge(x):
        raise HTTPError(500)
    r = fast(retries=3, retry_on=[429]).run(judge, [1]).results[0]
    assert (r.status, r.error_kind, r.calls) == ("ERROR", "unavailable", 1)
    r = fast(retries=2).run(judge, [1]).results[0]
    assert r.calls == 3 and r.retries == 2


def test_a_provider_asking_for_a_long_wait_isnt_waited_for():
    def judge(x):
        raise HTTPError(429, {"retry-after": "3600"})
    r = fast(retries=3, max_backoff=5).run(judge, [1]).results[0]
    assert (r.status, r.calls) == ("RATE_LIMITED", 1)


def test_an_invalid_answer_is_asked_again_unless_told_not_to():
    answers = iter(["not json", '{"score": 0.9}'])
    r = fast(retries=2).run(lambda x: next(answers), [1], threshold=0.5).results[0]
    assert (r.status, r.retries) == ("PASS", 1)
    r = fast(retries=2, retry_invalid=False).run(lambda x: "not json", [1]).results[0]
    assert (r.status, r.error_kind, r.calls) == ("INVALID", "invalid", 1)
    assert r.result.score is None  # never a 0


def test_a_slow_judge_times_out_and_the_rest_go_on():
    def judge(x):
        if x == 0:
            time.sleep(1.0)
        return True
    report = fast(timeout=0.1, retries=0, concurrency=2).run(judge, range(4))
    assert report.results[0].status == "TIMEOUT" and report.passed == 3
    assert "no answer within 0.1s" in report.results[0].error


def test_a_cancelled_error_from_a_library_is_that_samples_failure():
    """Ragas-style: a client gives up on a 429 by raising CancelledError."""
    calls = []

    async def judge(x):
        calls.append(x)
        if x == 1 and calls.count(1) == 1:
            raise asyncio.CancelledError()
        return True
    report = fast(concurrency=2, retries=1).run(judge, range(3))
    assert report.passed == 3 and report.results[1].retries == 1


def test_cancelling_the_run_keeps_what_was_judged():
    async def judge(x):
        await asyncio.sleep(0 if x < 2 else 5)
        return True

    async def main():
        rt = fast(concurrency=1)
        task = asyncio.ensure_future(rt.arun(judge, range(5)))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return rt.report
    report = asyncio.run(main())
    assert report.passed == 2 and report.counts["not_run"] == 3
    assert report.stopped == "the run was cancelled"


def test_max_time_leaves_the_rest_not_run_never_failed():
    def judge(x):
        time.sleep(0.15)
        return True
    report = fast(concurrency=1, max_time=0.4).run(judge, range(10))
    assert 2 <= report.passed <= 3 and report.failed == 0
    assert report.counts["not_run"] == 10 - report.passed - report.counts["timeout"]
    assert "max_time" in report.stopped and "not run: the run's time limit" in str(report)


def test_the_rate_limit_spaces_calls_across_samples():
    t0 = time.monotonic()
    report = fast(concurrency=5, rate_limit=600).run(lambda x: True, range(5))  # one call per 0.1s
    assert time.monotonic() - t0 >= 0.39 and report.passed == 5


# ---------- counting: calls, tokens, cost ----------

class Messages:
    """An Anthropic client: fails the first `fail` calls with `error`, then answers."""

    def __init__(self, fail=0, error=None, text='{"score": 0.8}'):
        self.fail, self.error, self.text, self.calls, self.options = fail, error, text, 0, []
        self.messages = self

    def with_options(self, **kw):
        self.options.append(kw)
        return self

    def create(self, **kw):
        self.calls += 1
        if self.calls <= self.fail:
            raise self.error
        return N(model="claude-opus-5", stop_reason="end_turn", content=[N(type="text", text=self.text)],
                 usage=N(input_tokens=1000, output_tokens=100, cache_read_input_tokens=0))


def test_through_judge_every_request_is_counted_once_with_the_sdks_retries_off():
    client = Messages(fail=2, error=HTTPError(529))
    judge = Judge("anthropic", "claude-opus-5", client=client)
    report = fast(retries=3, timeout=20, prices={"claude-opus-5": (5, 25)}).run(judge, ["Rate this"], schema=VERDICT,
                                                                                threshold=0.5)
    r = report.results[0]
    assert (r.status, r.calls, r.retries, client.calls) == ("PASS", 3, 2, 3)
    assert client.options[0] == {"max_retries": 0, "timeout": 20}  # the one retry layer is the runtime's
    assert report.tokens == {"input": 1000, "output": 100, "cached": 0}  # the two that failed used none
    assert report.cost_usd == pytest.approx((1000 * 5 + 100 * 25) / 1e6)
    assert "LLM calls:      3" in str(report) and "Retries:        2" in str(report)
    assert report.unpriced_calls == 0  # the two 529s used no tokens: they cost nothing
    assert "Estimated cost: $0.01" in str(report)


def test_without_a_runtime_judge_is_unchanged():
    client = Messages()
    assert Judge("anthropic", "claude-opus-5", client=client).ask("x").ok and client.options == []


def test_the_budget_stops_new_samples():
    judge = Judge("anthropic", "claude-opus-5", client=Messages())
    report = fast(concurrency=1, budget_usd=0.02, prices={"claude": (5, 25)}).run(judge, ["a"] * 10, schema=VERDICT)
    assert report.passed == 3 and report.counts["not_run"] == 7  # $0.0075 a call: the third passes $0.02
    assert "budget ($0.02)" in report.stopped


def test_unpriced_and_unseen_calls_say_so():
    report = fast().run(lambda x: True, [1])
    assert report.cost_usd is None and "the judge's calls aren't seen" in str(report)
    report = fast().run(Judge("anthropic", "claude-opus-5", client=Messages()), ["x"], schema=VERDICT)
    assert "no price for claude-opus-5" in str(report)


def test_an_attempt_that_calls_twice_is_reported():
    inner = Judge("anthropic", "claude-opus-5", client=Messages())

    def judge(x):
        inner.ask(x)  # e.g. a library that asks, then asks again
        return inner.ask(x, schema=VERDICT)
    report = fast().run(judge, ["x", "y"], threshold=0.5)
    assert report.passed == 2 and report.llm_calls == 4 and report.retries == 0 and report.repeated_calls == 2
    assert "2 attempts called the model more than once" in str(report)


def test_instrumented_calls_are_seen_and_not_double_counted(monkeypatch):
    import assay_sdk.auto as auto
    from assay_sdk import runtime
    reading = auto._reading("anthropic")
    resp = Messages().create()
    scope_calls = []

    def judge(x):
        auto._observe(reading, {}, resp)  # what a patched client.messages.create does
        scope_calls.append(runtime.current().calls)
        return True
    report = fast(prices={"claude-opus-5": (5, 25)}).run(judge, [1])
    assert report.llm_calls == 1 and report.tokens["input"] == 1000 and report.cost_usd > 0 and scope_calls == [1]


def test_identical_samples_are_judged_once_with_cache():
    n = []

    def judge(q):
        n.append(q)
        return True
    report = fast(cache=True, concurrency=3).run(judge, ["a", "b", "a", "a"])
    assert sorted(n) == ["a", "b"] and report.passed == 4 and report.cached == 2
    assert report.llm_calls == 2


def test_samples_record_checks_on_their_runs():
    class Run:
        def __init__(self):
            self.checks = []

        def check(self, field, status, **kw):
            self.checks.append((field, status, kw.get("error_kind")))
    a, b = Run(), Run()
    fast(retries=0).run(lambda q, answer: {"score": 0.9} if answer else "??",
                        [Sample("q1", "yes", id="q1", run=a), Sample("q2", answer=None, id="q2", run=b)],
                        field="helpful", threshold=0.5)
    assert a.checks == [("helpful", "pass", None)] and b.checks == [("helpful", "error", "invalid")]
    shared = Run()
    fast().run(lambda q: True, ["x", "y"], run=shared, field="ok")
    assert sorted(c[0] for c in shared.checks) == ["ok:0", "ok:1"]


def test_map_runs_your_own_function_under_the_same_limits():
    def fn(item):
        if item == "boom":
            raise KeyError("no")
        return item.upper()
    report = fast(concurrency=2).map(fn, ["a", "boom", "c"])
    assert [r.value for r in report.results] == ["A", None, "C"] and report.counts["done"] == 2
    assert report.results[1].error_kind == "error" and "2 done" in str(report)


def test_run_works_from_inside_a_running_loop():
    async def main():
        return fast().run(lambda x: True, [1, 2])
    assert asyncio.run(main()).passed == 2


def test_report_reads_like_the_summary():
    answers = {0: True, 1: False, 2: "??", 3: HTTPError(429)}

    def judge(x):
        a = answers[x]
        if isinstance(a, Exception):
            raise a
        return a
    text = str(fast(retries=0).run(judge, range(4)))
    for line in ("4 samples in", "2 judged (1 passed, 1 failed)", "1 invalid judge output", "1 rate limited",
                 "LLM calls:      4", "Retries:        0"):
        assert line in text, text


# ---------- pieces ----------

def test_cost_and_retry_after_readers():
    anth = normalize(N(model="claude-opus-5", stop_reason="end_turn", content=[],
                       usage=N(input_tokens=100, output_tokens=10, cache_read_input_tokens=1000)))
    assert cost_of(anth, {"claude-opus": (5, 25, 0.5)}) == pytest.approx((100 * 5 + 1000 * 0.5 + 10 * 25) / 1e6)
    oai = normalize({"model": "gpt-5-mini", "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
                     "usage": {"prompt_tokens": 1100, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 1000}}})
    assert cost_of(oai, {"gpt-5": (1, 2), "gpt-5-mini": (0.5, 2)}) == pytest.approx((100 * 0.5 + 1000 * 0.5 + 20) / 1e6)
    lite = normalize({"model": "x", "choices": [{"finish_reason": "stop", "message": {"content": "x"}}],
                      "_hidden_params": {"response_cost": 0.0042}})
    assert lite.cost == 0.0042 and cost_of(lite, {}) == 0.0042
    assert cost_of(normalize({"model": "unknown-model", "choices": [], "usage": {"prompt_tokens": 1}}), {}) is None
    assert retry_after(HTTPError(429, {"retry-after-ms": "1500"})) == 1.5
    assert retry_after(HTTPError(429, {"retry-after": "7"})) == 7
    assert retry_after(HTTPError(429, {"retry-after": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0
    assert retry_after(ValueError("x")) is None


def test_prices_from_the_environment(monkeypatch):
    monkeypatch.setenv("ASSAY_PRICES", '{"claude-opus-5": [5, 25]}')
    assert EvalRuntime().prices == {"claude-opus-5": (5.0, 25.0)}
    monkeypatch.setenv("ASSAY_PRICES", "nope")
    with pytest.raises(ValueError, match="ASSAY_PRICES"):
        EvalRuntime()
