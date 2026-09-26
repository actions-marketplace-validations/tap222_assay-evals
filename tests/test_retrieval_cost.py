"""Retrieved context and what it costs: run.retrieve(), per-query and whole-run checks, limits, prices."""
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from assay import behavior, local, schema
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")

AGENT = '''
import os
import assay_sdk as assay
n, prompt = {"before": (3, 800), "creep": (4, 1100), "dump": (12, 3000)}[os.environ["MODE"]]
assay.init()
for i in range(5):
    with assay.run("support", test=f"q{i}") as r:
        q = f"refund policy {i}"
        r.retrieve(q, [{"id": f"kb-{k}", "text": "x" * 400, "score": 1 - k / 20} for k in range(n)])
        r.llm(model="claude-opus-5", tokens_in=prompt, tokens_out=50)
        r.answer("Refunds take 5 days.")
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_PRICES", "ASSAY_POLICY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "agent.py").write_text(AGENT)

    def toml(extra=""):
        (tmp_path / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n{extra}')
    toml()
    return toml


def run(monkeypatch, capsys, mode):
    monkeypatch.setenv("MODE", mode)
    code = main(["test"])
    return code, capsys.readouterr().out


def test_every_query_a_little_bigger_fails_the_run_on_its_totals(project, monkeypatch, capsys):
    """The bill tripled and each query looked normal: no case passes its own ratio, the run does."""
    assert run(monkeypatch, capsys, "before")[0] == 0
    code, out = run(monkeypatch, capsys, "creep")
    assert code == 1, out
    assert "behaved worse than their baseline" not in out  # no single case crossed its ratio
    assert "Input tokens for the whole run: 4,000 → 5,500 (1.4×), over the 5 cases in both runs" in out
    assert "Retrieved tokens for the whole run: 1,500 → 2,000 (1.3×)" in out
    junit = Path(".assay").glob("*.xml")
    assert not list(junit)


def test_a_query_that_dumps_everything_into_the_prompt_is_named(project, monkeypatch, capsys):
    assert run(monkeypatch, capsys, "before")[0] == 0
    code, out = run(monkeypatch, capsys, "dump")
    assert code == 1
    assert "Retrieved context (retrieve): 3 fragments, 300 tokens → 12 fragments, 1,200 tokens (40% of the prompt)" in out
    assert "Context: 800 tokens → 3,000 tokens (3.8×)" in out
    assert "Input tokens: 800" not in out  # one model call: the same number as the context

    assert main(["diff"]) == 1
    diff = capsys.readouterr().out
    assert "WHOLE-RUN TOTALS" in diff and "Input tokens for the whole run: 4,000 → 15,000 (3.8×)" in diff
    assert "Retrieved context (retrieve)" in diff
    assert main(["diff", "--format", "markdown"]) == 1
    assert "### Whole-run totals" in capsys.readouterr().out


def test_limits_hold_without_a_baseline(project, monkeypatch, capsys):
    project("[behavior]\nmax_fragments = 8\nmax_context_tokens = 2500\n")
    code, out = run(monkeypatch, capsys, "dump")
    assert code == 1, out
    assert "retrieve (step 0) put 12 fragments in the prompt, over the limit of 8" in out
    assert "the model call at step 1 got 3,000 tokens of input, over the limit of 2,500" in out
    assert run(monkeypatch, capsys, "before")[0] == 0  # 3 fragments and 800 tokens: within both


def test_prices_give_recorded_calls_their_cost(project, monkeypatch, capsys):
    project('[prices]\n"claude-opus-5" = [5, 25]\n')
    run(monkeypatch, capsys, "before")
    events = [json.loads(x) for f in Path(".assay/runs").glob("*.jsonl") for x in f.read_text().splitlines()]
    llm = [e for e in events if e.get("kind") == "llm"]
    assert llm and all(e["cost_usd"] == pytest.approx((800 * 5 + 50 * 25) / 1e6) for e in llm)
    frags = [e for e in events if e.get("kind") == "retrieval"][0]["fragments"]
    assert [f["tokens"] for f in frags] == [100, 100, 100] and all(f["used"] and f["estimated"] for f in frags)


def test_the_whole_run_check_can_be_loosened_only_in_the_open(project):
    base = local.load_config(Path("."))
    assert base["behavior"]["suite"] == 1.25 and base["behavior"]["limits"] == {}
    pr = {**base, "behavior": {**base["behavior"], "suite": 0.0, "limits": {"max_fragments": 20}}}
    tight = {**base, "behavior": {**base["behavior"], "limits": {"max_fragments": 8}}}
    changes = local.policy_changes(tight, pr)
    assert {"text": "turns the whole-run totals check off", "weakens": True} in changes
    assert {"text": "raises the limit on fragments per query from 8 to 20", "weakens": True} in changes
    held = local.strictest(tight, pr)["behavior"]
    assert held["suite"] == 1.25 and held["limits"] == {"max_fragments": 8}
    with pytest.raises(local.SetupError, match="max_fragmnets"):
        local._behavior_config({"max_fragmnets": 8})
    with pytest.raises(local.SetupError, match="prices"):
        local._prices_config({"claude": "cheap"})


# ---------- pieces ----------

def test_fragments_in_every_shape():
    from assay_sdk.retrieval import fragments, summary
    doc = N(page_content="a" * 40, metadata={"id": "d1", "source": "kb/refunds.md"})
    node = N(node=N(get_content=lambda: "b" * 80, node_id="n1", metadata={}), score=0.7)
    fs = fragments(["plain text", doc, node, (N(page_content="c", metadata={}), 0.2),
                    {"id": "x", "text": "d", "tokens": 900}], used=["d1", "n1", 4])
    assert [f["id"] for f in fs] == ["0", "d1", "n1", "3", "x"]
    assert [f["tokens"] for f in fs] == [3, 10, 20, 1, 900] and fs[1]["source"] == "kb/refunds.md"
    assert fs[2]["score"] == 0.7 and fs[3]["score"] == 0.2
    assert [f["used"] for f in fs] == [False, True, True, False, True]
    assert summary(fs) == {"retrieved": 5, "used": 3, "tokens_retrieved": 934, "tokens_used": 930}
    assert [f["used"] for f in fragments(["a", "b", "c"], used=2)] == [True, True, False]


def test_a_retrieval_step_sent_to_the_api():
    e = schema.EVENTS.validate_python([{"v": 1, "type": "step", "id": "s1", "ts": "2026-09-26T10:00:00Z", "run_id": "r",
                                        "seq": 0, "kind": "retrieval", "name": "search", "query": "refunds",
                                        "fragments": [{"text": "x" * 40}, {"id": "k2", "tokens": 50, "used": False}]}])[0]
    assert [(f["tokens"], f["used"]) for f in e.fragments] == [(10, True), (50, False)]
    assert schema.retrieval_args(e.query, e.fragments) == {"query": "refunds", "retrieved": 2, "used": 1,
                                                          "tokens_retrieved": 60, "tokens_used": 10}
    with pytest.raises(Exception, match="fragments"):
        schema.EVENTS.validate_python([{"v": 1, "type": "step", "id": "s2", "ts": "2026-09-26T10:00:00Z",
                                        "run_id": "r", "seq": 0, "kind": "retrieval"}])


def test_suite_totals_and_limits():
    before = {f"c{i}": {"input_tokens": 800, "cost_usd": 0.004} for i in range(5)}
    now = {f"c{i}": {"input_tokens": 1100, "cost_usd": 0.0055} for i in range(5)}
    cases = sorted(before)
    got = behavior.suite_totals(now, before, cases)
    assert [x["metric"] for x in got] == ["input_tokens"]  # cost grew $0.0075 in all: under the minimum
    assert behavior.suite_totals(now, before, cases, 0) == [] and behavior.suite_totals(now, before, cases, 1.5) == []
    assert behavior.suite_totals(now, before, cases[:2]) == []  # 600 more tokens over 2 cases: too little to say
    traj = {"steps": [{"seq": 0, "kind": "retrieval", "name": "kb", "args": {"used": 9, "tokens_used": 4000}},
                      {"seq": 1, "kind": "reason", "tokens_in": 5000}]}
    lim = {r["field"]: r for r in behavior.limits(traj, {"max_fragments": 8, "max_retrieved_tokens": 5000})}
    assert lim["max_fragments"]["status"] == "fail" and lim["max_retrieved_tokens"]["status"] == "pass"
    assert behavior.measure({**traj, "outcome": None})["context_share"] == 0.8


def test_instrument_records_what_a_langchain_retriever_returned(tmp_path, monkeypatch):
    import assay_sdk as assay

    class BaseRetriever:
        name = None

        def invoke(self, input, config=None, **kw):
            return self._get(input)

    class KB(BaseRetriever):
        def _get(self, q):
            return [N(page_content="refunds take 5 days " * 10, metadata={"id": "kb-1"}),
                    N(page_content="damaged items " * 5, metadata={"id": "kb-2"})]

    class Ensemble(BaseRetriever):  # wraps another retriever: one retrieval, not two
        def _get(self, q):
            return KB().invoke(q)[:1]
    mod = types.ModuleType("langchain_core.retrievers")
    mod.BaseRetriever = BaseRetriever
    monkeypatch.setitem(sys.modules, "langchain_core", types.ModuleType("langchain_core"))
    monkeypatch.setitem(sys.modules, "langchain_core.retrievers", mod)
    assert "langchain_core BaseRetriever.invoke" in assay.instrument()
    path = tmp_path / "e.jsonl"
    monkeypatch.delenv("ASSAY_URL", raising=False)
    assay.init(path=str(path))
    with assay.run("x"):
        KB().invoke("how long do refunds take?")
        Ensemble().invoke("damaged?")
    assay.shutdown()
    steps = [json.loads(x) for x in path.read_text().splitlines() if '"retrieval"' in x]
    assert [(s["name"], s["query"], len(s["fragments"])) for s in steps] == [
        ("KB", "how long do refunds take?", 2), ("Ensemble", "damaged?", 1)]
    assert steps[0]["fragments"][0]["id"] == "kb-1" and steps[0]["fragments"][0]["tokens"] == 50
