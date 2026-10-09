"""Which judge, and which model served: a new judge isn't compared as like for like, and a failure
that follows the routed model says so (assay/local.py judge_changes, routing)."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")

JUDGED = '''
import os
import assay_sdk as assay
assay.init()
fail = os.environ.get("FAIL") == "1"
for case in ("q1", "q2"):
    bad = fail and case == "q1"
    assay.check(None, case, "fail" if bad else "pass", field="helpful", score=0.2 if bad else 0.9,
                judge_model=os.environ["JUDGE"], judge_prompt="helpful@3")
'''

ROUTED = '''
import os
import assay_sdk as assay
assay.init()
attempt, mode = int(os.environ["ASSAY_TEST_ATTEMPT"]), os.environ["MODE"]
for case in ("refund", "greeting"):
    with assay.run("support", test=case) as r:
        model = "gpt-5-mini" if mode != "before" and attempt % 2 else "claude-opus-5"
        r.llm(model=model, tokens_in=100, tokens_out=10)
        ok = not (case == "refund" and model == "gpt-5-mini")
        r.check("answer", "pass" if ok else "fail", expected="10 days", actual="10 days" if ok else "5 days")
        r.answer("ok")
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


def test_a_new_judge_isnt_compared_with_the_old_ones_baseline(project, monkeypatch, capsys):
    (project / "judged.py").write_text(JUDGED, encoding="utf-8")
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} judged.py"\n', encoding="utf-8")
    monkeypatch.setenv("JUDGE", "claude-opus-5")
    assert main(["test"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("JUDGE", "claude-fable-5-1")  # a new judge model, and q1 fails under it
    monkeypatch.setenv("FAIL", "1")
    code, out = main(["test"]), capsys.readouterr().out
    assert code == 0, out  # not a regression of the AI: the measure changed
    assert "? 1 judge changed" in out
    assert "judged by a different judge than their baseline" in out
    assert "claude-opus-5 · helpful@3 → claude-fable-5-1 · helpful@3: q1 helpful" in out
    assert "`assay calibrate` with the new judge" in out
    assert main(["test"]) == 0  # the new judge's result is the baseline now: the same failure, known
    monkeypatch.setenv("FAIL", "0")
    assert main(["test"]) == 0


def test_a_failure_that_follows_the_routed_model(project, monkeypatch, capsys):
    (project / "routed.py").write_text(ROUTED, encoding="utf-8")
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} routed.py"\nrepeat = 4\n', encoding="utf-8")
    monkeypatch.setenv("MODE", "before")  # every attempt on claude-opus-5
    assert main(["test"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("MODE", "routed")  # half the attempts routed to gpt-5-mini
    code, out = main(["test"]), capsys.readouterr().out
    assert code == 1
    assert "fails only on gpt-5-mini (0/2), passes on claude-opus-5 (2/2): the model it was routed to" in out
    assert "By model\n  claude-opus-5  2/2 cases\n  gpt-5-mini     1/2 cases" in out
    assert "Could be chance" not in out  # the routing explains it
    assert main(["diff"]) == 1
    diff = capsys.readouterr().out
    assert "BY MODEL" in diff and "Answer: fails only on gpt-5-mini (0/2)" in diff


def test_routing_explains_a_flaky_case(project, monkeypatch, capsys):
    (project / "routed.py").write_text(ROUTED, encoding="utf-8")
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} routed.py"\nrepeat = 4\n', encoding="utf-8")
    monkeypatch.setenv("MODE", "routed")
    main(["test"])
    main(["accept"])
    capsys.readouterr()
    code, out = main(["test"]), capsys.readouterr().out
    assert "flaky" in out and "fails only on gpt-5-mini (0/2)" in out


def test_evaluate_and_the_built_in_judge_say_which_judge_they_were():
    from assay import judge
    from assay_sdk import Judge, evaluate
    from assay_sdk.llm import Response
    seen = []

    class Run:
        def check(self, field, status, **kw):
            seen.append((kw.get("judge_model"), kw.get("judge_prompt")))
    evaluate(lambda q: Response(text='{"score": 0.9}', structured={"score": 0.9}, model="gpt-5"), "q", run=Run(),
             judge_prompt="helpful@3")
    msgs = N(create=lambda **kw: N(model="claude-opus-5", stop_reason="end_turn", usage=None,
                                   content=[N(type="text", text='{"score": 0.9}')]))
    evaluate(Judge("anthropic", "claude-opus-5", client=N(messages=msgs)), "q", run=Run())
    assert seen == [("gpt-5", "helpful@3"), ("claude-opus-5", None)]
    traj = {"answer": "ok", "status": "completed", "steps": [{"seq": 0, "kind": "answer", "text": "ok"}]}
    v = {"consistency": {"applicable": True, "score": 5, "reason": "fine"},
         "plan_quality": {"applicable": False, "score": 1, "reason": "no plan"}}
    fake = N(messages=N(create=lambda **kw: N(model="claude-opus-5", stop_reason="end_turn",
                                               content=[N(type="text", text=json.dumps(v))])))
    out = judge.judge(traj, "x", client=fake)["consistency"]
    assert out["judge_model"] == "claude-opus-5" and out["judge_prompt"] == judge.PROMPT
    assert judge.PROMPT.startswith("assay.judge@1#")
