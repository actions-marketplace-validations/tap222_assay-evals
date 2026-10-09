from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from assay import agents
from assay.api import create_app
from assay.config import Settings

T0 = datetime(2026, 9, 1, 12)


def traj(*steps, answer=None):
    out = []
    for i, s in enumerate(steps):
        kind, rest = s[0], s[1:]
        base = dict(seq=i, kind=kind, name=None, args=None, result=None, error=None, text=None, model=None,
                    tokens=None, cost_usd=None, started_at=T0 + timedelta(seconds=i), finished_at=None)
        if kind == "tool":
            base |= dict(name=rest[0], args=rest[1], result=rest[2] if len(rest) > 2 else None,
                         error=rest[3] if len(rest) > 3 else None)
        elif kind == "state":
            base |= dict(name=rest[0], args={"op": rest[1]}, result=rest[2])
        elif kind == "answer":
            base |= dict(text=rest[0])
        out.append(base)
    return {"steps": out, "answer": answer, "started_at": T0, "finished_at": T0 + timedelta(seconds=len(out)),
            "task": "refund_request", "lineage": {}}


REF = {"calls": [{"tool": "get_order", "args": {"order_id": "1"}},
                 {"tool": "issue_refund", "args": {"order_id": "1", "amount": 42.5}}],
       "allow_extra": ["lookup_customer"], "answer": "42.50", "answer_match": "contains", "max_steps": 10,
       "state": [{"object": "refund:1", "exists": True}, {"object": "refund:*", "field": "amount", "equals": 42.5}]}
GOOD = [("tool", "lookup_customer", {"email": "a@b"}), ("tool", "get_order", {"order_id": "1"}, {"price": 42.5}),
        ("tool", "issue_refund", {"order_id": "1", "amount": 42.5}), ("state", "refund:1", "create", {"amount": 42.5}),
        ("answer", "Refunded $42.50.")]


def test_a_good_trajectory_passes_everything():
    t = traj(*GOOD, answer="Refunded $42.50.")
    ev = agents.evaluate_one(t, REF, [])
    assert ev["answer"] and ev["tool_calls"]["passed"] and ev["tool_calls"]["precision"] == 1.0
    assert all(a["passed"] for a in ev["end_state"]) and ev["efficiency"]["passed"]


def test_divergence_wrong_tool_and_bad_arguments():
    wrong = traj(("tool", "search_orders", {"customer_id": "c"}), ("tool", "issue_refund", {"order_id": "1", "amount": 42.5}))
    d = agents.compare(wrong, REF)["divergence"]
    assert d["kind"] == "wrong_tool" and d["seq"] == 0 and d["expected_tool"] == "get_order"
    bad = traj(("tool", "get_order", {"order_id": "1"}), ("tool", "issue_refund", {"order_id": "1", "amount": 40}))
    c = agents.compare(bad, REF)
    assert c["divergence"]["kind"] == "bad_arguments" and "amount: expected 42.5, got 40" in c["divergence"]["detail"]
    assert c["recall"] == 0.5


def test_retry_after_an_error_is_not_a_divergence_and_stopping_early_is():
    retried = traj(("tool", "get_order", {"order_id": "1"}, None, "timeout"), ("tool", "get_order", {"order_id": "1"}),
                   ("tool", "issue_refund", {"order_id": "1", "amount": 42.5}))
    assert agents.compare(retried, REF)["passed"]
    stopped = traj(("tool", "get_order", {"order_id": "1"}), ("answer", "Done"))
    assert agents.compare(stopped, REF)["divergence"]["kind"] == "gave_up"


def test_answer_matching_is_whole_word():
    ref = {"answer": "3"}
    assert agents.check_answer({"answer": "Your order now has 3 items."}, ref)
    assert not agents.check_answer({"answer": "Your order now has 13 items."}, ref)
    assert agents.check_answer({"answer": "Refunded $1,240.00"}, {"answer": "1240.00"})


def test_first_bad_step_is_the_earliest():
    # Loops on the lookup first, then refunds the wrong amount: the loop is the first bad step.
    t = traj(*([("tool", "lookup_customer", {"email": "a@b"})] * 3), ("tool", "get_order", {"order_id": "1"}),
             ("tool", "issue_refund", {"order_id": "1", "amount": 40}), answer="Refunded $40.00")
    why = agents.credit(t, REF, [])
    assert why["mechanism"] == "looped" and why["seq"] == 2
    assert agents.credit(t, REF, [], "tool_calls")["mechanism"] == "looped"  # earliest wins for any check
    # The answer was in a tool result and the final answer doesn't have it.
    t = traj(("tool", "get_order", {"order_id": "1"}, {"refundable": "42.50"}),
             ("tool", "issue_refund", {"order_id": "1", "amount": 42.5}), answer="Refunded.")
    assert agents.credit(t, REF, [], "answer")["mechanism"] == "ignored_result"


