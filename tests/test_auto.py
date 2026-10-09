"""Attaching with a few lines (assay_sdk/auto.py): @assay.step, @assay.tool, assay.instrument()."""
import asyncio
import json
import sys
import types
from types import SimpleNamespace

import pytest

import assay_sdk as assay


@pytest.fixture
def events(tmp_path, monkeypatch):
    path = tmp_path / "events.jsonl"
    monkeypatch.delenv("ASSAY_URL", raising=False)
    assay.init(path=str(path))
    def read():
        assay.flush()
        return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []
    yield read
    assay.shutdown()


def test_steps_make_a_pipeline_run_without_any_run_code(events):
    @assay.step("ocr")
    def ocr(pages):
        return {"text": "INVOICE 17 total 27.61"}

    @assay.step("extract")
    def extract(text):
        return {"total": "27.61", "invoice_number": "17"}

    @assay.step("process", id_from="document_id")
    def process(document_id, pages):
        return extract(ocr(pages)["text"])

    assert process("doc-17", pages=2) == {"total": "27.61", "invoice_number": "17"}  # what it returns is unchanged
    ev = events()
    start = next(e for e in ev if e["type"] == "run.start")
    assert (start["run_id"], start["kind"], start["task"]) == ("doc-17", "pipeline", "process")
    stages = [e for e in ev if e["type"] == "step" and e["kind"] == "stage"]
    assert [s["name"] for s in stages] == ["ocr", "extract", "process"]  # each records when it ends
    assert stages[1]["outputs"] == {"total": "27.61", "invoice_number": "17"}
    assert stages[0]["parent_seq"] == stages[2]["seq"]  # nested inside the step that called it
    assert next(e for e in ev if e["type"] == "run.end")["status"] == "completed"


def test_a_failing_step_fails_the_run_and_still_raises(events):
    @assay.step
    def flaky():
        raise ValueError("bad page")
    with pytest.raises(ValueError):
        flaky()
    ev = events()
    assert next(e for e in ev if e["type"] == "step")["status"] == "error"
    assert next(e for e in ev if e["type"] == "run.end")["status"] == "failed"


def test_tools_record_into_the_run_in_progress_and_run_plainly_outside_one(events):
    @assay.tool
    def get_order(order_id, verbose=False):
        return {"status": "delivered"}

    @assay.tool("refund", server="shop")
    async def refund(order_id):
        raise RuntimeError("card declined")

    assert get_order("O-1") == {"status": "delivered"}  # no run: just runs
    assert events() == []
    with assay.run("support", test="c1"):
        get_order("O-17", verbose=True)
        with pytest.raises(RuntimeError):
            asyncio.run(refund("O-17"))
    tools = [e for e in events() if e["type"] == "step" and e["kind"] == "tool"]
    assert (tools[0]["name"], tools[0]["args"], tools[0]["result"]) == ("get_order", {"order_id": "O-17", "verbose": True},
                                                                         {"status": "delivered"})
    assert (tools[1]["name"], tools[1]["server"], tools[1]["status"]) == ("refund", "shop", "error")
    assert "card declined" in tools[1]["error"]


@pytest.fixture
def fake_sdks(monkeypatch):
    """Stand-ins for the anthropic and openai packages, at the module paths instrument() patches."""
    class Messages:
        def create(self, **kw):
            if kw.get("model") == "broken":
                raise RuntimeError("overloaded (529)")
            return SimpleNamespace(model="claude-opus-5", usage=SimpleNamespace(input_tokens=900, output_tokens=40),
                                   content=[SimpleNamespace(type="thinking", thinking=""),
                                            SimpleNamespace(type="text", text="Class: invoice")])

    class Completions:
        async def create(self, **kw):
            return SimpleNamespace(model="gpt-x", usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3),
                                   choices=[SimpleNamespace(message=SimpleNamespace(content="42"))])
    for name, attrs in (("anthropic", {}), ("anthropic.resources", {}), ("anthropic.resources.messages", {"Messages": Messages}),
                        ("openai", {}), ("openai.resources", {}), ("openai.resources.chat", {}),
                        ("openai.resources.chat.completions", {"AsyncCompletions": Completions})):
        monkeypatch.setitem(sys.modules, name, types.SimpleNamespace(__name__=name, **attrs))
    return Messages, Completions


def test_instrument_records_model_calls_in_the_step_they_happen_in(events, fake_sdks):
    Messages, Completions = fake_sdks
    done = assay.instrument()
    assert done == ["anthropic Messages.create", "openai AsyncCompletions.create"] and assay.instrument() == []

    assert Messages().create(model="claude-opus-5", messages=[]).model == "claude-opus-5"  # no run: nothing recorded
    assert events() == []

    @assay.step("classification")
    def classify(doc):
        return Messages().create(model="claude-opus-5", messages=[], tools=[{"name": "lookup", "input_schema": {}}])
    classify("doc")
    with assay.run("chat"):
        asyncio.run(Completions().create(model="gpt-x", messages=[]))
        with pytest.raises(RuntimeError):
            Messages().create(model="broken", messages=[])
        Messages().create(model="claude-opus-5", messages=[], stream=True)  # a stream is the caller's to read
    llm = [e for e in events() if e["type"] == "step" and e["kind"] == "llm"]
    assert len(llm) == 3
    assert (llm[0]["name"], llm[0]["model"], llm[0]["tokens_in"], llm[0]["tokens_out"], llm[0]["text"], llm[0]["tools"]) == \
        ("classification", "claude-opus-5", 900, 40, "Class: invoice", ["lookup"])
    assert (llm[1]["model"], llm[1]["tokens_in"], llm[1]["text"]) == ("gpt-x", 12, "42")
    assert llm[2]["status"] == "error" and "overloaded" in llm[2]["error"]
