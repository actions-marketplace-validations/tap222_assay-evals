"""Judge calibration (assay/calibrate.py): a judge against a golden set, and against its last calibration."""
import json
import sys
from pathlib import Path

import pytest

from assay import calibrate
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")

JUDGE = '''
import os, re
def grade(question, answer):
    q = int(re.search(r"quality=(\\d)", answer).group(1))
    mode = os.environ.get("MODE", "good")
    if mode == "invalid" and "#0 " in answer:
        return "I think it's fine?"
    if mode == "broken" and question.startswith("product"):
        q = 6 - q  # the prompt change that fixed one question type and broke this one
    if mode == "lenient":
        q = min(5, q + 1)
    if "BROKEN" in answer and mode != "blind":
        q = max(1, q - 3)  # it notices the answer is wrong
    if os.environ.get("AS_MODEL"):
        from assay_sdk.llm import Response
        return Response(text="x", structured={"score": q}, model=os.environ["AS_MODEL"])
    return {"score": q, "reason": "rubric"}


def strict(question, answer):
    q = int(re.search(r"quality=(\\d)", answer).group(1))
    return {"score": 6 - q if question.startswith("product") and q in (1, 5) else q}
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_POLICY", "MODE"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "evals").mkdir()
    (tmp_path / "evals" / "judges.py").write_text(JUDGE, encoding="utf-8")
    (tmp_path / "assay.toml").write_text('[test]\ncommand = "true"\n\n[calibrate]\njudge = "evals/judges.py:grade"\n'
                                         'repeat = 3\nscore_range = [1, 5]\nthreshold = 3\n', encoding="utf-8")
    items = []
    for i in range(30):
        kind = "behavioral" if i % 2 else "product"
        q = i % 5 + 1
        items.append({"id": f"{kind}-{i}", "input": f"{kind} question {i}", "output": f"#{i} answer quality={q}",
                      "score": q, "by": "sam", "tags": [kind]})
    for i, x in enumerate(items[:10]):  # a second person labeled a third of them, one point off now and then
        q = x.pop("score")
        x.pop("by")
        x["labels"] = [{"by": "sam", "score": q}, {"by": "ana", "score": q if i % 3 else max(1, q - 1)}]
    (tmp_path / "golden.jsonl").write_text("\n".join(json.dumps(x) for x in items) + "\n", encoding="utf-8")
    return tmp_path


def test_a_calibrated_judge_then_a_change_that_breaks_one_question_type(project, monkeypatch, capsys):
    assert main(["calibrate"]) == 0
    out = capsys.readouterr().out
    assert "Judge calibration" in out and "evals/judges.py:grade · 30 items · 3 judgements each" in out
    assert "Ranking      Spearman 0.99 (95% interval 0.98–1.00)" in out, out
    assert "pairs in the right order: 100% (375 of 375)" in out  # ties in the labels aren't pairs
    assert "Agreement    exact 97%, within one 100%" in out
    assert "people agree: exact 70%, within one 100% (10 items labeled twice)" in out
    assert "this one is the baseline" in out

    monkeypatch.setenv("MODE", "lenient")  # the same order, but a point kinder: it now passes bad answers
    assert main(["calibrate"]) == 1
    out = capsys.readouterr().out
    assert "(lenient)" in out and "bad answers it caught before now pass" in out and "(beyond chance)" in out
    assert "it confirms good answers but lets bad ones through" in out

    monkeypatch.setenv("MODE", "broken")
    assert main(["calibrate"]) == 1
    out = capsys.readouterr().out
    assert "✗ product" in out and "beyond chance" in out and "Regressed." in out
    # The code and the golden set are the same as the baseline's: the only explanation left is drift.
    assert "the provider changed the model under its name" in out
    assert "new ordering violations two or more apart" in out
    assert "  behavioral     Spearman" in out and "✗ behavioral" not in out  # the other type is fine

    monkeypatch.setenv("MODE", "good")
    assert main(["calibrate"]) == 0  # the baseline is still the last one that passed


def test_answers_that_arent_verdicts_are_counted_apart(project, monkeypatch, capsys):
    monkeypatch.setenv("MODE", "invalid")
    assert main(["calibrate", "--repeat", "2"]) == 0
    out = capsys.readouterr().out
    assert "Validity     29 of 30 items judged; not verdicts: 2 invalid" in out


def test_golden_set_stats_add_and_the_ceiling(project, capsys):
    assert main(["golden", "stats"]) == 0
    out = capsys.readouterr().out
    assert "golden.jsonl: 30 items, labeled by sam (30), ana (10)" in out
    assert "People agree:" in out and "No judge will do much better than that." in out
    assert main(["golden", "add", "extra-1", "--score", "4", "--by", "sam", "--output", "quality=4",
                 "--input", "product q", "--tags", "product"]) == 0
    assert main(["golden", "add", "extra-1", "--score", "3", "--by", "ana"]) == 0
    items = calibrate.load_golden(project / "golden.jsonl")
    extra = next(x for x in items if x["id"] == "extra-1")
    assert [lab["score"] for lab in extra["labels"]] == [4, 3] and extra["label"] == 3.5
    assert main(["golden", "add", "extra-2", "--score", "9", "--output", "x"]) == 2


def test_the_statistics():
    assert calibrate.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == pytest.approx(1)
    assert calibrate.spearman([1, 2, 3, 4], [4, 3, 2, 1]) == pytest.approx(-1)
    assert calibrate.spearman([1, 1, 1], [1, 2, 3]) is None
    items = [{"id": "a", "label": 5, "judged": 2.0}, {"id": "b", "label": 2, "judged": 3.0},
             {"id": "c", "label": 4, "judged": 4.0}]
    v, comparable = calibrate.violations(items)
    assert comparable == 3 and [(x["high"], x["low"], x["gap"]) for x in v] == [("a", "b", 3), ("a", "c", 1)]
    lo, hi = calibrate.bootstrap_rho(list(range(20)), list(range(20)))
    assert lo == pytest.approx(1) and hi == pytest.approx(1)
    cov = calibrate.coverage([{"label": 1, "labels": [{"by": "x", "score": 1}], "tags": []}], (1, 5))
    assert cov["missing"] == [2, 3, 4, 5]


def test_setup_problems(project, capsys):
    (project / "golden.jsonl").write_text('{"id": "a", "output": "x"}\n', encoding="utf-8")
    assert main(["calibrate"]) == 2 and "no score" in capsys.readouterr().err
    assert main(["calibrate", "--judge", "nope"]) == 2
    (project / "golden.jsonl").unlink()
    assert main(["calibrate"]) == 2 and "assay golden add" in capsys.readouterr().err


def test_suggest_spreads_what_to_label_over_the_judges_scores(project, monkeypatch, capsys):
    (project / "record.py").write_text('''
