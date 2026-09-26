"""Agent run lifecycle (assay/lifecycle.py): evaluated when a run ends, not on a timer."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from assay import lifecycle, store
from assay.api import create_app
from assay.config import Settings

H = {"X-Tenant": "t"}
SRC = {"source": "events:t"}


@pytest.fixture
def app(tmp_path):
    settings = Settings(store_url=f"sqlite:///{tmp_path / 'l.db'}", cron_secret="shh")
    client = TestClient(create_app(settings))
    client.engine = store.make_engine(settings.store_url)
    return client


def send(client, *events):
    r = client.post("/v1/ingest", json={"events": list(events)}, headers=H)
    assert r.status_code == 200, r.text


_n = iter(range(10 ** 6))


def ev(type, run_id, ts="2026-09-25T10:00:00Z", **kw):
    return {"v": 1, "id": f"e{next(_n)}", "ts": ts, "type": type, "run_id": run_id, **kw}


def tool(run_id, seq, name, args=None, error=None, **kw):
    return ev("step", run_id, seq=seq, kind="tool", name=name, args=args or {}, error=error,
              status="error" if error else "ok", **kw)


def evaluation(client, run_id):
    return client.get(f"/v1/agents/trajectories/{run_id}", params=SRC).json()["evaluation"]


def test_a_slow_run_is_evaluated_when_it_ends_not_before(app):
    send(app, ev("run.start", "slow", task="refund"), tool("slow", 0, "get_order", {"id": 1}))
    assert evaluation(app, "slow") is None  # still running: not judged half-way
    life = app.get("/v1/agents/lifecycle", params=SRC).json()
    assert life["running"] == 1 and life["evaluated"] == 0

    send(app, tool("slow", 1, "refund", {"id": 1}), ev("run.end", "slow", ts="2026-09-25T10:05:00Z"))
    e = evaluation(app, "slow")  # in the same request that ended it
    assert e["status"] == "completed" and e["failed"] == 0
    assert [c["check"] for c in e["checks"]] == ["completed", "loops", "tool_errors"]
    life = app.get("/v1/agents/lifecycle", params=SRC).json()
    assert (life["running"], life["evaluated"], life["failing"]) == (0, 1, 0)


def test_failures_loops_unrecovered_tool_errors_and_contracts(app):
    assert app.post("/v1/contracts", json={"source": "events:t", "kind": "never",
                                           "step": "delete_order"}).status_code == 201
    send(app, ev("run.start", "bad"),
         *[tool("bad", i, "search", {"q": "x"}) for i in range(3)],
         tool("bad", 3, "charge", {"amount": 5}, error="card declined"),
         tool("bad", 4, "delete_order", {"id": 9}),
         ev("run.end", "bad", status="failed", error="RuntimeError: gave up"))
    e = evaluation(app, "bad")
    failed = {c["check"]: c["reason"] for c in e["checks"] if c["status"] == "fail"}
    assert failed["completed"] == "Failed after step 4: RuntimeError: gave up."
    assert failed["loops"].startswith("Looped: search(q='x') 3 times")
    assert failed["tool_errors"] == "1 tool error nothing recovered: charge (step 3): card declined"
    assert "delete_order never runs" in failed["safety"]
    assert app.get("/v1/agents/lifecycle", params=SRC).json()["recent_failures"][0]["trajectory_id"] == "bad"


def test_a_run_that_goes_quiet_is_abandoned_then_evaluated(app):
    send(app, ev("run.start", "dead"), tool("dead", 0, "lookup"))
    assert lifecycle.sweep(app.engine, abandon_minutes=30) == {"abandoned": 0, "evaluated": 0}  # not yet
    later = datetime.utcnow() + timedelta(minutes=31)
    assert lifecycle.abandon(app.engine, 30, now=later) == 1
    assert lifecycle.sweep(app.engine, 30)["evaluated"] == 1
    e = evaluation(app, "dead")
    assert e["status"] == "abandoned"
    assert e["checks"][0]["reason"] == "Never finished after step 0: no events for 30 minutes."
    assert app.get("/v1/agents/lifecycle", params=SRC).json()["abandoned"] == 1



def test_each_agent_has_its_own_abandon_limit(app):
    r = app.put("/v1/agents/limits", json={"source": "events:t", "abandon_minutes": {"research": 120, "*": 10}},
                headers=H)
    assert r.status_code == 200, r.text
    assert r.json() == {"source": "events:t", "default": 30.0, "abandon_minutes": {"*": 10.0, "research": 120.0}}
    send(app, ev("run.start", "deep", task="research"), tool("deep", 0, "search"),
         ev("run.start", "quick", task="support"), tool("quick", 0, "lookup"))
    later = lambda m: datetime.utcnow() + timedelta(minutes=m)
    assert lifecycle.abandon(app.engine, 30, now=later(11)) == 1  # support: this source's default, 10
    assert lifecycle.abandon(app.engine, 30, now=later(60)) == 0  # research keeps thinking
    assert lifecycle.abandon(app.engine, 30, now=later(121)) == 1
    lifecycle.sweep(app.engine, 30)
    assert evaluation(app, "quick")["checks"][0]["reason"] == "Never finished after step 0: no events for 10 minutes."
    assert evaluation(app, "deep")["checks"][0]["reason"] == "Never finished after step 0: no events for 120 minutes."
    assert app.get("/v1/agents/lifecycle", params=SRC).json()["abandon_limits"] == {"*": 10.0, "research": 120.0}

    r = app.put("/v1/agents/limits", json={"abandon_minutes": {"*": None, "research": 0}}, headers=H)
    assert r.status_code == 422 and "research" in r.json()["detail"]
    r = app.put("/v1/agents/limits", json={"source": "events:t", "abandon_minutes": {"*": None}}, headers=H)
    assert r.json()["abandon_minutes"] == {"research": 120.0}  # null removes it


def test_late_data_is_evaluated_again(app):
    send(app, ev("run.start", "late"), tool("late", 0, "search", {"q": "x"}), ev("run.end", "late"))
    first = evaluation(app, "late")
    assert first["failed"] == 0
    send(app, *[tool("late", i, "search", {"q": "x"}) for i in (1, 2)])  # arrived after run.end
    second = evaluation(app, "late")
    assert second["evaluated_at"] > first["evaluated_at"] and second["failed"] == 1  # now a loop
    send(app, *[tool("late", i, "search", {"q": "x"}) for i in (1, 2)])  # a retry: no new data...
    assert lifecycle.pending(app.engine, "t") == []  # ...but evaluated again, and nothing left over


def test_a_parent_waits_for_its_children(app):
    send(app, ev("run.start", "parent"), ev("run.start", "child", parent_run_id="parent"),
         ev("run.end", "parent"))
    assert evaluation(app, "parent") is None  # its child is still working
    send(app, ev("run.end", "child"))
    assert evaluation(app, "child")["failed"] == 0 and evaluation(app, "parent")["failed"] == 0


def test_a_test_case_fills_in_its_test_run_as_it_ends(app):
    assert app.post("/v1/agents/references", headers=H, json=[
        {"case_id": "c1", "calls": [{"tool": "get_order"}], "answer": "27.61"}]).status_code == 200
    test = {"run": "nightly", "case": "c1"}
    send(app, ev("run.start", "r1", test=test), tool("r1", 0, "get_order"),
         ev("step", "r1", seq=1, kind="answer", text="Refunded $27.61."))
    out = app.post("/v1/agents/runs/nightly/evaluate", params=SRC).json()
    assert out["trajectories"] == 0 and out["running"] == 1  # the explicit call waits too
    send(app, ev("run.end", "r1"))
    runs = app.get("/v1/evals/runs", params=SRC).json()
    assert runs[0]["run_id"] == "nightly" and runs[0]["failed"] == 0 and runs[0]["passed"] >= 3


def test_cron_sweeps_without_scheduled_sources(app):
    send(app, ev("run.start", "quiet"))
    with app.engine.begin() as conn:  # pretend it went quiet an hour ago
        conn.execute(store.agent_trajectories.update().values(updated_at=datetime.utcnow() - timedelta(hours=1)))
    out = app.get("/v1/cron", headers={"Authorization": "Bearer shh"}).json()
    assert out["lifecycle"]["last"]["abandoned"] == 1 and out["lifecycle"]["last"]["evaluated"] == 1
    assert evaluation(app, "quiet")["status"] == "abandoned"


# ---------- OpenTelemetry: a run's spans arrive over several batches ----------

def span(span_id, parent=None, start=0, end=1, name="s", error=False, **attrs):
    val = lambda v: {"intValue": v} if isinstance(v, int) else {"stringValue": v}
    return {"traceId": "tr1", "spanId": span_id, **({"parentSpanId": parent} if parent else {}), "name": name,
            "startTimeUnixNano": str(1_790_000_000_000_000_000 + start * 10 ** 9),
            "endTimeUnixNano": str(1_790_000_000_000_000_000 + end * 10 ** 9),
            "status": {"code": 2, "message": "boom"} if error else {},
            "attributes": [{"key": k.replace("__", "."), "value": val(v)} for k, v in attrs.items()]}


def otlp(app, *spans):
    r = app.post("/v1/otlp/v1/traces", headers={**H, "Content-Type": "application/json"},
                 json={"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]})
    assert r.status_code == 200, r.text


def test_otlp_run_stays_open_until_its_root_span_then_is_evaluated_whole(app):
    tool = lambda sid, start, q: span(sid, "root", start, start + 1, name="execute_tool",
                                      gen_ai__operation__name="execute_tool", gen_ai__tool__name="search",
                                      gen_ai__tool__call__arguments=f'{{"q": "{q}"}}')
    otlp(app, tool("t1", 1, "a"), tool("t2", 3, "b"))  # batch 1: two tool spans, no root yet
    t = app.get("/v1/agents/trajectories/tr1", params=SRC).json()
    assert t["status"] == "running" and len(t["steps"]) == 2 and t["evaluation"] is None
    otlp(app, tool("t3", 5, "c"), tool("t2", 3, "b"))  # batch 2: one more, and a retry of t2
    assert len(app.get("/v1/agents/trajectories/tr1", params=SRC).json()["steps"]) == 3  # kept, not replaced
    otlp(app, span("root", None, 0, 9, name="agent", assay__task="support", assay__answer="found it"))  # root alone
    t = app.get("/v1/agents/trajectories/tr1", params=SRC).json()
    assert t["status"] == "completed" and t["task"] == "support" and t["answer"] == "found it"
    assert [s["args"]["q"] for s in t["steps"] if s["kind"] == "tool"] == ["a", "b", "c"]
    assert t["evaluation"]["status"] == "completed" and t["evaluation"]["failed"] == 0
    otlp(app, tool("t4", 6, "d"))  # a straggler after the root: still ended, and evaluated again
    t = app.get("/v1/agents/trajectories/tr1", params=SRC).json()
    assert t["status"] == "completed" and len(t["steps"]) == 4 and t["answer"] == "found it"


def test_otlp_root_span_that_errored_fails_the_run(app):
    otlp(app, span("root", None, 0, 4, error=True, assay__task="support"),
         span("t1", "root", 1, 2, gen_ai__tool__name="charge", error=True))
    e = evaluation(app, "tr1")
    assert e["status"] == "failed" and {c["check"] for c in e["checks"] if c["status"] == "fail"} == {
        "completed", "tool_errors"}


def test_otlp_turns_of_one_conversation(app):
    otlp(app, span("t1", "root", 1, 2, name="execute_tool", gen_ai__tool__name="search",
                   gen_ai__conversation__id="chat-9"),
         span("root", None, 0, 3, name="agent", assay__turn=1, assay__answer="done"))
    t = app.get("/v1/agents/trajectories/tr1", params=SRC).json()
    assert (t["conversation_id"], t["turn"]) == ("chat-9", 1)
    assert app.get("/v1/agents/conversations/chat-9", params=SRC).json()["turns"][0]["trajectory_id"] == "tr1"


def plan(run_id, seq, steps, **kw):
    return ev("step", run_id, seq=seq, kind="plan", plan=steps, **kw)


def plan_check(app, run_id):
    return next(c for c in evaluation(app, run_id)["checks"] if c["check"] == "plan")


def test_a_run_is_checked_against_its_own_plan(app):
    steps = ["search_customer", "get_order", {"tool": "refund", "args": {"id": "O-17"}}]
    send(app, ev("run.start", "kept"), plan("kept", 0, steps, text="Find the customer, check the order, refund"),
         tool("kept", 1, "search_customer"), tool("kept", 2, "lookup_faq"),  # unplanned, but harmless
         tool("kept", 3, "get_order", error="503"), tool("kept", 4, "get_order"),  # an error, retried
         tool("kept", 5, "refund", {"id": "O-17"}), ev("run.end", "kept"))
    assert plan_check(app, "kept")["status"] == "pass"

    send(app, ev("run.start", "strayed"), plan("strayed", 0, steps), tool("strayed", 1, "refund", {"id": "O-18"}),
         tool("strayed", 2, "search_customer"), ev("run.end", "strayed"))
    c = plan_check(app, "strayed")
    assert c["status"] == "fail"
    assert c["reason"] == ("Strayed from its plan: planned refund(id='O-17'), called refund(id='O-18') (step 1): "
                           "id: expected 'O-17', got 'O-18' (+2 more).")

    send(app, ev("run.start", "skipped"), plan("skipped", 0, steps), tool("skipped", 1, "search_customer"),
         tool("skipped", 2, "get_order", error="timeout"), tool("skipped", 3, "refund", {"id": "O-17"}),
         ev("run.end", "skipped"))
    assert plan_check(app, "skipped")["reason"] == \
        "Strayed from its plan: planned get_order, but it errored and was never made."


def test_replanning_isnt_skipping(app):
    send(app, ev("run.start", "re"), plan("re", 0, ["search_customer", "refund"]), tool("re", 1, "search_customer"),
         plan("re", 2, ["escalate"], text="Not eligible for a refund: hand it to a person"),
         tool("re", 3, "escalate"), ev("run.end", "re"))
    assert plan_check(app, "re")["status"] == "pass"
    send(app, ev("run.start", "died"), plan("died", 0, ["search_customer", "refund"]),
         tool("died", 1, "search_customer"), ev("run.end", "died", status="failed", error="crashed"))
    assert plan_check(app, "died")["status"] == "pass"  # "completed" fails it; not skipping twice
    send(app, ev("run.start", "none"), tool("none", 0, "search_customer"), ev("run.end", "none"))
    assert "plan" not in [c["check"] for c in evaluation(app, "none")["checks"]]  # no plan: nothing to adhere to
