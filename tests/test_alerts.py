import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from sqlalchemy import select

from assay import alerts, store
from assay.alerts import learn_band, slo_for


@pytest.fixture
def engine(tmp_path):
    return store.make_engine(f"sqlite:///{tmp_path / 'a.db'}")


T0 = datetime(2026, 9, 1)


def add_run(engine, day, rows, source="s"):
    """rows: list of (measure_id, dimension, slice_value, value, n)."""
    with engine.begin() as c:
        rid = c.execute(store.measure_runs.insert().values(
            started_at=T0 + timedelta(days=day), source=source,
            window_start=T0, window_end=T0)).inserted_primary_key[0]
        c.execute(store.measure_results.insert(), [
            dict(run_id=rid, measure_id=m, status="measured", dimension=d, slice_value=v, value=val, n=n)
            for m, d, v, val, n in rows])
    return rid


def all_alerts(engine):
    with engine.connect() as c:
        return [dict(r._mapping) for r in c.execute(select(store.alerts))]


# ---------- bands ----------

def test_band_needs_history():
    assert learn_band([0.5, 0.5, 0.5], "ratio") is None


def test_flat_history_still_has_a_width():
    b = learn_band([0.0] * 8, "ratio")
    assert b.high > 0  # a zero-width band would flag every ordinary run


def test_sampling_error_widens_band_for_small_samples():
    hist = [0.01] * 8
    small, large = learn_band(hist, "ratio", n=100), learn_band(hist, "ratio", n=100000)
    assert small.high > large.high


# ---------- lifecycle ----------

def test_alert_goes_pending_then_open_then_resolved(engine):
    notes = []
    notify = lambda event, a: notes.append((event, a["measure_id"]))
    for d in range(6):
        alerts.evaluate_run(engine, add_run(engine, d, [("stage_failure_rate", None, None, 0.01, 1000)]),
                            notify=notify)
    assert all_alerts(engine) == []

    alerts.evaluate_run(engine, add_run(engine, 6, [("stage_failure_rate", None, None, 0.20, 1000)]), notify=notify)
    assert [a["state"] for a in all_alerts(engine)] == ["pending"] and notes == []

    alerts.evaluate_run(engine, add_run(engine, 7, [("stage_failure_rate", None, None, 0.21, 1000)]), notify=notify)
    assert [a["state"] for a in all_alerts(engine)] == ["open"]
    assert notes == [("opened", "stage_failure_rate")]

    alerts.evaluate_run(engine, add_run(engine, 8, [("stage_failure_rate", None, None, 0.01, 1000)]), notify=notify)
    a = all_alerts(engine)[0]
    assert a["state"] == "resolved" and a["resolved_at"] is not None
    assert notes[-1] == ("resolved", "stage_failure_rate")


def test_one_run_blip_never_fires(engine):
    for d in range(6):
        alerts.evaluate_run(engine, add_run(engine, d, [("stage_failure_rate", None, None, 0.01, 1000)]))
    alerts.evaluate_run(engine, add_run(engine, 6, [("stage_failure_rate", None, None, 0.3, 1000)]))
    alerts.evaluate_run(engine, add_run(engine, 7, [("stage_failure_rate", None, None, 0.01, 1000)]))
    assert all_alerts(engine) == []


def test_improvement_does_not_alert(engine):
    for d in range(6):
        alerts.evaluate_run(engine, add_run(engine, d, [("stage_failure_rate", None, None, 0.2, 1000)]))
    for d in (6, 7):
        alerts.evaluate_run(engine, add_run(engine, d, [("stage_failure_rate", None, None, 0.0, 1000)]))
    assert all_alerts(engine) == []


def test_neutral_measure_alerts_both_ways(engine):
    for d in range(6):
        alerts.evaluate_run(engine, add_run(engine, d, [("document_volume", None, None, 1000, 1000)]))
    for d in (6, 7):  # outage: volume drops to zero, and zero still counts
        alerts.evaluate_run(engine, add_run(engine, d, [("document_volume", None, None, 0, 0)]))
    assert [a["state"] for a in all_alerts(engine)] == ["open"]


def test_small_slices_are_not_judged(engine):
    for d in range(8):
        alerts.evaluate_run(engine, add_run(engine, d, [("stage_failure_rate", "stage", "x", 0.5 if d > 5 else 0.0, 5)]))
    assert all_alerts(engine) == []


# ---------- SLOs ----------

def slo(measure, dim=None, val=None, target=0.9, source="*"):
    return dict(id=1, source=source, measure_id=measure, dimension=dim, slice_value=val, target=target, note=None)


def test_most_specific_slo_wins():
    slos = [slo("m", target=0.9), slo("m", "segment", None, 0.8), slo("m", "segment", "acme", 0.7)]
    assert slo_for(slos, "m", None, None)["target"] == 0.9
    assert slo_for(slos, "m", "segment", "globex")["target"] == 0.8
    assert slo_for(slos, "m", "segment", "acme")["target"] == 0.7
    assert slo_for(slos, "m", "stage", "x") is None


def test_source_specific_slo_beats_global():
    slos = [slo("m", target=0.9), slo("m", target=0.5, source="s")]
    assert slo_for(slos, "m", None, None)["target"] == 0.5


def test_slo_breach_fires_without_history(engine):
    with engine.begin() as c:
        c.execute(store.slos.insert().values(source="*", measure_id="cost_coverage", target=0.99,
                                             updated_at=T0))
    for d in (0, 1):
        alerts.evaluate_run(engine, add_run(engine, d, [("cost_coverage", None, None, 0.7, 500)]))
    a = all_alerts(engine)
    assert len(a) == 1 and a[0]["kind"] == "slo" and a[0]["state"] == "open" and "SLO" in a[0]["message"]


# ---------- webhook ----------

def test_webhook_posts_slack_payload():
    received = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            received.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    send = alerts.webhook_notifier(f"http://127.0.0.1:{srv.server_port}/hook", public_url="https://assay.internal")
    send("opened", dict(id=1, kind="anomaly", source="sql", measure_id="handoff_loss",
                        dimension="segment", slice_value="acme", value=0.9, message="Lost at the handoff is 90%."))
    srv.server_close()
    assert "Lost at the handoff is 90%." in received[0]["text"]
    assert "https://assay.internal/#measures/handoff_loss" in received[0]["text"]


def test_webhook_failure_is_swallowed():
    send = alerts.webhook_notifier("http://127.0.0.1:9/nothing-listens-here")
    send("opened", dict(id=1, kind="slo", source="s", measure_id="cost_coverage", dimension=None,
                        slice_value=None, value=0.5, message="x"))  # must not raise


def test_scheduler_runs_each_source_and_records_failures(tmp_path):
    from assay.config import Settings
    from assay.scheduler import Scheduler
    settings = Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", schedule_minutes=5,
                        schedule_sources=["events:x", "sql"])  # no ASSAY_SOURCE_URL configured
    eng = store.make_engine(settings.store_url)
    sched = Scheduler(eng, settings)
    sched.run_once()
    assert sched.last["events:x"]["ok"] is True
    assert sched.last["sql"]["ok"] is False and "ASSAY_SOURCE_URL" in sched.last["sql"]["error"]
    with eng.connect() as c:
        assert len(c.execute(select(store.measure_runs)).all()) == 1
