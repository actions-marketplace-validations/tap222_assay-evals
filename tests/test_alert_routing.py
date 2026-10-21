import json
from datetime import datetime, timedelta
import pytest
from fastapi.testclient import TestClient

from assay import alerts, store
from assay.api import create_app
from assay.config import Settings


@pytest.fixture
def test_db(tmp_path):
    db_path = tmp_path / "alerts_test.db"
    engine = store.make_engine(f"sqlite:///{db_path}")
    return engine, f"sqlite:///{db_path}"


def test_is_silenced_matching_and_expiry():
    now = datetime.utcnow()
    silences = [
        {
            "id": 1,
            "source": "events:prod",
            "measure_id": "time_to_complete_p90",
            "dimension": "segment",
            "slice_value": "enterprise",
            "kind": None,
            "until": now + timedelta(hours=2),
            "reason": "Database maintenance",
        },
        {
            "id": 2,
            "source": "events:prod",
            "measure_id": "stage_failure_rate",
            "dimension": None,
            "slice_value": None,
            "kind": None,
            "until": now - timedelta(minutes=5),  # expired
            "reason": "Past incident",
        },
    ]

    matching_alert = {
        "source": "events:prod",
        "measure_id": "time_to_complete_p90",
        "dimension": "segment",
        "slice_value": "enterprise",
        "kind": "anomaly",
        "message": "p90 spiked",
    }
    other_alert = {
        "source": "events:prod",
        "measure_id": "time_to_complete_p90",
        "dimension": "segment",
        "slice_value": "retail",
        "kind": "anomaly",
        "message": "p90 spiked",
    }
    expired_alert = {
        "source": "events:prod",
        "measure_id": "stage_failure_rate",
        "dimension": None,
        "slice_value": None,
        "kind": "anomaly",
        "message": "failures up",
    }

    assert alerts.is_silenced(matching_alert, silences, now=now) is not None
    assert alerts.is_silenced(matching_alert, silences, now=now)["id"] == 1
    assert alerts.is_silenced(other_alert, silences, now=now) is None
    assert alerts.is_silenced(expired_alert, silences, now=now) is None


def test_match_routes_by_team():
    routes = [
        {
            "id": 1,
            "team": "billing-eng",
            "source": "events:billing",
            "measure_id": None,
            "dimension": None,
            "slice_value": None,
            "channel_url": "https://hooks.slack.com/billing",
            "pagerduty_key": "pd-key-billing",
        },
        {
            "id": 2,
            "team": "core-infra",
            "source": "*",
            "measure_id": "stage_failure_rate",
            "dimension": None,
            "slice_value": None,
            "channel_url": "https://hooks.slack.com/infra",
            "pagerduty_key": None,
        },
    ]

    billing_alert = {"source": "events:billing", "measure_id": "cost_per_doc", "kind": "anomaly"}
    matched = alerts.match_routes(billing_alert, routes)
    assert len(matched) == 1
    assert matched[0]["team"] == "billing-eng"

    infra_alert = {"source": "events:other", "measure_id": "stage_failure_rate", "kind": "anomaly"}
    matched_infra = alerts.match_routes(infra_alert, routes)
    assert len(matched_infra) == 1
    assert matched_infra[0]["team"] == "core-infra"


def test_pagerduty_send_payload(monkeypatch):
    sent = []

    class MockResponse:
        def read(self):
            return b'{"status": "success", "message": "Event processed"}'

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    def mock_urlopen(req, timeout=5):
        payload = json.loads(req.data.decode("utf-8"))
        sent.append(payload)
        return MockResponse()

    monkeypatch.setattr("urllib.request.urlopen", mock_urlopen)

    res = alerts.pagerduty_send(
        routing_key="test-key-123",
        event_action="trigger",
        dedup_key="assay:test:dedup:1",
        summary="High latency alert triggered",
        severity="error",
        custom_details={"p90": 25.4},
    )
    assert res["status"] == "success"
    assert len(sent) == 1
    p = sent[0]
    assert p["routing_key"] == "test-key-123"
    assert p["event_action"] == "trigger"
    assert p["dedup_key"] == "assay:test:dedup:1"
    assert p["payload"]["summary"] == "High latency alert triggered"
    assert p["payload"]["custom_details"]["p90"] == 25.4


