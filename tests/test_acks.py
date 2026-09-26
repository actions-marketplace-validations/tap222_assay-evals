"""Acknowledged failures (assay/acks.py): quiet until worse than the band they showed, and never for ever."""
import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from assay import acks, local
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")

CHECKS = '''
import json, os
import assay_sdk as assay
assay.init()
for case, (field, status, score, reason, *cat) in json.loads(os.environ["SPEC"]).items():
    assay.check(None, case, status, field=field, score=score, reason=reason, category=cat[0] if cat else None)
'''


def judge(score, passed=False):
    return ["consistency", "pass" if passed else "fail", score, f"{score}/5: the answer overstates step 2"]


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY",
              "ASSAY_POLICY_CHANGE", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "checks.py").write_text(CHECKS)
    (tmp_path / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} checks.py"\n')
    return tmp_path


def run(monkeypatch, capsys, **spec):
    spec = {"ok": ["answer", "pass", None, None], **spec}
    monkeypatch.setenv("SPEC", json.dumps(spec))
    code = main(["test"])
    return code, capsys.readouterr().out


def test_a_noisy_judge_stays_quiet_inside_its_band_and_wakes_below_it(project, monkeypatch, capsys):
    for s in (3, 2, 3):  # a case the judge scores 2 or 3, on its own
        assert run(monkeypatch, capsys, q1=judge(s))[0] == 1
    assert main(["ack", "q1", "--reason", "judge disagrees, #412", "--for", "14d", "--by", "sam"]) == 0
    out = capsys.readouterr().out
    assert "Acknowledged q1 Consistency, until " in out and "quiet while its score stays within 2–3 (from 3 scores)" \
        in out and "Reasoning · low score" in out
    a = acks.load(project / "assay.toml")[0]
    assert (a["case"], a["check"], a["by"], a["band"]) == ("q1", "consistency", "sam",
                                                            {"min": 2.0, "max": 3.0, "median": 3.0, "n": 3})
    code, out = run(monkeypatch, capsys, q1=judge(2))  # a dip it already showed
    assert code == 0, out
    assert "1 check acknowledged (1 case), quiet until worse" in out and "· 1 acknowledged" in out
    code, out = run(monkeypatch, capsys, q1=judge(1))  # worse than it has been
    assert code == 1
    assert "Acknowledged by sam (judge disagrees, #412), but worse: score 1, below the 2–3 it showed" in out


def test_the_same_score_failing_a_different_way_wakes_it(project, monkeypatch, capsys):
    wrong = ["answer", "fail", None, "Wrong answer: said 5 days, the policy says 10"]
    assert run(monkeypatch, capsys, q2=wrong)[0] == 1
    assert main(["ack", "q2", "answer", "--reason", "known: #88"]) == 0
    capsys.readouterr()
    assert run(monkeypatch, capsys, q2=["answer", "fail", None, "Wrong answer: said 6 days, the policy says 10"])[0] == 0
    code, out = run(monkeypatch, capsys, q2=["answer", "fail", None, "Unsafe action: refunded before delivery"])
    assert code == 1
    assert "fails differently now: Output quality · Unsafe action (acknowledged: Output quality · Wrong answer)" in out


def test_it_ends_at_its_date_and_when_the_check_passes(project, monkeypatch, capsys):
    assert run(monkeypatch, capsys, q1=judge(3))[0] == 1
    assert main(["ack", "q1", "--reason", "#412"]) == 0
    capsys.readouterr()
    config = project / "assay.toml"
    items = acks.load(config)
    items[0]["until"] = acks._now() - timedelta(minutes=1)  # two weeks later
    acks.save(config, items)
    code, out = run(monkeypatch, capsys, q1=judge(3))
    assert code == 1 and "Acknowledgement ended: q1 Consistency, acknowledged by" in out

    items[0]["until"] = acks._now() + timedelta(days=7)
    acks.save(config, items)
    assert run(monkeypatch, capsys, q1=judge(3))[0] == 0  # held again
    assert run(monkeypatch, capsys, q1=judge(4, passed=True))[0] == 0  # it passes: the ack is spent
    code, out = run(monkeypatch, capsys, q1=judge(3))
    assert code == 1  # failing again is news, though it's the same failure
    assert "no longer applies (`assay acks --prune`)" in out
    assert main(["acks"]) == 0
    assert "passing since" in capsys.readouterr().out
    assert main(["acks", "--prune"]) == 0 and "Removed 1 acknowledgement" in capsys.readouterr().out
    assert acks.load(config) == []


def test_accept_acknowledges_for_a_while_not_for_ever(project, monkeypatch, capsys):
    assert run(monkeypatch, capsys, q1=judge(3))[0] == 1
    assert main(["accept", "--for", "2w"]) == 0
    out = capsys.readouterr().out
    assert "Its 1 failing check is acknowledged until" in out
    a = acks.load(project / "assay.toml")[0]
    assert a["reason"] == "accepted with `assay accept`" and a["until"] - a["at"] == timedelta(days=14)
    assert main(["accept", "--for", "1y"]) == 2
    assert main(["ack", "q1", "--reason", "x", "--for", "120d"]) == 2
    assert "at most 90 days" in capsys.readouterr().err


