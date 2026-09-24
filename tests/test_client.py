from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from assay.api import create_app
from assay.client import Assay
from assay.config import Settings


@pytest.fixture
def server(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'c.db'}")))


def through(server, tenant="sdk"):
    def send(path, batch):
        r = server.post(path, json=batch, headers={"X-Tenant": tenant})
        r.raise_for_status()
    return send


def test_client_records_a_document_end_to_end(server):
    a = Assay("http://unused", tenant="sdk", transport=through(server), strict=True)
    start = datetime.utcnow()
    a.document("inv-1", received_at=start, document_type="invoice", segment="acme", page_count=2)
    with a.stage("inv-1", "field_extraction"):
        pass
    with pytest.raises(ValueError):
        with a.stage("inv-1", "validation"):
            raise ValueError("boom")
    a.call("inv-1", stage="field_extraction", model_declared="m", model_served="m",
           latency_ms=900, cost_usd=0.01, status="success")
    a.review("inv-1", minutes=3)
    a.document("inv-1", received_at=start, completed_at=datetime.utcnow())
    a.flush()
    run = server.post("/v1/runs", json={"source": "events:sdk", "days": 1}).json()["measures"]
    assert run["document_volume"]["overall"]["value"] == 1
    assert run["stage_failure_rate"]["overall"]["value"] == 0.5
    assert run["human_touch_rate"]["overall"]["value"] == 1.0
    assert run["time_to_complete_p90"]["status"] == "measured"


def test_failed_send_is_kept_for_the_next_flush():
    calls = []

    def flaky(path, batch):
        calls.append(len(batch))
        if len(calls) == 1:
            raise OSError("network down")

    a = Assay("http://unused", transport=flaky)
    a.call("d", stage="ocr")
    a.flush()           # fails quietly
    a.flush()           # retried
    assert calls == [1, 1]


def test_batches_flush_automatically():
    sent = []
    a = Assay("http://unused", batch_size=3, transport=lambda p, b: sent.append((p, len(b))))
    for i in range(7):
        a.call(f"d{i}", stage="ocr")
    assert sent == [("/v1/events/calls", 3), ("/v1/events/calls", 3)]
