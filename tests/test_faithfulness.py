"""RAG faithfulness (assay_sdk/faithfulness.py): claim by claim against the fragments, with the judge's
evidence checked, and context relevance apart."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from assay_sdk import Judge, faithfulness

FRAGS = [{"id": "kb-1", "text": "Refunds take 10 business days from approval."},
         {"id": "kb-2", "text": "Shipping is free on orders over $50."},
         {"id": "kb-3", "text": "Our office is in Berlin."}]
ANSWER = "Refunds take 5 days. Shipping is free on orders over $50. You will also get a coupon."
GOOD = {"claims": [
    {"claim": "Refunds take 5 days", "verdict": "contradicted", "fragment": "kb-1", "evidence": "Refunds take 10 business days"},
    {"claim": "Shipping is free on orders over $50", "verdict": "supported", "fragment": "kb-2",
     "evidence": "Shipping is free on orders over $50"},
    {"claim": "You will also get a coupon", "verdict": "fabricated"}],
    "fragments": [{"id": "kb-1", "relevant": True}, {"id": "kb-2", "relevant": True}, {"id": "kb-3", "relevant": False}]}


class Fake:
    def __init__(self, *answers):
        self.answers, self.messages, self.calls = list(answers), self, []

    def create(self, **kw):
        self.calls.append(kw)
        a = self.answers.pop(0)
        return N(model="claude-opus-5", stop_reason="end_turn", content=[N(type="text", text=json.dumps(a))])


def judge(*answers):
    return Judge("anthropic", "claude-opus-5", client=Fake(*answers))


def test_claim_by_claim_with_the_worst_naming_the_failure():
    out = faithfulness(judge(GOOD), "How long do refunds take?", ANSWER, FRAGS)
    f, c = out["faithfulness"], out["context_relevance"]
    assert (f.status, round(f.score, 2), f.category) == ("FAIL", 0.33, "contradicts_source")
    assert "1 of 3 claims supported" in f.reason and "contradicted: “Refunds take 5 days” vs kb-1" in f.reason
    assert "fabricated: “You will also get a coupon”" in f.reason
    assert (c.status, round(c.score, 2)) == ("PASS", 0.67) and "not: kb-3" in c.reason
    assert [x["verdict"] for x in out["claims"]] == ["contradicted", "supported", "fabricated"]


def test_evidence_that_isnt_in_the_fragment_isnt_counted():
    misquoted = {**GOOD, "claims": [
        {"claim": "Refunds take 5 days", "verdict": "supported", "fragment": "kb-1", "evidence": "Refunds take 5 days"},
        *GOOD["claims"][1:]]}
    f = faithfulness(judge(misquoted), "q", ANSWER, FRAGS)["faithfulness"]
    assert f.score == 0.5 and "(1 claim the judge listed didn't check out: not counted)" in f.reason
    invented = {"claims": [{"claim": "Refunds are instant", "verdict": "supported", "fragment": "kb-1",
                            "evidence": "instant"}] * 3, "fragments": GOOD["fragments"]}
    f = faithfulness(judge(invented, invented), "q", ANSWER, FRAGS)["faithfulness"]
    assert (f.status, f.error_kind, f.attempts) == ("INVALID", "invalid", 2)  # asked again, then never a score
    assert "none of the claims the judge listed check out" in f.error


def test_nothing_to_be_faithful_to_and_recording_on_a_run():
    assert faithfulness(judge(), "q", ANSWER, [])["faithfulness"].status == "INVALID"
    seen = []

    class Run:
        def check(self, field, status, **kw):
            seen.append((field, status, kw.get("category"), kw.get("judge_model"), kw.get("evaluator")))
    faithfulness(judge(GOOD), "q", ANSWER, FRAGS, run=Run())
    assert seen == [("faithfulness", "fail", "contradicts_source", "claude-opus-5", "assay.faithfulness@1"),
                    ("context_relevance", "pass", None, "claude-opus-5", "assay.faithfulness@1")]


def test_the_built_in_judge_checks_faithfulness_for_runs_that_retrieved(tmp_path, monkeypatch, capsys):
    from assay import judge as judge_mod
    from assay.__main__ import main
    sdk = str(Path(__file__).resolve().parents[1] / "sdk" / "python")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", sdk)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "agent.py").write_text(f'''
import assay_sdk as assay
assay.init()
with assay.run("support", test="refund", input="How long do refunds take?") as r:
    r.retrieve("How long do refunds take?", {json.dumps(FRAGS)})
    r.answer({json.dumps(ANSWER)})
''', encoding="utf-8")
    (tmp_path / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n', encoding="utf-8")
    consistency = {"consistency": {"applicable": True, "score": 4, "reason": "fine"},
                   "plan_quality": {"applicable": False, "score": 1, "reason": "no plan"}}

    class Both(Fake):
        def create(self, **kw):  # the consistency judge and the faithfulness judge share the client
            system = json.dumps(kw.get("system"), default=str)
            a = GOOD if "faithful to the context" in system else consistency
            return N(model="claude-opus-5", stop_reason="end_turn", content=[N(type="text", text=json.dumps(a))])
    monkeypatch.setattr(judge_mod, "_client", lambda: Both())
    assert main(["test", "--judge"]) == 1
    out = capsys.readouterr().out
    assert "Faithfulness" in out and "1 of 3 claims supported" in out
    assert "Failures by kind\n  1 contradicts source" in out
    assert "couldn't be judged" not in out  # the judge's context is the run's own fragments: the audit agrees


def test_a_long_fragment_is_split_and_shown_whole_not_cut():
    fm = sys.modules["assay_sdk.faithfulness"]
    filler = "Section 1 sets out definitions that apply throughout this Part. " * 47  # ~3,000 characters
    statute = filler + "\n\n" + filler + "\n\nA notice must be given at least 28 days before the tenancy ends."
    ps = fm.parts(statute)
    assert "".join(ps) == statute and len(ps) == 2 and all(len(p) <= fm.MAX_PART for p in ps)
    assert ps[0].endswith("\n\n")  # at the paragraph, not mid-sentence
    answer = "Notice must be given at least 28 days before the tenancy ends."
    verdict = {"claims": [{"claim": answer.rstrip("."), "verdict": "supported", "fragment": "s21",
                           "evidence": "A notice must be given at least 28 days before the tenancy ends"}],
               "fragments": [{"id": "s21", "relevant": True}]}
    client = Fake(verdict)
    out = faithfulness(Judge("anthropic", "claude-opus-5", client=client), "How much notice?", answer,
                       [{"id": "s21", "text": statute}])
    prompt = json.dumps(client.calls[0]["messages"])
    assert "28 days before the tenancy ends" in prompt and 'part=\\"2 of 2\\"' in prompt
    assert (out["faithfulness"].status, out["faithfulness"].score) == ("PASS", 1.0)


def test_claims_text_the_judge_never_saw_could_support_are_not_called_fabricated(monkeypatch):
    fm = sys.modules["assay_sdk.faithfulness"]
    monkeypatch.setattr(fm, "MAX_CONTEXT", 60)
    frags = [{"id": "kb-1", "text": "Shipping is free on orders over $50."},
             {"id": "kb-2", "text": "Refunds take 10 business days from approval. " * 5}]
    answer = "Shipping is free on orders over $50. Refunds come with a coupon."
    both = {"claims": [{"claim": "Shipping is free on orders over $50", "verdict": "supported", "fragment": "kb-1",
                        "evidence": "Shipping is free on orders over $50"},
                       {"claim": "Refunds come with a coupon", "verdict": "fabricated"}],
            "fragments": [{"id": "kb-1", "relevant": True}, {"id": "kb-2", "relevant": True}]}
    f = faithfulness(judge(both), "q", answer, frags)["faithfulness"]
    assert (f.status, f.score) == ("PASS", 1.0)
    assert "couldn't judge 1 claim: kb-2 longer than the judge was shown" in f.reason
    only = {**both, "claims": both["claims"][1:]}
    out = faithfulness(judge(only), "q", answer, frags)  # one try: asking again shows the judge no more
    assert (out["faithfulness"].status, out["faithfulness"].attempts) == ("INVALID", 1)
    assert "couldn't judge" in out["faithfulness"].error and "fabricated" not in out["faithfulness"].error
    assert out["context_relevance"].status == "PASS"


def test_a_fragment_recorded_on_a_run_at_its_2000_character_limit_counts_as_not_seen_in_full():
    from assay_sdk.retrieval import MAX_TEXT, fragments
    long = "The landlord must protect the deposit within 30 days. " * 60
    assert len(fragments([long])[0]["text"]) == MAX_TEXT and fragments([long], max_text=None)[0]["text"] == long
    recorded = fragments([{"id": "s21", "text": long}])
    answer = "The deposit must be protected within 30 days. It earns interest."
    verdict = {"claims": [{"claim": "The deposit must be protected within 30 days", "verdict": "supported",
                           "fragment": "s21", "evidence": "must protect the deposit within 30 days"},
                          {"claim": "It earns interest", "verdict": "fabricated"}],
               "fragments": [{"id": "s21", "relevant": True}]}
    f = faithfulness(judge(verdict), "q", answer, recorded)["faithfulness"]
    assert (f.status, f.score) == ("PASS", 1.0) and "couldn't judge 1 claim: s21" in f.reason
    f = faithfulness(judge(verdict), "q", answer, [{"id": "s21", "text": long}])["faithfulness"]
    assert (f.status, f.score) == ("FAIL", 0.5)  # passed in whole: the judge saw it all, so fabricated stands