def test_routed_notifier_suppresses_silenced_and_dispatches_active(test_db, monkeypatch):
    engine, url = test_db
    # Insert a silence and a route
    now = datetime.utcnow()
    with engine.begin() as conn:
        conn.execute(store.alert_silences.insert().values(
            source="events:prod",
            measure_id="call_error_rate",
            dimension=None,
            slice_value=None,
            kind=None,
            reason="Planned upgrade",
            created_by="ops",
            until=now + timedelta(hours=4),
            created_at=now,
        ))
        conn.execute(store.alert_routes.insert().values(
            source="*",
            team="sre",
            measure_id="document_volume",
            dimension=None,
            slice_value=None,
            kind=None,
            channel_url="https://fake.slack/sre",
            format="slack",
            pagerduty_key="sre-pd-key",
            created_at=now,
        ))

    dispatched_pd = []
    monkeypatch.setattr(alerts, "pagerduty_send", lambda *args, **kwargs: dispatched_pd.append((args, kwargs)))

    notifier = alerts.routed_notifier(engine)

    # 1. Silenced alert:
    silenced_alert = {"source": "events:prod", "measure_id": "call_error_rate", "kind": "anomaly", "message": "Error rate up"}
    out1 = notifier("opened", silenced_alert)
    assert out1["silenced"] is True

    # 2. Active routed alert:
    active_alert = {"source": "events:prod", "measure_id": "document_volume", "kind": "anomaly", "message": "Volume drop"}
    out2 = notifier("opened", active_alert)
    assert out2["silenced"] is False
    assert "sre" in out2["paged_teams"]
    assert len(dispatched_pd) == 1


def test_api_alert_routing_and_acknowledgement(test_db):
    engine, url = test_db
    client = TestClient(create_app(Settings(store_url=url)))

    # 1. Insert alert in DB
    now = datetime.utcnow()
    with engine.begin() as conn:
        aid = conn.execute(store.alerts.insert().values(
            source="events:test",
            measure_id="call_error_rate",
            kind="anomaly",
            state="open",
            opened_at=now,
            last_seen_at=now,
            message="Error spike detected",
        )).inserted_primary_key[0]

    # 2. Acknowledge alert via API
    res_ack = client.post(f"/v1/alerts/{aid}/ack", json={"note": "Investigating with team"})
    assert res_ack.status_code == 200
    assert res_ack.json()["acknowledged"] is True

    # Verify alert state reflects ack
    alerts_list = client.get("/v1/alerts", params={"source": "events:test"}).json()
    assert alerts_list[0]["acknowledged_by"] is not None
    assert alerts_list[0]["acknowledgement_note"] == "Investigating with team"

    # 3. Add alert route via API
    res_route = client.post("/v1/alerts/routes", json={
        "team": "checkout",
        "source": "events:test",
        "measure_id": "call_error_rate",
        "channel_url": "https://hooks.slack.com/checkout",
        "pagerduty_key": "pd-checkout-1",
    })
    assert res_route.status_code == 200
    assert res_route.json()["team"] == "checkout"
    route_id = res_route.json()["id"]

    routes = client.get("/v1/alerts/routes", params={"source": "events:test"}).json()
    assert len(routes) == 1 and routes[0]["id"] == route_id

    # 4. Add alert silence via API
    res_silence = client.post("/v1/alerts/silences", json={
        "source": "events:test",
        "measure_id": "call_error_rate",
        "hours": 12.0,
        "reason": "Scheduled deployment",
    })
    assert res_silence.status_code == 200
    silence_id = res_silence.json()["id"]

    silences = client.get("/v1/alerts/silences", params={"source": "events:test"}).json()
    assert len(silences) == 1 and silences[0]["id"] == silence_id
