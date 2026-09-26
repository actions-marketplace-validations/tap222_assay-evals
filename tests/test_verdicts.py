"""One verdict per check (assay/verdicts.py), and evaluation that can't get stuck (assay/lifecycle.py)."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from assay import lifecycle, store
from assay.api import create_app
from assay.config import Settings

H, SRC = {"X-Tenant": "t"}, {"source": "events:t"}


@pytest.fixture
def app(tmp_path):
    settings = Settings(store_url=f"sqlite:///{tmp_path / 'v.db'}")
    client = TestClient(create_app(settings))
    client.engine = store.make_engine(settings.store_url)
    return client


def check(i, run, case, status, evaluator="helpful@1", attempt=None, reason=None, field="helpful"):
    return {"v": 1, "id": f"{run}-{case}-{evaluator}-{attempt}-{i}", "ts": "2026-09-25T10:00:00Z", "type": "check",
            "test": {"run": run, "case": case, **({"attempt": attempt} if attempt is not None else {})},
            "status": status, "field": field, "evaluator": evaluator, "reason": reason}


def test_each_check_gets_one_verdict(app):
    ev = []
    for i in range(10):
        c = f"c{i}"
        ev.append(check(i, "r", c, "pass", evaluator="exact@1", field="total"))  # present for every case
        if i == 0:
            ev.append(check(i, "r", c, "fail", reason="wrong total"))
        elif i == 1:
            ev.append(check(i, "r", c, "error", reason="Read timed out (503)"))
        elif i == 2:
            ev.append(check(i, "r", c, "error", reason="judge returned invalid JSON"))
        elif i == 3:
            ev += [check(i, "r", c, "pass", attempt=0), check(i, "r", c, "fail", attempt=1, reason="meh")]
        elif i in (4, 5):
            continue  # the judge skipped these
        else:
            ev.append(check(i, "r", c, "pass"))
    assert app.post("/v1/ingest", json=ev, headers=H).status_code == 200
    out = app.get("/v1/evals/runs/r/verdicts", params=SRC).json()
    assert out["counts"] == {"PASS": 14, "FAIL": 1, "FLAKY": 1, "INCONCLUSIVE": 0, "INVALID": 0, "TIMEOUT": 0,
                             "RATE_LIMITED": 0, "EVALUATOR_ERROR": 1, "INFRA_ERROR": 1, "MISSING": 2}
    by = {(c["case_id"], c["evaluator"]): c for c in out["checks"]}
    assert by[("c1", "helpful@1")]["verdict"] == "INFRA_ERROR" and "503" in by[("c1", "helpful@1")]["reason"]
    assert by[("c2", "helpful@1")]["verdict"] == "EVALUATOR_ERROR"
    assert by[("c4", "helpful@1")]["reason"] == "helpful@1 reported on 8 of 10 cases, not this one"
    assert [c["case_id"] for c in app.get("/v1/evals/runs/r/verdicts", params={**SRC, "verdict": "fail"})
            .json()["checks"]] == ["c0"]
    # Results that never arrived keep the release from advancing.
    st = app.get("/v1/evals/runs/r/stability", params=SRC).json()
    assert st["outcome"] != "advance" and st["verdicts"]["MISSING"] == 2
    assert any("2 results never arrived from helpful@1" in r for r in st["reasons"])


def test_one_run_that_cant_be_evaluated_doesnt_hold_up_the_others(app, monkeypatch):
    for rid in ("good", "poison"):
        app.post("/v1/ingest", headers=H, json=[
            {"v": 1, "id": f"{rid}s", "ts": "2026-09-25T10:00:00Z", "type": "run.start", "run_id": rid},
            {"v": 1, "id": f"{rid}e", "ts": "2026-09-25T10:00:01Z", "type": "run.end", "run_id": rid}])
    real = lifecycle.run_checks

    def flaky_checker(traj, *a, **k):
        if traj["trajectory_id"] == "poison":
            raise ValueError("unexpected step shape")
        return real(traj, *a, **k)
    monkeypatch.setattr(lifecycle, "run_checks", flaky_checker)
    with app.engine.begin() as conn:  # both need evaluating again
        conn.execute(store.run_checks.delete())
    assert lifecycle.sweep(app.engine)["evaluated"] == 2
    good = app.get("/v1/agents/trajectories/good", params=SRC).json()["evaluation"]
    bad = app.get("/v1/agents/trajectories/poison", params=SRC).json()["evaluation"]
    assert good["failed"] == 0
    assert bad["checks"] == [{"check": "evaluation", "status": "error", "reason": "ValueError: unexpected step shape"}]
    assert lifecycle.pending(app.engine) == []  # not retried forever
    assert app.get("/v1/agents/lifecycle", params=SRC).json()["evaluation_errors"] == 1


def test_a_stuck_evaluation_opens_an_alert_and_resolves_it(app):
    app.post("/v1/ingest", headers=H, json=[
        {"v": 1, "id": "s", "ts": "2026-09-25T10:00:00Z", "type": "run.start", "run_id": "r1"},
        {"v": 1, "id": "e", "ts": "2026-09-25T10:00:01Z", "type": "run.end", "run_id": "r1"}])
    with app.engine.begin() as conn:  # it ended an hour ago and was never evaluated
        conn.execute(store.run_checks.delete())
        conn.execute(store.agent_trajectories.update().values(updated_at=datetime.utcnow() - timedelta(hours=1)))
    life = app.get("/v1/agents/lifecycle", params=SRC).json()
    assert life["awaiting_evaluation"] == 1 and life["oldest_awaiting_minutes"] >= 59
    assert lifecycle.check_backlog(app.engine, 10) == {"t": {"waiting": 1, "oldest_minutes": pytest.approx(60, abs=1)}}
    alerts = app.get("/v1/alerts", params={"source": "events:t"}).json()
    alert = next(a for a in alerts if a["kind"] == "evaluation")
    assert alert["state"] == "open" and "1 agent run ended but isn't evaluated" in alert["message"]
    lifecycle.sweep(app.engine)
    assert lifecycle.check_backlog(app.engine, 10) == {}
    alerts = app.get("/v1/alerts", params={"source": "events:t", "state": "all"}).json()
    assert all(a["state"] == "resolved" for a in alerts if a["kind"] == "evaluation")


def test_an_evaluator_that_stopped_running_is_missing_against_the_baseline(app):
    ev = []
    for i in range(6):
        ev.append(check(i, "before", f"c{i}", "pass", evaluator="exact@1", field="total"))
        ev.append(check(i, "before", f"c{i}", "pass", evaluator="faithful@2", field="faithful"))
        ev.append(check(i, "before", f"c{i}", "pass", evaluator="tone@1", field="tone"))
        ev.append(check(i, "after", f"c{i}", "pass", evaluator="exact@1", field="total"))  # faithful@2 never ran
        ev.append(check(i, "after", f"c{i}", "pass", evaluator="tone@2", field="tone"))  # a new version: fine
    ev.append(check(0, "unrelated", "c0", "pass", evaluator="faithful@2", field="faithful"))  # another run's
    assert app.post("/v1/ingest", json=ev, headers=H).status_code == 200
    out = app.get("/v1/evals/runs/after/verdicts", params={**SRC, "baseline": "before"}).json()
    assert out["counts"]["MISSING"] == 6
    gone = [c for c in out["checks"] if c["verdict"] == "MISSING"]
    assert {c["evaluator"] for c in gone} == {"faithful@2"} and {c["field"] for c in gone} == {"faithful"}
    assert gone[0]["reason"] == "faithful@2 reported on 6 of these cases in the baseline, none in this run"
    st = app.get("/v1/evals/runs/after/stability", params={**SRC, "baseline": "before"}).json()
    assert st["outcome"] == "rerun" and any("never arrived from faithful@2" in r for r in st["reasons"])


def test_the_same_judgement_sent_twice_counts_once(app):
    def judged(i, status, score, attempt=0, run_id="trace-1"):
        return {**check(i, "r", "c1", status, attempt=attempt), "score": score, "run_id": run_id}
    r = app.post("/v1/ingest", json=[judged(0, "fail", 1), judged(1, "fail", 1)], headers=H)  # a retried job
    assert r.json()["duplicate_checks"] == 1
    r = app.post("/v1/ingest", json=[judged(2, "fail", 1)], headers=H)  # and again, in a later batch
    assert r.json()["duplicate_checks"] == 1
    assert "duplicate_checks" not in app.post("/v1/ingest", json=[judged(0, "fail", 1)], headers=H).json()  # a resend
    c = app.get("/v1/evals/runs/r/verdicts", params=SRC).json()["checks"]
    assert [(x["verdict"], x["attempts"]) for x in c] == [("FAIL", 1)]
    # A second attempt is a real attempt; a different judgement of the same output is a contradiction.
    app.post("/v1/ingest", json=[judged(3, "fail", 1, attempt=1), judged(4, "pass", 5)], headers=H)
    c = app.get("/v1/evals/runs/r/verdicts", params=SRC).json()["checks"]
    assert [(x["verdict"], x["attempts"]) for x in c] == [("EVALUATOR_ERROR", 3)]