import assay_sdk as assay
assay.init()
for i in range(20):
    s = i / 19
    assay.check(None, f"case-{i}", "pass" if s >= 0.5 else "fail", field="helpful", score=s)
''', encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", SDK)
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} record.py"\n', encoding="utf-8")
    main(["test"])
    capsys.readouterr()
    assert main(["golden", "suggest", "-n", "5", "--field", "helpful"]) == 0
    out = capsys.readouterr().out
    picked = [line.split()[-1] for line in out.splitlines() if line.strip().startswith("judged")]
    assert picked == ["case-0", "case-5", "case-10", "case-14", "case-19"]  # low to high, not five typical ones
    assert main(["golden", "suggest"]) == 2 and "Which check is the judge" in capsys.readouterr().err


def test_variants_a_second_judge_and_a_judge_that_cant_tell_broken_from_good(project, monkeypatch, capsys):
    items = calibrate.load_golden(project / "golden.jsonl")
    for x in items[:6]:
        q = int(x["output"].split("quality=")[1])
        x["variants"] = [{"output": f"{x['output']} (reworded)", "expect": "same", "note": "paraphrase"},
                         {"output": f"{x['output']} BROKEN", "expect": "lower", "note": "wrong refund window"}]
    calibrate.save_golden(project / "golden.jsonl", items)
    assert main(["calibrate", "--second-judge", "evals/judges.py:strict"]) == 0
    out = capsys.readouterr().out
    assert "Variants     " in out and "paraphrases kept their score" in out
    assert "Second judge evals/judges.py:strict over the same 30 items" in out
    assert "they disagree on by 2+ points (the rubric is likely ambiguous there)" in out

    monkeypatch.setenv("MODE", "blind")  # scores a broken answer like the good one
    assert main(["calibrate"]) == 1
    out = capsys.readouterr().out
    assert "variants that behaved before no longer do" in out and "should score lower" in out


def test_every_judged_number_says_whether_it_can_be_trusted(project, monkeypatch, capsys):
    (project / "assay.toml").write_text((project / "assay.toml").read_text(encoding="utf-8").replace('command = "true"',
                                        f'command = "{sys.executable} record.py"') + 'field = "helpful"\n', encoding="utf-8")
    (project / "record.py").write_text("""
import os
import assay_sdk as assay
assay.init()
with assay.run("support", test="q1") as r:
    r.answer("ok")
    r.check("helpful", "pass", score=4, judge_model=os.environ.get("JUDGE_MODEL"))
    r.check("tone", "pass", score=5)
""", encoding="utf-8")
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.setenv("AS_MODEL", "claude-opus-5")
    assert main(["calibrate", "--repeat", "1"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("JUDGE_MODEL", "claude-opus-5")
    main(["test"])
    out = capsys.readouterr().out
    assert "Judges" in out and "helpful  calibrated today: Spearman" in out and "(claude-opus-5)" in out
    assert "tone     not calibrated: its scores haven't been checked against people" in out
    monkeypatch.setenv("JUDGE_MODEL", "claude-fable-5-1")
    main(["test"])
    assert "calibrated for claude-opus-5, but judged by claude-fable-5-1 this run" in capsys.readouterr().out
