"""Separating a judge's bias from its signal: catch rate, bias probes, same-family judging, score rises
that come with the answers' surface, and labeling where judges disagree."""
import json
import sys
from pathlib import Path

import pytest

from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")

JUDGE = '''
import re
from assay_sdk.llm import Response
def grade(question, answer):
    q = int(re.search(r"quality=(\\d)", answer).group(1))
    if "[1]" in answer:
        q = min(5, q + 2)  # impressed by a citation
    return Response(text="x", structured={"score": q}, model="claude-opus-5")
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    return tmp_path


def test_a_judge_that_rewards_citations_is_caught_by_the_probes(project, capsys):
    (project / "judges.py").write_text(JUDGE)
    (project / "assay.toml").write_text('[test]\ncommand = "true"\n[calibrate]\njudge = "judges.py:grade"\nrepeat = 1\n'
                                        'threshold = 3\n')
    items = [{"id": f"i{i}", "input": "q", "output": f"answer quality={i % 5 + 1}" + (" as shown in [1]" if i % 2 else ""),
              "score": i % 5 + 1, "by": "sam"} for i in range(30)]
    (project / "golden.jsonl").write_text("\n".join(json.dumps(x) for x in items) + "\n")
    assert main(["calibrate"]) == 0
    out = capsys.readouterr().out
    assert "Bias probe   answers with citations or links score" in out and "more than people scored them" in out, out
    assert "Catch rate   fails" in out and "it confirms good answers but lets bad ones through" in out


FAMILY = '''
import os
import assay_sdk as assay
assay.init()
long = os.environ.get("LONG") == "1"
for i in range(4):
    with assay.run("support", test=f"q{i}") as r:
        r.llm(model="claude-sonnet-5", tokens_in=100, tokens_out=10)
        text = ("## Answer\\n- " + "a detailed point " * 12 + "\\n- another point") if long else "Five days."
        r.answer(text)
        r.check("helpful", "pass", score=0.9 if long else 0.7, judge_model="claude-opus-5")
        if os.environ.get("SECOND") == "1":
            r.check("helpful_gpt", "pass", score=0.9 if i == 2 else 0.1 if i == 1 else 0.85, judge_model="gpt-5")
            r.check("answer", "fail" if i == 3 else "pass", expected="5 days", actual="Five days.")
'''


def test_same_family_judging_and_a_score_that_rose_with_the_length(project, monkeypatch, capsys):
    (project / "agent.py").write_text(FAMILY)
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n')
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "the judge and the answers are both anthropic models, and judges favor their own family" in out
    monkeypatch.setenv("LONG", "1")
    main(["test"])
    out = capsys.readouterr().out
    assert "helpful rose 0.20 on average over 4 cases, and the answers are" in out and "longer" in out
    assert "formatted with headers or bullets" in out and "what the judge rewards besides quality" in out


def test_suggest_where_the_judges_disagree(project, monkeypatch, capsys):
    (project / "agent.py").write_text(FAMILY)
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n')
    monkeypatch.setenv("SECOND", "1")
    main(["test"])
    capsys.readouterr()
    assert main(["golden", "suggest", "--field", "helpful", "--vs", "helpful_gpt", "-n", "3"]) == 0
    out = capsys.readouterr().out
    lines = [x.strip() for x in out.splitlines() if x.startswith("  q")]
    assert lines[0] == "q3  helpful passed it, but Answer (deterministic) failed"
    assert lines[1] == "q1  helpful 0.7 vs helpful_gpt 0.1"
