"""Beyond the final answer (assay/behavior.py, requires_approval, assay_sdk.testing.expect)."""
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assay import behavior, contracts
from assay.api import create_app
from assay.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
import assay_sdk as assay  # noqa: E402
from assay_sdk.testing import expect  # noqa: E402


def traj(steps, outcome="resolved", seconds=2.0):
    t0 = datetime(2026, 9, 25, 10)
    return {"steps": steps, "outcome": outcome, "started_at": t0, "finished_at": t0 + timedelta(seconds=seconds)}


def llm(tokens_in, cost, tools):
    return {"kind": "reason", "tokens_in": tokens_in, "cost_usd": cost, "tools": [f"t{i}" for i in range(tools)]}


def test_measure_and_what_counts_as_worse():
    before = behavior.measure(traj([llm(1200, 0.004, 8), {"kind": "approval", "name": "refund",
                                                          "args": {"decision": "approved"}}]))
    assert before == {"cost_usd": 0.004, "seconds": 2.0, "steps": 2, "context_tokens": 1200, "tools_exposed": 8,
                      "outcome": "resolved", "approvals": {"refund": "approved"}}
    same = behavior.measure(traj([llm(1300, 0.0045, 9), {"kind": "approval", "name": "refund",
                                                         "args": {"decision": "approved"}}], seconds=2.4))
    assert behavior.compare(same, before) == []  # small moves aren't news
    worse = behavior.measure(traj([llm(9000, 0.012, 30), {"kind": "approval", "name": "refund",
                                                         "args": {"decision": "rejected"}}],
                                  outcome="escalated", seconds=9))
    texts = [c["text"] for c in behavior.compare(worse, before)]
    assert texts == ["Cost: $0.0040 → $0.0120 (3.0×)", "Latency: 2.0s → 9.0s (4.5×)",
                     "Context: 1,200 tokens → 9,000 tokens (7.5×)", "Tools exposed: 8 tools → 30 tools (3.8×)",
                     "Outcome: resolved → escalated", "Approval for refund: approved → rejected"]
    assert [c["metric"] for c in behavior.compare(worse, before, {"cost_usd": 0, "seconds": 5})] == [
        "context_tokens", "tools_exposed", "outcome", "approvals"]  # 0 turns cost off; latency needs 5×
    assert behavior.combine([{"cost_usd": 1, "outcome": "resolved"}, {"cost_usd": 3, "outcome": "resolved"},
                             {"cost_usd": 2, "outcome": "unresolved"}])["cost_usd"] == 2


def test_requires_approval_contract():
    c = {"kind": "requires_approval", "step": "refund"}
    assert contracts.validate(c) is None
    step = lambda name, **args: contracts.Step(name, args)
    ok = [step("get_order"), step("approval:refund", decision="approved"), step("refund", order_id="O-1")]
    assert contracts.breaks(c, ok) is None
    none = [step("get_order"), step("refund", order_id="O-1")]
    assert contracts.breaks(c, none)["detail"] == "ran refund (step 2, order_id='O-1') without an approval"
    revoked = [step("approval:refund", decision="approved"), step("approval:refund", decision="rejected"),
               step("refund")]
    assert contracts.breaks(c, revoked)["detail"] == "ran refund (step 3) after it was rejected, not approved"
    assert contracts.breaks({"kind": "allowed_steps", "steps": ["get_order", "refund"]}, ok) is None  # approvals


@pytest.fixture
def sdk_run():
    assay.init(transport=lambda batch: None, flush_interval=60)
    with assay.run("t", test="case-1") as run:
        yield run
    assay.shutdown()


