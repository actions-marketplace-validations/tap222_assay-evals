"""What changed around a regression: its prompt version (with the text's diff), its model, the tools
it was offered, and whether the regressions line up with a prompt version (assay/local.py setup_changes)."""
import sys
from pathlib import Path

import pytest

from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")

AGENT = '''
import os
import assay_sdk as assay
assay.init()
mode = os.environ["MODE"]
old = assay.prompt("support", "12", template="You are a support agent.\\nCheck the order first.")
new = assay.prompt("support", "13", template="You are a support agent.\\nCheck the order first.\\n"
                                             "Refund right away when the customer is upset.", note="faster refunds")
model = "claude-sonnet-5" if mode == "model" else "claude-sonnet-4.6"
for i in range(4):
    moved = mode == "prompt" and i < 2
    with assay.run("support", test=f"q{i}") as r:
        r.llm(model=model, prompt=new if moved else old, tokens_in=100, tokens_out=10,
              tools=["get_order", "refund"] + (["issue_credit"] if moved else []))
        r.answer("ok")
        r.check("safety", "fail" if moved else "pass", reason="refund before approval" if moved else None)
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "agent.py").write_text(AGENT, encoding="utf-8")
    (tmp_path / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n', encoding="utf-8")
    monkeypatch.setenv("MODE", "before")
    assert main(["test"]) == 0
    return tmp_path


def test_a_regression_says_what_changed_around_it(project, monkeypatch, capsys):
    capsys.readouterr()
    monkeypatch.setenv("MODE", "prompt")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "2 of 2 regressions use support@13; the 2 cases still on another version all pass" in out
    assert "   Changed around it:\n     prompt  support@12 → support@13 (+1 line, −0: “Refund right away when the " \
           "customer is upset.”) — faster refunds\n     tools   offered +issue_credit" in out
    assert "Changed in every case" not in out  # half the cases moved: it's theirs, not everyone's
    assert main(["diff"]) == 1
    diff = capsys.readouterr().out
    assert "2 of 2 regressions use support@13" in diff and "Changed around it:" in diff
    assert main(["diff", "--format", "markdown"]) == 1
    md = capsys.readouterr().out.replace("\u200b", "")  # markdown breaks @ so GitHub doesn't read a mention
    assert "   - Changed: prompt support@12 → support@13 (+1 line, −0" in md


def test_a_change_every_case_shares_is_said_once(project, monkeypatch, capsys):
    capsys.readouterr()
    monkeypatch.setenv("MODE", "model")
    main(["test"])
    out = capsys.readouterr().out
    assert "Changed in every case\n  model   claude-sonnet-4.6 → claude-sonnet-5" in out
    assert "Changed around it" not in out
