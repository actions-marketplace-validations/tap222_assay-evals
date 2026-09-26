"""Evaluator inputs, checked against the trace (assay/audit.py)."""
import pytest
from fastapi.testclient import TestClient

from assay.api import create_app
from assay.audit import findings, roles
from assay.config import Settings

RUN = {"input": {"message": "Refund order O-17, it arrived broken."}, "outputs": ["Refunded $27.61 for O-17."]}
Q, A = "Refund order O-17, it arrived broken.", "Refunded $27.61 for O-17."
RUBRIC = "Rate from 1 to 5 how well the reply resolves the customer's request."


def test_the_right_data_has_no_findings():
    assert findings({"query": Q, "generation": A, "instructions": RUBRIC,
                     "messages": [{"role": "system", "content": RUBRIC}, {"role": "user", "content": f"{Q}\n{A}"}]},
                    RUN) == []
    assert findings({"question": Q, "response": A + " Anything else?"}, RUN) == []  # a wrapped copy matches


def test_generation_filled_with_the_trace_input():
    f = findings({"query": Q, "generation": Q}, RUN)
    assert f == ["query and output are the same text, so the output graded isn't a response to anything",
                 "output is the run's input, not its answer (the app answered “Refunded $27.61 for O-17.”)"]


def test_output_from_somewhere_else_and_query_from_another_run():
    assert findings({"query": Q, "output": "Your order O-99 has shipped."}, RUN) == [
        "output isn't what the app produced (it answered “Refunded $27.61 for O-17.”)"]
    assert findings({"query": "Where is O-18?", "output": A}, RUN) == [
        "query isn't the run's input (the run was asked “Refund order O-17, it arrived broken.”)"]


def test_unfilled_and_empty_values():
    assert findings({"query": "{{query}}", "generation": A}, RUN) == [
        "query still holds a template variable nobody filled in: {{query}}"]
    assert findings({"query": Q, "output": A, "context": []}, RUN) == ["context is empty"]
    assert findings({"query": "${input.text}", "output": A}, None) == [
        "query still holds a template variable nobody filled in: ${input.text}"]


def test_instructions_sent_as_the_users_message():
    msgs = [{"role": "user", "content": f"{RUBRIC}\nQ: {Q}\nA: {A}"}]
    assert findings({"query": Q, "output": A, "instructions": RUBRIC, "messages": msgs}, RUN) == [
        "the evaluator's instructions were sent as the user's message, not as the system prompt"]
    assert findings({"query": f"{RUBRIC} {Q}", "output": A, "instructions": RUBRIC}, RUN)[-1] == \
        "the evaluator's instructions are inside the query, mixed with the user's request"



DOCS = [{"order_id": "O-17", "status": "delivered", "note": "Customer reported the item arrived broken."},
        {"policy": "Damaged items are refunded in full within 30 days of delivery."}]
RAG = {**RUN, "retrieved": DOCS}


def test_context_checked_against_what_the_run_retrieved():
    right = ["Damaged items are refunded in full within 30 days of delivery.",
             "Customer reported the item arrived broken."]
    assert findings({"query": Q, "output": A, "context": right}, RAG) == []
    assert findings({"query": Q, "output": A, "context": "Policy: damaged items are refunded in full within 30 "
                                                          "days of delivery."}, RAG) == []  # a wrapped passage
    # From another run's retrieval
    assert findings({"query": Q, "output": A, "context": ["Orders ship within 2 business days.",
                                                          "Order O-99 was delivered on Monday."]}, RAG) == [
        "context isn't what the run retrieved (its tools returned “O-17 delivered Customer reported the item "
        "arrived broken.”)"]
    # {{context}} filled with the trace input
    assert findings({"query": Q, "output": A, "context": Q}, RAG) == [
        "context is the run's input, not what it retrieved"]
    # Nothing, though the run retrieved two documents
    assert findings({"query": Q, "output": A, "documents": []}, RAG) == [
        "documents is empty, but the run's tools returned 2 results"]
    # A run with no tool results: its context may come from somewhere Assay doesn't see
    assert findings({"query": Q, "output": A, "context": ["Orders ship within 2 business days."]}, RUN) == []


