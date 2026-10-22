"""Retrieval that failed quietly: assay.faults() breaks a retriever like a tool, handles_failure()
checks the answer didn't go on as if it had the evidence, and must_retrieve() checks a case
searched where its evidence is."""
import asyncio

import pytest

import assay_sdk as assay
from assay import schema
from assay_sdk.auto import _wrap_retriever
from assay_sdk.faults import ToolFault
from assay_sdk.testing import expect

DOCS = [{"id": "kb-1", "text": "Refunds take 30 days.", "source": "refund-policy.md"}]


@pytest.fixture
def sent():
    out = []
    assay.init(transport=lambda b: out.extend(b), flush_interval=60)
    yield out
    assay.init(enabled=False)


def search(q):
    return list(DOCS)


def rag(run, q, tries=1, honest=True):
    """Searches the policies, retrying a failed search; answers from what it found."""
    found = []
    for i in range(tries):
        try:
            found = run.search("policies", search, q)
            if found:
                break
        except ToolFault:
            pass
    if found:
        run.answer(found[0]["text"])
    else:
        run.answer("I couldn't find anything about refunds in our policies." if honest else "Refunds take 14 days.")


def test_a_retriever_broken_on_purpose(sent):
    with assay.run("rag", input="refunds?") as r:
        with assay.faults(policies="empty"):
            rag(r, "refunds?", honest=False)
    step = next(s for s in r.steps if s["kind"] == "retrieval")
    assert (step["name"], step["fault"], step["fragments"]) == ("policies", "empty", [])
    assert expect(r).handles_failure().failures() == [
        "handles_failure(2): The policies retrieval found nothing (step 0), but the answer goes on as if it had "
        "the evidence: 'Refunds take 14 days.'."]
    with assay.run("rag", input="refunds?") as told:
        with assay.faults(policies="error"):
            rag(told, "refunds?")
    step = next(s for s in told.steps if s["kind"] == "retrieval")
    assert step["status"] == "error" and step["fault"] == "error" and "injected" in step["error"]
    assert expect(told).handles_failure().failures() == []  # it said it couldn't find it
    with assay.run("rag", input="refunds?") as retried:
        with assay.faults(policies="error:1"):  # fails once, then works: a retry recovers
            rag(retried, "refunds?", tries=2, honest=False)
    assert expect(retried).handles_failure().failures() == []
    with assay.run("rag", input="refunds?") as looped:
        with assay.faults(policies="empty"):
            rag(looped, "refunds?", tries=5)
    assert "The policies retrieval ran 5 times after it failed" in expect(looped).handles_failure().failures()[0]
    assay.flush()
    ev = next(x for x in sent if x["type"] == "step" and x.get("kind") == "retrieval")
    assert ev["fault"] == "empty"  # sent, and the server's schema takes it


def test_an_empty_retrieval_counts_without_a_fault(sent):
    with assay.run("rag", input="warranty?") as r:
        r.search("policies", lambda q: [], "warranty?")
        r.answer("The warranty lasts two years.")
    assert "found nothing" in expect(r).handles_failure().failures()[0]


def test_must_retrieve(sent):
    with assay.run("rag", input="refunds?") as r:
        rag(r, "refunds?")
    assert expect(r).must_retrieve("policies").must_retrieve(source="refund-policy").failures() == []
    assert expect(r).must_retrieve("contracts").failures() == [
        "must_retrieve(contracts): No retrieval from contracts ran (retrievals: policies)."]
    assert expect(r).must_retrieve(source="warranty").failures() == [
        "must_retrieve(source='warranty'): Nothing from 'warranty' was retrieved; it found: refund-policy.md."]
    with assay.run("rag", input="refunds?") as empty:
        with assay.faults(policies="empty"):
            rag(empty, "refunds?")
    assert expect(empty).must_retrieve("policies").failures() == [
        "must_retrieve(policies): policies ran, but failed or found nothing every time."]


class Retriever:  # stands in for a LangChain BaseRetriever
    name = "kb"

    def invoke(self, q):
        return list(DOCS)

    async def ainvoke(self, q):
        return list(DOCS)


def test_langchain_and_llamaindex_retrievers_break_by_name(sent):
    Retriever.invoke = _wrap_retriever(Retriever.invoke)
    Retriever.ainvoke = _wrap_retriever(Retriever.ainvoke)
    with assay.run("rag") as r:
        assert len(Retriever().invoke("refunds?")) == 1
        with assay.faults(kb="empty"):
            assert Retriever().invoke("refunds?") == []
            assert asyncio.run(Retriever().ainvoke("refunds?")) == []
        with assay.faults(kb="timeout"), pytest.raises(TimeoutError):
            Retriever().invoke("refunds?")
    rets = [s for s in r.steps if s["kind"] == "retrieval"]
    assert [(s["name"], s["fault"], len(s["fragments"]), s["status"]) for s in rets] == [
        ("kb", None, 1, "ok"), ("kb", "empty", 0, "ok"), ("kb", "empty", 0, "ok"), ("kb", "timeout", 0, "error")]


def test_a_lost_reply_on_an_async_tool_still_runs_it(sent):
    paid = []

    @assay.tool
    async def pay(order_id):
        paid.append(order_id)

    async def go():
        with assay.faults(pay="lost"), pytest.raises(TimeoutError):
            await pay("O-17")
    with assay.run("ap") as r:
        asyncio.run(go())
    assert paid == ["O-17"] and r.steps[0]["fault"] == "lost"  # it ran; only the reply was lost


def test_the_server_takes_every_fault():
    base = {"type": "step", "run_id": "r", "seq": 0, "v": 1, "id": "x", "ts": "2026-10-22T00:00:00Z"}
    for fault in ("empty", "error", "timeout", "lost", "return"):
        schema.EVENTS.validate_python([{**base, "kind": "tool", "name": "pay", "fault": fault}])
    schema.EVENTS.validate_python([{**base, "kind": "retrieval", "name": "kb", "fragments": [], "fault": "empty"}])
