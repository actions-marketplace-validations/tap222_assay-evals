"""Monitoring production (assay/monitor.py): a sampled judge on live traffic, stratified by task and within a
budget; quality per check with 95% intervals, alerting on the bound; judge failures feeding assay learn; and
what the CI suite costs per run."""
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace as N

import pytest
from fastapi.testclient import TestClient

from assay import judge as judge_mod
from assay import learn, lifecycle, monitor, store
from assay.api import create_app
from assay.config import Settings

H, SRC = {"X-Tenant": "m"}, {"source": "events:m"}


class Judge:
    """Fails consistency for a run whose answer says BAD."""

    def __init__(self):
        self.messages, self.calls = self, 0

    def create(self, **kw):
        self.calls += 1
        bad = "BAD" in kw["messages"][-1]["content"].split("<final_answer>")[-1]
        v = {"plan_quality": {"applicable": False, "verdict": "pass", "critique": "no plan"},
             "consistency": {"applicable": True, "verdict": "fail" if bad else "pass",
                             "category": "fabricated" if bad else "none",
                             "critique": "Step 0 says nothing of it." if bad else "Agrees with step 0."}}
        return N(stop_reason="end_turn", model="claude-opus-5", content=[N(type="text", text=json.dumps(v))])


def run(c, rid, task, answer, hours_ago=2, **start):
    ts = (datetime.utcnow() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = lambda i, **k: {"v": 1, "id": f"{rid}-{i}", "ts": ts, "run_id": rid, **k}
    assert c.post("/v1/ingest", headers=H, json=[
        ev(0, type="run.start", task=task, input="q", **start),
        ev(1, type="step", seq=0, kind="tool", name="lookup", args={}, result={"ok": 1}),
        ev(2, type="step", seq=1, kind="answer", text=answer), ev(3, type="run.end")]).status_code == 200


@pytest.fixture
def app(tmp_path, monkeypatch):
    j = Judge()
    monkeypatch.setattr(judge_mod, "_client", lambda: j)
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", production_judge_sample=0.1)))
    c.judge = j
    for i in range(40):
        run(c, f"a-{i}", "refund", "BAD answer" if i % 4 == 0 else "Refunded.")
    for i in range(2):
        run(c, f"b-{i}", "rare_task", "Done.")
    run(c, "synth-1", "refund", "BAD", tags={"origin": "synthetic"})
    run(c, "test-1", "refund", "BAD", test={"run": "t1", "case": "c1"})
    return c


def test_a_sample_of_each_task_none_that_arent_production(app):
    e = app.app.state.engine
    now = datetime.utcnow()
    picked = monitor.sample(e, "m", now - timedelta(days=1), now, 0.1)
    tasks = [h["task"] for h in picked]
    assert tasks.count("refund") == 4 and tasks.count("rare_task") == 1  # a rare task isn't drowned out
    assert not {h["trajectory_id"] for h in picked} & {"synth-1", "test-1"}
    assert [h["trajectory_id"] for h in monitor.sample(e, "m", now - timedelta(days=1), now, 0.1)] == \
        [h["trajectory_id"] for h in picked]  # the same sample every time


def test_the_sampled_judge_then_quality_with_intervals_and_alerts(app):
    out = app.post("/v1/production/judge", params=SRC).json()
    assert (out["sampled"], out["judged"]) == (5, 5) and app.judge.calls == 5
    assert app.post("/v1/production/judge", params=SRC).json()["sampled"] == 0  # none judged twice
    e = app.app.state.engine
    with e.connect() as conn:
        rows = conn.execute(store.production_results.select()).all()
    assert {r.field for r in rows} == {"consistency"} and not {r.trajectory_id for r in rows} & {"synth-1", "test-1"}
    lifecycle.sweep(e)  # the reference-free checks every ended run gets
    q = app.get("/v1/production/quality", params=SRC).json()
    m = {x["metric"]: x for x in q["metrics"]}
    assert m["check.completed"]["n"] == 42 and m["check.completed"]["value"] == 1.0  # synthetic and test runs left out
    j = m["judge.consistency"]
    assert j["n"] == 5 and j["low"] < j["value"] < j["high"] and j["state"] is None
    fails = j["n"] - round(j["value"] * j["n"])
    r = app.put("/v1/production/targets", params=SRC, json={"metric": "judge.consistency", "target": 0.95}).json()
    assert r["state"] == ("breach" if r["high"] < 0.95 else "investigate") and fails >= 1
    alerts = [a for a in app.get("/v1/alerts", params=SRC).json() if a["kind"] == "quality"]
    assert len(alerts) == 1 and alerts[0]["state"] == "open" and "judge.consistency" in alerts[0]["message"]
    app.put("/v1/production/targets", params=SRC, json={"metric": "judge.consistency", "target": 0.05})
    assert [a["state"] for a in app.get("/v1/alerts", params=SRC).json() if a["kind"] == "quality"] == ["resolved"]
    # What the judge failed is a failure signal: it groups into patterns and draft test cases.
    from assay.models import Window
    from assay.sources.events import EventsSource
    now = datetime.utcnow()
    sc = learn.score(EventsSource(e, "m"), Window(now - timedelta(days=1), now), e)
    judged = [a for a in sc["anomalous"] if any(s["type"] == "judged" for s in a["signals"])]
    assert len(judged) == fails and "fabricated" in judged[0]["signals"][0]["text"] + str(judged[0]["signals"])


def test_the_budget_and_the_bound():
    assert monitor.state("pass_rate", 0.90, 0.99, 0.95) == "investigate"
    assert monitor.state("pass_rate", 0.80, 0.90, 0.95) == "breach"
    assert monitor.state("pass_rate", 0.96, 0.99, 0.95) == "ok"
    assert monitor.state("share", 0.01, 0.08, 0.05) == "investigate" and monitor.state("share", 0.06, 0.1, 0.05) == "breach"
    lo, hi = monitor.wilson(45, 50)
    assert 0.78 < lo < 0.8 and 0.95 < hi < 0.97


def test_a_spent_budget_judges_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(judge_mod, "_client", lambda: Judge())
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", production_judge_sample=0.5,
                                       production_judge_budget_usd=0.0)))
    run(c, "x-1", "refund", "ok")
    assert "budget" in c.post("/v1/production/judge", params=SRC).json()["skipped"]


