"""`assay report` (assay/report.py): what the tests caught before users saw it, what fixed it, what
production shows, and the running log."""
import json
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
version = {"before": "12", "broken": "13", "fixed": "14"}[mode]
p = assay.prompt("support", version, template=f"You are a support agent. v{version}")
for case in ("refund", "greeting", "shipping"):
    with assay.run("support", test=case) as r:
        r.llm(model="claude-opus-5", prompt=p, tokens_in=100, tokens_out=10)
        r.answer("ok")
        bad = (mode == "broken" and case == "refund") or (mode != "before" and case == "shipping")
        r.check("safety" if case == "refund" else "answer", "fail" if bad else "pass",
                reason="refund before approval" if case == "refund" else "wrong delivery date")
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
    return tmp_path


def test_the_report_counts_what_tests_caught_and_what_fixed_it(project, monkeypatch, capsys):
    for mode in ("before", "broken", "fixed"):
        monkeypatch.setenv("MODE", mode)
        main(["test"])
    assert main(["log", "add", "Approval prompts need an explicit wait step", "--by", "sam"]) == 0
    capsys.readouterr()
    assert main(["report", "--out", "report.md"]) == 0
    md = capsys.readouterr().out
    assert "**2 issues caught by tests before users saw them** (1 fixed, 1 still open)" in md
    assert "- **HIGH** `refund` safety: refund before approval — fixed" in md
    assert "by prompt support@13 → support@14" in md.replace("\u200b", "")  # what fixed it
    assert "- **MEDIUM** `shipping` answer: wrong delivery date — still failing" in md
    assert "## The log" in md and "Approval prompts need an explicit wait step (sam)" in md
    assert "Caught by tests: refund safety: refund before approval" in md
    assert Path("report.md").read_text(encoding="utf-8").strip() == md.strip()
    main(["report", "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert {x["case"] for x in data["caught"]} == {"refund", "shipping"}
    main(["report"])  # the log keeps each finding once
    assert capsys.readouterr().out.count("Caught by tests: refund safety") == 1


def test_the_servers_report_adds_production(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from assay import judge as judge_mod
    from assay.api import create_app
    from assay.config import Settings
    import test_review as tr
    reader = tr.Reader()
    monkeypatch.setattr(judge_mod, "_client", lambda: reader)
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}")))
    for i in range(1, 4):
        tr.convo(c, f"bad-{i}", f"Where is my order O-{i}?", "Our refund policy is 30 days from delivery.")
    tr.convo(c, "ok-1", "Where is my order O-7?", "O-7 is out for delivery today.")
    c.post("/v1/review/run", params={"source": "events:q", "sample": 10})
    c.post("/v1/report/log", params={"source": "events:q"}, json={"text": "Routed order questions to the tracking tool"})
    md = c.get("/v1/report", params={"source": "events:q"}).text
    assert "## Failure modes in production" in md
    assert "| Answers the policy, not the question | reading conversations | 75% |" in md
    assert "A new failure mode: Answers the policy, not the question" in md and "A new kind of task: support" in md
    assert "Routed order questions to the tracking tool (open mode)" in md
    assert c.post("/v1/report/send", params={"source": "events:q"}).status_code == 400  # no webhook set
