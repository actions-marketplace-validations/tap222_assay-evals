from datetime import datetime
import pytest
from sqlalchemy import create_engine, select

from assay import runaway, store


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'runaway.db'}")
    store.upgrade(eng)
    return eng


def test_mask_error():
    err1 = "Call failed to https://api.service.com/v1/orders/a1b2c3d4-e5f6-7890-abcd-ef1234567890: timeout after 30s"
    err2 = "Call failed to https://api.service.com/v1/orders/99887766-5544-3322-1100-aabbccddeeff: timeout after 60s"
    m1 = runaway.mask_error(err1)
    m2 = runaway.mask_error(err2)
    assert m1 == m2
    assert "<url>" in m1 or "<id>" in m1
    assert "<n>" in m1


def test_check_repeated_failure():
    # 4 failures: not runaway yet
    steps = [
        {"kind": "tool", "name": "fetch_user", "status": "error", "error": f"DB connection lost on host-{i}"}
        for i in range(4)
    ]
    assert runaway.check_repeated_failure(steps, threshold=5) is None

    # 5th failure: triggers runaway
    steps.append({"kind": "tool", "name": "fetch_user", "status": "error", "error": "DB connection lost on host-99"})
    res = runaway.check_repeated_failure(steps, threshold=5)
    assert res is not None
    assert res["kind"] == "repeated_failure"
    assert res["tool"] == "fetch_user"
    assert res["count"] == 5
    assert "Runaway loop" in res["message"]


def test_check_runaway_size():
    # Normal size
    assert runaway.check_runaway_size(steps_count=15, cost=0.25, baseline={"steps": 20, "cost": 0.5}) is None

    # Runaway steps
    res_steps = runaway.check_runaway_size(steps_count=100, cost=0.5, baseline={"steps": 20, "cost": 0.5})
    assert res_steps is not None
    assert res_steps["kind"] == "excessive_steps"

    # Runaway cost
    res_cost = runaway.check_runaway_size(steps_count=20, cost=8.0, baseline={"steps": 20, "cost": 0.5})
    assert res_cost is not None
    assert res_cost["kind"] == "excessive_cost"


def test_check_spend_rate():
    # Normal spend: $2/hr vs $2/hr normal
    assert runaway.check_spend_rate(hourly_spend=2.0, normal_hourly_spend=2.0) is None

    # Spend burst: $15/hr vs $2/hr normal (7.5x)
    burst = runaway.check_spend_rate(hourly_spend=15.0, normal_hourly_spend=2.0, threshold_factor=3.0)
    assert burst is not None
    assert burst["kind"] == "spend_spike"
    assert "7.5x normal" in burst["message"]


def test_check_slow_leak():
    # 20 runs, 15 finished with zero tool actions and incurred cost
    runs = [{"status": "completed", "tool_actions": 0, "cost": 0.15} for _ in range(15)] + \
           [{"status": "completed", "tool_actions": 3, "cost": 0.20} for _ in range(5)]
    leak = runaway.check_slow_leak(runs)
    assert leak is not None
    assert leak["kind"] == "slow_leak"
    assert leak["ratio"] == 0.75
    assert "75% of completed runs performed no actions" in leak["message"]


def test_check_and_alert_runaway(engine):
    notified = []

    def mock_notifier(event, alert):
        notified.append((event, alert))

    steps = [
        {"kind": "tool", "name": "charge_card", "status": "error", "error": f"Gateway timeout 504 on req {i}"}
        for i in range(6)
    ]

    alert = runaway.check_and_alert_runaway(engine, "tenant_test", "run_loop_1", steps, notifier=mock_notifier)
    assert alert is not None
    assert alert["measure_id"] == "runaway"
    assert alert["slice_value"] == "run_loop_1"
    assert len(notified) == 1
    assert notified[0][0] == "opened"

    # Check database persistence in store.alerts
    with engine.connect() as conn:
        rows = conn.execute(select(store.alerts).where(store.alerts.c.slice_value == "run_loop_1")).all()
        assert len(rows) == 1
        assert rows[0].measure_id == "runaway"
        assert rows[0].state == "open"
