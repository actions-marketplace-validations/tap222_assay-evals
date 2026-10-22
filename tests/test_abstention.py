"""Saying "the sources don't say": a case whose sources don't answer its question expects an
abstention, and the report counts abstentions apart from the other checks."""
import sys

import pytest

import assay_sdk as assay
from assay import local
from assay.__main__ import main
from assay_sdk.checks import abstains
from assay_sdk.testing import expect
from test_local import config, project  # noqa: F401  (the fixture)


@pytest.fixture
def sent():
    out = []
    assay.init(transport=lambda b: out.extend(b), flush_interval=60)
    yield out
    assay.init(enabled=False)


def test_what_counts_as_saying_the_sources_dont_answer():
    for said in ["The documents don't say how long the warranty lasts.",
                 "I couldn't find anything about refunds in the policy.",
                 "The provided context does not mention a fee.",
                 "This is not covered in the sources.",
                 "There is no information about the Q3 budget in these documents.",
                 "I’m unable to answer that from the sources."]:
        assert abstains(said), said
    for answer in ["The warranty lasts two years.",
                   "The policy doesn't cover water damage.",  # an answer: what the policy says
                   "No, a counter can't go down.",
                   "There is no fee for returns.",
                   "I don't have access to your account, but refunds take 30 days."]:
        assert abstains(answer) is None, answer
    assert abstains("Refunds take 30 days. The documents don't mention exchanges.") == \
        "The documents don't mention exchanges."


def test_must_abstain_and_must_not_abstain(sent):
    with assay.run("rag", input="How long is the warranty?") as r:
        r.answer("The warranty lasts two years.")
    assert expect(r).must_abstain().failures() == [
        "must_abstain(): The sources don't answer this, but the answer doesn't say so: "
        "'The warranty lasts two years.'."]
    with assay.run("rag", input="How long is the warranty?") as r2:
        r2.answer("The documents don't say how long the warranty lasts.")
    assert expect(r2).must_abstain().failures() == []
    assert expect(r2).must_not_abstain().failures() == [
        "must_not_abstain(): The answer says the sources don't answer this "
        "(“The documents don't say how long the warranty lasts.”), but they do."]
    with assay.run("rag", input="?") as r3:
        pass
    assert "gave no answer" in expect(r3).must_abstain().failures()[0]


def test_counted_apart_in_the_report():
    f = lambda field, passed, total: {"field": field, "passed": passed, "total": total}
    assert local.abstention_text([f("answer", 3, 3)]) is None
    assert local.abstention_text([f(local.ABSTAIN, 4, 4), f(local.ANSWER, 6, 6)]) == \
        "4/4 rightly said the sources don't answer · 6/6 answered when the sources do"
    assert local.abstention_text([f(local.ABSTAIN, 3, 5), f(local.ANSWER, 5, 6)]) == \
        "3/5 rightly said the sources don't answer · 2 answered anyway · 1/6 said so when the sources do answer"


SUITE = '''
import pytest
from assay_sdk.testing import expect

ANSWERS = {"warranty": "The documents don't say how long the warranty lasts.",
           "fee": "There is a $5 fee.",
           "refund": "Refunds take 30 days.",
           "exchange": "I couldn't find anything about exchanges in the policy."}

@pytest.mark.parametrize("q", ["warranty", "fee"])
def test_unanswerable(assay_case, q):
    expect(assay_case).must_abstain()
    assay_case.answer(ANSWERS[q])

@pytest.mark.parametrize("q", ["refund", "exchange"])
def test_answerable(assay_case, q):
    expect(assay_case).must_not_abstain()
    assay_case.answer(ANSWERS[q])
'''


def test_assay_test_reports_unanswerable_questions(project, capsys):  # noqa: F811
    (project / "test_rag.py").write_text(SUITE, encoding="utf-8")
    config(project, f"{sys.executable} -m pytest -q -p no:cacheprovider test_rag.py")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "Unanswerable questions\n  1/2 rightly said the sources don't answer · 1 answered anyway · " \
           "1/2 said so when the sources do answer" in out
    assert "Says the sources don't answer  1/2" in out