def test_unsafe_action_from_an_argument_aware_contract():
    rule = {"id": 1, "kind": "never", "step": "delete_order", "where": {"confirmed": {"not": True}},
            "severity": "critical"}
    t = traj(("tool", "delete_order", {"order_id": "1"}), answer="Deleted.")
    assert agents.credit(t, None, [rule], "safety")["mechanism"] == "unsafe_action"
    assert not agents.safety(traj(("tool", "delete_order", {"order_id": "1", "confirmed": True})), [rule])


# ---------- end to end ----------

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}")))


def send(client, run, start, broken):
    trajs = []
    for i in range(30):
        wrong = broken and i < 12
        steps = [{"kind": "reason", "model": "m", "tokens": 500, "cost_usd": 0.001},
                 {"kind": "tool", "name": "search_orders" if wrong else "get_order",
                  "args": {"customer_id": "c"} if wrong else {"order_id": f"o{i}"}, "result": {"price": 10}},
                 {"kind": "tool", "name": "issue_refund", "args": {"order_id": f"o{i}", "amount": 10}},
                 {"kind": "state", "name": f"refund:o{i}", "args": {"op": "create"}, "result": {"amount": 10}},
                 {"kind": "answer", "text": "Refunded $10.00"}]
        trajs.append({"trajectory_id": f"{run}.c{i}", "run_id": run, "case_id": f"c{i}", "attempt": 0,
                      "task": "refund", "started_at": (start + timedelta(seconds=i)).isoformat(),
                      "finished_at": (start + timedelta(seconds=i + 5)).isoformat(), "answer": "Refunded $10.00",
                      "lineage": {"prompt": "agent@v2" if broken else "agent@v1"}, "steps": steps})
    assert client.post("/v1/events/trajectories", json=trajs, headers={"X-Tenant": "a"}).json() == {"ingested": 30}


def test_agent_run_end_to_end(client):
    src = "events:a"
    refs = [{"case_id": f"c{i}", "calls": [{"tool": "get_order", "args": {"order_id": f"o{i}"}},
                                            {"tool": "issue_refund", "args": {"order_id": f"o{i}"}}],
             "answer": "10.00", "state": [{"object": f"refund:o{i}", "exists": True}]} for i in range(30)]
    assert client.post("/v1/agents/references", json=refs, headers={"X-Tenant": "a"}).json() == {"ingested": 30}
    client.post("/v1/contracts", json={"source": src, "kind": "only_after", "step": "issue_refund",
                                       "other": "get_order", "same": ["order_id"]})
    now = datetime.utcnow()
    send(client, "r1", now - timedelta(days=2), broken=False)
    send(client, "r2", now - timedelta(hours=2), broken=True)
    for r in ("r1", "r2"):
        out = client.post(f"/v1/agents/runs/{r}/evaluate", params={"source": src}).json()
        assert out["trajectories"] == 30
    assert out["checks"]["tool_calls"] == {"pass": 18, "fail": 12} and out["checks"]["safety"]["fail"] == 12

    s = client.get("/v1/agents/runs/r2", params={"source": src}).json()
    assert s["baseline"] == "r1" and s["current"]["mechanisms"] == {"wrong_tool": 12}
    assert s["previous"]["checks"]["tool_calls"]["rate"] == 1.0

    t = client.get("/v1/agents/trajectories/r2.c0", params={"source": src}).json()
    assert t["first_bad"]["mechanism"] == "wrong_tool" and t["first_bad"]["seq"] == 1
    assert t["tool_calls"]["divergence"]["expected_tool"] == "get_order" and t["safety"][0]["tool"] == "issue_refund"

    causes = client.get("/v1/evals/runs/r2/failures", params={"source": src}).json()
    names = {(g["kind"], g["mechanism"]): g for g in causes["groups"]}
    wrong = names[("ai", "wrong_tool")]
    assert wrong["regression"] is True and wrong["cases"] == 12 and "agent@v1 → agent@v2" in " ".join(wrong["evidence"])
    assert names[("ai", "unsafe_action")]["cases"] == 12

    # Tools are steps: the workflow graph and trace work on agents too.
    wf = client.get("/v1/workflow", params={"source": src, "days": 7}).json()
    assert {"get_order", "search_orders", "issue_refund", "answer"} <= {n["id"] for n in wf["nodes"]}
    assert client.get("/v1/trace/r2.c0", params={"source": src}).status_code == 200



