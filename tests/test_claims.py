"""Claim contracts (assay/contracts.py claim_breaks): an answer's claim needs the run's own evidence."""
import sys
from pathlib import Path

import pytest

from assay import contracts
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")

AGENT = '''
import os
import assay_sdk as assay
assay.init()
mode = os.environ["MODE"]
with assay.run("support", test="refund") as r:
    if mode != "no_call":
        err = "402 card declined" if mode in ("failed", "honest") else None
        r.tool("refund", {"order_id": "O-17"}, None if err else {"ok": True}, error=err)
    r.state("order:17", "update", {"status": "delivered" if mode in ("wrong_state", "failed", "honest") else "refunded"})
    r.answer("I couldn't refund it: the card was declined." if mode == "honest" else "Refunded O-17: the money is on its way.")
'''

TOML = """[test]
command = "{python} agent.py"

[[contracts]]
kind = "claim"
claim = "refunded|money is on its way"
needs = "refund"
state = {{ name = "order:*", field = "status", is = "refunded" }}
"""


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "agent.py").write_text(AGENT)
    (tmp_path / "assay.toml").write_text(TOML.format(python=sys.executable))
    return tmp_path


@pytest.mark.parametrize("mode, why", [
    ("failed", "claimed “Refunded” in the answer, but refund was called but failed (402 card declined) and "
               "order:17 status is 'delivered', not 'refunded'"),
    ("no_call", "claimed “Refunded” in the answer, but refund was never called"),
    ("wrong_state", "but order:17 status is 'delivered', not 'refunded'"),
])
def test_a_claim_without_its_evidence_fails(project, monkeypatch, capsys, mode, why):
    monkeypatch.setenv("MODE", mode)
    code, out = main(["test"]), capsys.readouterr().out
    assert code == 1 and why in out, out


@pytest.mark.parametrize("mode", ["good", "honest"])
def test_a_claim_with_its_evidence_or_no_claim_passes(project, monkeypatch, capsys, mode):
    monkeypatch.setenv("MODE", mode)  # honest: the refund failed, and the answer says so
    assert main(["test"]) == 0, capsys.readouterr().out


def test_claim_contracts_are_checked_when_saved():
    ok = {"kind": "claim", "claim": "refunded", "needs": "refund"}
    assert contracts.validate(ok) is None
    assert contracts.describe(ok) == "Claiming “refunded” needs a successful refund"
    assert "needs the evidence" in contracts.validate({"kind": "claim", "claim": "refunded"})
    assert "regular expression" in contracts.validate({"kind": "claim", "claim": "(", "needs": "x"})
    assert "state is" in contracts.validate({"kind": "claim", "claim": "x", "state": {"name": "o"}})
    self_review = {"kind": "claim", "claim": "transaction succeeded", "needs": "submit_tx", "claim_in": "any"}
    traj = {"answer": "Done.", "steps": [{"seq": 0, "kind": "reason", "text": "Self-review: the transaction succeeded."},
                                          {"seq": 1, "kind": "answer", "text": "Done."}]}
    b = contracts.claim_breaks(self_review, traj)
    assert b["seq"] == 0 and "in the model text" in b["detail"] and "submit_tx was never called" in b["detail"]
    assert contracts.claim_breaks({**self_review, "claim_in": "answer"}, traj) is None


def test_a_claim_contract_over_the_api_and_in_production(tmp_path):
    from fastapi.testclient import TestClient
    from assay.api import create_app
    from assay.config import Settings
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}")))
    r = c.post("/v1/contracts", json={"source": "events:p", "kind": "claim", "claim": "refunded", "needs": "refund",
                                      "state": {"name": "order:*", "field": "status", "is": "refunded"}})
    assert r.status_code == 201, r.json()
    assert r.json()["label"].startswith("Claiming “refunded” needs a successful refund")
    ev = lambda i, **k: {"v": 1, "id": f"p1-{i}", "ts": "2026-09-26T10:00:00Z", "run_id": "p1", **k}
    c.post("/v1/ingest", json=[ev(0, type="run.start", task="support", input="refund O-17"),
                               ev(1, type="step", seq=0, kind="tool", name="refund", args={"order_id": "O-17"},
                                  status="error", error="402"),
                               ev(2, type="step", seq=1, kind="answer", text="Refunded."), ev(3, type="run.end")],
           headers={"X-Tenant": "p"})
    an = c.get("/v1/learn/anomalies", params={"source": "events:p"}).json()
    sig = next(s for a in an["items"] if a["trace_id"] == "p1" for s in a["signals"] if s["type"] == "contract")
    assert "claimed “Refunded”" in sig["text"] and "refund was called but failed (402)" in sig["text"]