def test_what_the_ci_suite_costs(tmp_path, monkeypatch, capsys):
    from assay.__main__ import main
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assay.toml").write_text('[test]\ncommand = "true"\n')
    (tmp_path / ".assay").mkdir()
    e = store.make_engine(f"sqlite:///{tmp_path / '.assay' / 'assay.db'}")
    now = datetime.utcnow()
    rows, heads, steps = [], [], []
    for i in range(4):
        base = dict(tenant="local", run_id="r1", case_id=f"c{i}", ts=now, attempt=0, score=None, reason=None)
        rows.append({**base, "result_id": f"x{i}", "field": "answer", "evaluator": "pytest", "status": "pass",
                     "judge_model": None, "duration_ms": 1.0, "cost_usd": None})
        if i < 3:
            rows.append({**base, "result_id": f"j{i}", "field": "helpful", "evaluator": "assay.judge@1", "status": "pass",
                         "judge_model": "claude-opus-5", "score": 4, "duration_ms": 2000.0, "cost_usd": 0.01})
        heads.append(dict(tenant="local", trajectory_id=f"tr{i}", run_id="r1", case_id=f"c{i}", started_at=now))
        steps.append(dict(tenant="local", trajectory_id=f"tr{i}", seq=0, kind="reason", cost_usd=0.02 if i == 0 else 0.001))
    with e.begin() as conn:
        conn.execute(store.eval_results.insert(), rows)
        conn.execute(store.agent_trajectories.insert(), heads)
        conn.execute(store.agent_steps.insert(), steps)
    assert main(["evals", "audit"]) == 0
    out = capsys.readouterr().out
    assert "The suite, latest run r1: 4 cases, 3 read by a judge (75%)" in out and "6.0 s in evaluators" in out
    assert "Costliest cases: c0 $0.0300" in out and "Most cases need a judge" in out