def test_a_check_the_test_failed_fails_the_run_on_the_dashboard(client):
    """expect(...) failing in the test is a failure there too, though every check the server
    works out itself passes."""
    ts = datetime.utcnow().isoformat() + "Z"
    test = {"case": "tests/test_support.py::test_refund", "run": "t-1"}
    ev = lambda i, **kw: {"v": 1, "id": f"e{i}", "ts": ts, "run_id": "tr1", **kw}
    events = [ev(0, type="run.start", kind="agent", task="test_refund", test=test),
              ev(1, type="step", seq=0, kind="tool", name="get_order", args={"order_id": "1"}, result={"ok": True}),
              ev(2, type="step", seq=1, kind="tool", name="refund", args={"order_id": "1"}, result={"ok": True}),
              ev(3, type="step", seq=2, kind="answer", text="Refunded."),
              ev(4, type="check", test=test, status="pass", field="expect.must_call(get_order)",
                 evaluator="assay.expect@1"),
              ev(5, type="check", test=test, status="fail", field="expect.must_get_approval_before(refund)",
                 evaluator="assay.expect@1", reason="refund ran at step 2 without an approval"),
              ev(6, type="run.end", status="completed", outcome="resolved")]
    assert client.post("/v1/ingest", json={"events": events}, headers={"X-Tenant": "a"}).status_code == 200
    assert client.post("/v1/agents/runs/t-1/evaluate", params={"source": "events:a"}).status_code == 200

    s = client.get("/v1/agents/runs/t-1", params={"source": "events:a"}).json()["current"]
    (row,) = s["trajectories_list"]
    assert row["checks"]["efficiency"] == "pass" and row["checks"]["recorded"] == "fail"
    assert row["failing_recorded"] == ["expect.must_get_approval_before(refund)"]
    assert s["checks"]["recorded"] == {"passed": 0, "total": 1, "rate": 0.0}

    t = client.get("/v1/agents/trajectories/tr1", params={"source": "events:a"}).json()
    assert t["failed"] == ["recorded"]
    bad = [c for c in t["recorded"] if c["status"] == "fail"]
    assert [c["reason"] for c in bad] == ["refund ran at step 2 without an approval"]
    assert all(c["evaluator"] != agents.EVALUATOR for c in t["recorded"])  # the server's own aren't repeated

def test_opentelemetry_agent_spans_become_a_trajectory():
    from assay.ingest import from_otlp
    ns = lambda s: str(int((T0 + timedelta(seconds=s)).timestamp() * 1e9))
    kv = lambda k, v: {"key": k, "value": {"stringValue": v} if isinstance(v, str) else {"intValue": str(v)}}
    spans = [
        {"traceId": "t1", "spanId": "root", "name": "agent", "startTimeUnixNano": ns(0), "endTimeUnixNano": ns(9),
         "attributes": [kv("assay.answer", "Refunded $10.00"), kv("assay.case_id", "c1"), kv("assay.run_id", "r")]},
        {"traceId": "t1", "spanId": "s1", "parentSpanId": "root", "name": "chat", "startTimeUnixNano": ns(1),
         "endTimeUnixNano": ns(2), "attributes": [kv("gen_ai.request.model", "m"), kv("gen_ai.usage.input_tokens", 100)]},
        {"traceId": "t1", "spanId": "s2", "parentSpanId": "root", "name": "execute_tool get_order",
         "startTimeUnixNano": ns(3), "endTimeUnixNano": ns(4),
         "attributes": [kv("gen_ai.operation.name", "execute_tool"), kv("gen_ai.tool.name", "get_order"),
                        kv("gen_ai.tool.call.arguments", '{"order_id": "o1"}')]},
    ]
    b = from_otlp({"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]})
    t = b.trajectories[0]
    assert t.case_id == "c1" and [s.kind for s in t.steps] == ["reason", "tool", "answer"]
    assert t.steps[1].args == {"order_id": "o1"} and not b.calls  # the model span isn't counted twice
