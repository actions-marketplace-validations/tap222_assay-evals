"""A case's noise floor and coin flips (assay/local.py scores): a score drop counts only when it clears
the range the case shows when nothing is wrong."""
import json
import sys
from pathlib import Path

import pytest

from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")

SCORED = '''
import json, os
import assay_sdk as assay
assay.init()
a = int(os.environ["ASSAY_TEST_ATTEMPT"])
for case, scores in json.loads(os.environ["SCORES"]).items():
    s = scores[a % len(scores)]
    with assay.run("support", test=case) as r:  # each attempt its own run, as with the pytest plugin
        r.answer(f"answer {s}")
        r.check("helpful", "pass" if s >= 0.5 else "fail", score=s)
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "scored.py").write_text(SCORED, encoding="utf-8")

    def toml(repeat):
        (tmp_path / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} scored.py"\nrepeat = {repeat}\n', encoding="utf-8")
    return toml


def run(monkeypatch, capsys, **scores):
    monkeypatch.setenv("SCORES", json.dumps({"steady": [0.9], **scores}))
    return main(["test"]), capsys.readouterr().out


def test_a_drop_counts_only_when_it_clears_the_noise_floor(project, monkeypatch, capsys):
    project(3)
    for _ in range(2):  # nothing wrong: q1 scores 0.8 to 0.9
        assert run(monkeypatch, capsys, q1=[0.9, 0.8, 0.85])[0] == 0
    code, out = run(monkeypatch, capsys, q1=[0.82, 0.83, 0.84])
    assert code == 0 and "1 score lower, within the noise (not failing): q1 0.83 in 0.8–0.9" in out
    code, out = run(monkeypatch, capsys, q1=[0.6, 0.62, 0.61])  # still passing, clearly worse
    assert code == 1, out
    assert "1 check still passing, but scored below its noise floor" in out
    assert "q1  helpful: 0.61, below the 0.8–0.9 it scores when nothing is wrong" in out
    assert "✗ 1 regressed" in out
    assert main(["diff"]) == 1 and "helpful: 0.61, below the 0.8–0.9" in capsys.readouterr().out


def test_one_score_is_no_floor(project, monkeypatch, capsys):
    project(1)
    assert run(monkeypatch, capsys, q1=[0.9])[0] == 0
    code, out = run(monkeypatch, capsys, q1=[0.7])
    assert code == 0 and "1 score lower, with no noise floor yet (fewer than 3 scores)" in out
    assert "`assay test --repeat 5` measures it" in out


def test_coin_flips_are_named(project, monkeypatch, capsys):
    project(4)
    code, out = run(monkeypatch, capsys, flip=[0.9, 0.2], wide=[0.95, 0.55, 0.9, 0.6])
    assert "2 coin flips: different answers from the same system" in out, out
    assert "flip  helpful: passed 2/4, scores 0.2–0.9" in out and "wide  helpful: passed 4/4, scores 0.55–0.95" in out
    assert "steady" not in out.split("coin flip")[1]  # the same score every time is no coin flip
    assert "Tighten its rubric or its expected output" in out
    assert "couldn't be judged" not in out  # a judge's check with no actual isn't "the same output" twice