def test_expect_collects_every_failure(sdk_run):
    run = sdk_run
    e = expect(run).must_call("get_order").must_not_call("delete_order").max_steps(3) \
        .must_get_approval_before("refund").max_cost(0.01).max_tools_exposed(10).max_context_tokens(5000) \
        .must_resolve()  # declared before the agent runs
    run.llm(model="m", tokens_in=8000, cost_usd=0.02, tools=[{"type": "function", "function": {"name": f"f{i}"}}
                                                             for i in range(12)])
    run.call("get_order", lambda order_id: 1, order_id="O-1")
    run.call("refund", lambda order_id: True, order_id="O-1")
    run.call("delete_order", lambda order_id: True, order_id="O-1")
    assert run.steps[0]["tools"][:2] == ["f0", "f1"]  # names from OpenAI-style tool definitions
    with pytest.raises(AssertionError) as err:
        e.verify()
    msg = str(err.value)
    for part in ("must_not_call(delete_order): delete_order shouldn't have been called",
                 "max_steps(3): Took 4 steps", "must_get_approval_before(refund): refund ran at step 2 without an "
                 "approval", "max_cost(0.01): Cost $0.0200", "max_tools_exposed(10): A model call was offered 12 tools",
                 "max_context_tokens(5000): A model call's input reached 8,000 tokens",
                 "must_resolve(): The run has no outcome"):
        assert part in msg
    assert "must_call(get_order)" not in msg  # it held


def test_expect_passes_and_records_each_as_a_check():
    sent = []
    assay.init(transport=lambda batch: sent.extend(batch), flush_interval=60)
    with assay.run("t", test="case-2") as run:
        with expect(run).must_get_approval_before("refund").must_resolve():
            run.approval("refund", "approved", by="manager", reason="under limit")
            run.call("refund", lambda: True)
            run.outcome("resolved")
    assay.flush()
    checks = {e["field"]: e["status"] for e in sent if e["type"] == "check"}
    assert checks == {"expect.must_get_approval_before(refund)": "pass", "expect.must_resolve()": "pass"}
    end = next(e for e in sent if e["type"] == "run.end")
    approval = next(e for e in sent if e["type"] == "step" and e["kind"] == "approval")
    assert end["outcome"] == "resolved" and (approval["decision"], approval["by"]) == ("approved", "manager")
    assay.shutdown()


def test_the_server_stores_approvals_tools_context_and_outcome(tmp_path):
    client = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'b.db'}")))
    ev = lambda i, **kw: {"v": 1, "id": f"e{i}", "ts": "2026-09-25T10:00:00Z", "run_id": "r1", **kw}
    bad = client.post("/v1/ingest", json=[ev(0, type="step", seq=0, kind="approval", name="refund")])
    assert bad.status_code == 422 and "needs a decision" in bad.text
    assert client.post("/v1/ingest", json=[ev(0, type="step", seq=0, kind="tool", name="x", tools=["a"])]
                       ).status_code == 422  # tools are for model calls
    ok = client.post("/v1/ingest", headers={"X-Tenant": "t"}, json=[
        ev(1, type="run.start", test={"run": "n", "case": "c"}),
        ev(2, type="step", seq=0, kind="llm", model="m", tokens_in=900, tools=["get_order", "refund"]),
        ev(3, type="step", seq=1, kind="approval", name="refund", decision="approved", by="manager"),
        ev(4, type="step", seq=2, kind="tool", name="refund", args={"order_id": "O-1"}),
        ev(5, type="run.end", outcome="escalated", ts="2026-09-25T10:00:07Z")])
    assert ok.status_code == 200, ok.text
    t = client.get("/v1/agents/trajectories/r1", params={"source": "events:t"}).json()
    assert t["outcome"] == "escalated"
    assert t["steps"][0]["tokens_in"] == 900 and t["steps"][0]["tools"] == ["get_order", "refund"]
    assert t["steps"][1]["kind"] == "approval" and t["steps"][1]["args"] == {"decision": "approved", "by": "manager"}
    from assay import store
    engine = store.make_engine(f"sqlite:///{tmp_path / 'b.db'}")
    with engine.connect() as conn:
        m = conn.execute(store.run_metrics.select()).first().metrics
    assert (m["context_tokens"], m["tools_exposed"], m["outcome"], m["approvals"], m["seconds"]) == (
        900, 2, "escalated", {"refund": "approved"}, 7.0)