def test_a_pull_request_cant_acknowledge_its_own_regression(project, monkeypatch, capsys):
    assert run(monkeypatch, capsys, q1=judge(3))[0] == 1
    base = project / "base"
    base.mkdir()
    (base / "assay.base.toml").write_text((project / "assay.toml").read_text())  # the base branch: no acks
    assert main(["ack", "q1", "--reason", "fine, trust me"]) == 0
    capsys.readouterr()
    monkeypatch.setenv("ASSAY_POLICY", str(base / "assay.base.toml"))
    code, out = run(monkeypatch, capsys, q1=judge(3))
    assert code == 1 and "acknowledges q1 Consistency until" in out
    monkeypatch.setenv("ASSAY_POLICY_CHANGE", "accepted")  # the label: reviewed, and accepted
    assert run(monkeypatch, capsys, q1=judge(3))[0] == 0
    monkeypatch.delenv("ASSAY_POLICY_CHANGE")
    acks.save(base / "assay.base.toml", acks.load(project / "assay.toml"))  # on the base branch already
    assert (base / "assay.base.acks.toml").exists()
    assert run(monkeypatch, capsys, q1=judge(3))[0] == 0


AGENT = '''
import os
import assay_sdk as assay
assay.init()
with assay.run("support", test="rag") as r:
    r.llm(model="m", tokens_in=int(os.environ["TOKENS"]), tokens_out=10)
    r.answer("ok")
'''


def test_a_behavior_change_can_be_acknowledged_up_to_what_it_showed(project, monkeypatch, capsys):
    (project / "agent.py").write_text(AGENT)
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n')
    for tokens, code in (("800", 0), ("3000", 1)):
        monkeypatch.setenv("TOKENS", tokens)
        assert main(["test"]) == code
    out = capsys.readouterr().out
    assert "Context: 800 tokens → 3,000 tokens" in out
    assert main(["ack", "rag", "--reason", "the new retriever: #90"]) == 0
    assert "Acknowledged rag behavior (context), until" in capsys.readouterr().out
    assert acks.load(project / "assay.toml")[0]["check"] == "behavior.context_tokens"
    monkeypatch.setenv("TOKENS", "3000")
    assert main(["test"]) == 0
    monkeypatch.setenv("TOKENS", "5000")
    assert main(["test"]) == 1
    assert "worse than acknowledged: context 5,000 tokens, past the 3,000 tokens it showed" in capsys.readouterr().out


def test_failure_classes_and_durations():
    fc = acks.failure_class
    assert fc("consistency", "2/5: overstates step 2") == fc("consistency", "3/5: something else") == \
        "Reasoning · low score"
    assert fc("pii", "Personal data leaked: email in refund(…)") != fc("pii", "Personal data leaked: card in refund(…)")
    assert fc("pytest", "AssertionError: assert 'a' == 'b'") == "Output quality · AssertionError"
    assert fc("tool_calls", "Wrong tool: called cancel_order") == "Tool selection · Wrong tool"
    assert acks.duration("36h") == timedelta(hours=36) and acks.duration("2w") == timedelta(days=14)
    for bad in ("forever", "0d", "91d"):
        with pytest.raises(acks.AckError):
            acks.duration(bad)
    assert acks.path_for(Path("assay.toml")).name == "assay.acks.toml"
    assert acks.path_for(Path("/tmp/assay.base.toml")).name == "assay.base.acks.toml"


def test_an_acknowledgement_for_ever_is_rejected(tmp_path):
    (tmp_path / "assay.acks.toml").write_text('[[ack]]\ncase = "q1"\ncheck = "answer"\nreason = "x"\nby = "me"\n'
                                              'at = 2026-09-01T00:00:00Z\nuntil = 2099-01-01T00:00:00Z\n')
    with pytest.raises(acks.AckError, match="more than 90 days"):
        acks.load(tmp_path / "assay.toml")
    (tmp_path / "assay.toml").write_text("[test]\ncommand = 'x'\n")
    with pytest.raises(local.SetupError, match="Nothing is acknowledged for ever"):
        local.load_config(tmp_path)


def test_a_grounding_miss_that_becomes_a_policy_refusal_wakes_at_the_same_score(project, monkeypatch, capsys):
    """Keyed on the number, this would stay quiet: the judge scores both 2."""
    grounding = ["consistency", "fail", 2, "2/5: cites a refund window no step found", "grounding"]
    for _ in range(2):
        assert run(monkeypatch, capsys, q1=grounding)[0] == 1
    assert main(["ack", "q1", "--reason", "retriever misses the policy doc, #77"]) == 0
    assert "(Reasoning · grounding)" in capsys.readouterr().out
    assert run(monkeypatch, capsys, q1=grounding)[0] == 0
    code, out = run(monkeypatch, capsys, q1=["consistency", "fail", 2, "2/5: refused to discuss refunds",
                                             "policy_refusal"])
    assert code == 1
    assert "fails differently now: Reasoning · policy_refusal (acknowledged: Reasoning · grounding)" in out


def test_evaluate_reads_the_category_a_judge_names():
    from assay_sdk import evaluate
    seen = []

    class Run:
        def check(self, field, status, **kw):
            seen.append((status, kw.get("category")))
    r = evaluate(lambda q: {"score": 0.2, "reason": "made it up", "category": "grounding"}, "q", run=Run())
    assert (r.status, r.category) == ("FAIL", "grounding") and seen == [("fail", "grounding")]
    r = evaluate(lambda q: {"score": 0.9, "category": "grounding"}, "q")
    assert r.status == "PASS" and r.category is None  # a pass has no kind of failure