def test_role_names_judges_use():
    assert roles({"Question": 1, "completion": 2, "ground_truth": 3, "rubric": 4}) == {
        "query": 1, "output": 2, "expected": 3, "instructions": 4}


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'a.db'}")))


def _ev(i, **kw):
    return {"v": 1, "id": f"e{i}", "ts": "2026-09-25T10:00:00Z", **kw}


def test_the_audit_endpoint_and_what_it_does_to_the_release_call(client):
    events, n = [], 0
    for run, (bad_before, bad_now) in {"before": (0, 0), "after": (0, 6)}.items():
        for i in range(12):
            rid, q, a = f"{run}-{i}", f"question {i} about order O-{i}", f"answer {i}: done for O-{i}"
            n += 1
            events += [_ev(f"{run}s{i}", type="run.start", run_id=rid, input=q, test={"run": run, "case": f"c{i}"}),
                       _ev(f"{run}a{i}", type="step", run_id=rid, seq=0, kind="answer", text=a),
                       _ev(f"{run}e{i}", type="run.end", run_id=rid)]
            wrong = i < bad_now
            events.append(_ev(f"{run}c{i}", type="check", run_id=rid, test={"run": run, "case": f"c{i}"},
                              field="helpful", evaluator="helpful@1", status="fail" if wrong else "pass",
                              inputs={"query": q, "generation": q if wrong else a}))
    assert client.post("/v1/ingest", json=events, headers={"X-Tenant": "t"}).status_code == 200

    a = client.get("/v1/evals/runs/after/audit", params={"source": "events:t"}).json()
    assert (a["audited"], a["suspect"]) == (12, 6)
    assert a["evaluators"][0]["evaluator"] == "helpful@1" and a["evaluators"][0]["share"] == 0.5
    assert "output is the run's input" in a["examples"][0]["findings"][1]

    # Six meaningless fails don't hold the release: they say nothing about the AI.
    st = client.get("/v1/evals/runs/after/stability", params={"source": "events:t", "baseline": "before"}).json()
    assert st["outcome"] == "advance" and st["roles"]["evaluator_input"] == 6
    assert any("judged on data that doesn't match the trace" in r for r in st["reasons"])
    causes = client.get("/v1/evals/runs/after/failures", params={"source": "events:t"}).json()["groups"]
    assert causes[0]["kind"] == "evaluator" and "was given the wrong data" in causes[0]["name"]
    assert client.get("/v1/evals/runs/nope/audit", params={"source": "events:t"}).status_code == 404


def test_the_audit_reads_the_runs_tool_results(client):
    events = [_ev("s", type="run.start", run_id="r", input=Q, test={"run": "rag", "case": "c"}),
              _ev("t", type="step", run_id="r", seq=0, kind="tool", name="search", args={"q": "refund"},
                  result=DOCS[1]),
              _ev("a", type="step", run_id="r", seq=1, kind="answer", text=A),
              _ev("e", type="run.end", run_id="r")]
    for i, ctx in enumerate(([DOCS[1]["policy"]], ["Orders ship within 2 business days."])):
        events.append(_ev(f"c{i}", type="check", run_id="r", test={"run": "rag", "case": "c"},
                          field=f"faithful{i}", evaluator="faithful@1", status="pass",
                          inputs={"query": Q, "output": A, "context": ctx}))
    assert client.post("/v1/ingest", json=events, headers={"X-Tenant": "t"}).status_code == 200
    a = client.get("/v1/evals/runs/rag/audit", params={"source": "events:t"}).json()
    assert (a["audited"], a["suspect"]) == (2, 1)
    assert a["examples"][0]["field"] == "faithful1"
    assert a["examples"][0]["findings"] == [
        "context isn't what the run retrieved (its tools returned “Damaged items are refunded in full within "
        "30 days of delivery.”)"]
