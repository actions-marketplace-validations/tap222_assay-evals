"""OpenInference spans over OTLP (assay/ingest.py openinference): a Phoenix- or traceAI-style trace
becomes an agent run with its model calls, retrieval, tools, answer, conversation and user."""
import json

import pytest
from fastapi.testclient import TestClient

from assay import ingest
from assay.api import create_app
from assay.config import Settings

T0 = 1_790_000_000_000_000_000


def attr(k, v):
    key = "intValue" if isinstance(v, int) and not isinstance(v, bool) else "doubleValue" if isinstance(v, float) \
        else "boolValue" if isinstance(v, bool) else "stringValue"
    return {"key": k, "value": {key: str(v) if key == "intValue" else v}}


def span(sid, parent, name, start, attrs, error=False):
    return {"traceId": "t" * 32, "spanId": sid, "parentSpanId": parent, "name": name,
            "startTimeUnixNano": str(T0 + start * 10**9), "endTimeUnixNano": str(T0 + (start + 1) * 10**9),
            "attributes": [attr(k, v) for k, v in attrs.items()], "status": {"code": 2 if error else 1}}


def payload():
    spans = [
        span("root", None, "support-agent", 0, {"openinference.span.kind": "AGENT", "session.id": "chat-7",
                                                 "user.id": "u-42", "input.value": "Where is order O-17?",
                                                 "output.value": "O-17 is out for delivery; it arrives tomorrow."}),
        span("llm1", "root", "ChatCompletion", 1, {
            "openinference.span.kind": "LLM", "llm.model_name": "gpt-5", "llm.provider": "openai",
            "llm.token_count.prompt": 1800, "llm.token_count.completion": 40, "llm.cost.total": 0.004,
            "llm.prompt_template.version": "7",
            "llm.input_messages.0.message.role": "system", "llm.input_messages.0.message.content": "s" * 4000,
            "llm.input_messages.1.message.role": "user", "llm.input_messages.1.message.content": "Where is order O-17?",
            "llm.output_messages.0.message.role": "assistant",
            "llm.output_messages.0.message.content": "Let me look it up."}),
        span("ret", "root", "kb-search", 2, {
            "openinference.span.kind": "RETRIEVER", "input.value": "order O-17 delivery",
            "retrieval.documents.0.document.id": "kb-1", "retrieval.documents.0.document.score": 0.9,
            "retrieval.documents.0.document.content": "Orders ship in 2 days.",
            "retrieval.documents.1.document.id": "kb-2", "retrieval.documents.1.document.content": "Refunds take 10 days."}),
        span("tool", "root", "get_order", 3, {"openinference.span.kind": "TOOL", "tool.name": "get_order",
                                              "input.value": json.dumps({"order_id": "O-17"}),
                                              "output.value": json.dumps({"status": "out_for_delivery"})}),
    ]
    return {"resourceSpans": [{"resource": {"attributes": [attr("service.name", "support")]},
                               "scopeSpans": [{"spans": spans}]}]}


def test_an_openinference_trace_becomes_an_agent_run(tmp_path):
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}")))
    r = c.post("/v1/otlp/v1/traces", json=payload(), headers={"X-Tenant": "p", "Content-Type": "application/json"})
    assert r.status_code == 200, r.text
    t = c.get(f"/v1/agents/trajectories/{'t' * 32}", params={"source": "events:p"}).json()
    kinds = [s["kind"] for s in t["steps"]]
    assert kinds == ["reason", "retrieval", "tool", "answer"]
    llm, ret, tool, ans = t["steps"]
    assert (llm["model"], llm["tokens_in"], llm["tokens_out"], llm["cost_usd"]) == ("gpt-5", 1800, 40, 0.004)
    assert llm["context"]["system"] == 1000 and llm["prompt"] == "ChatCompletion@7"
    assert [f["id"] for f in ret["result"]] == ["kb-1", "kb-2"] and ret["args"]["query"] == "order O-17 delivery"
    assert (tool["name"], tool["args"], tool["result"]) == ("get_order", {"order_id": "O-17"},
                                                           {"status": "out_for_delivery"})
    assert ans["text"].startswith("O-17 is out for delivery")
    assert (t["task"], t["conversation_id"], t["status"]) == ("support-agent", "chat-7", "completed")
    from assay import learn, store
    engine = store.make_engine(f"sqlite:///{tmp_path / 's.db'}")
    assert learn._inputs(engine, "p", ["t" * 32])["t" * 32]["input"] == "Where is order O-17?"
    from sqlalchemy import select
    with engine.connect() as conn:
        assert conn.execute(select(store.agent_trajectories.c.user_id)).scalar() == "u-42"


def test_gen_ai_spans_are_read_as_before_and_attributes_are_never_overwritten():
    a = {"openinference.span.kind": "LLM", "llm.model_name": "gpt-5", "gen_ai.request.model": "claude-opus-5"}
    assert ingest.openinference(a)["gen_ai.request.model"] == "claude-opus-5"  # the span's own attribute wins
    plain = {"gen_ai.tool.name": "x"}
    assert ingest.openinference(plain) is plain  # not OpenInference: untouched
