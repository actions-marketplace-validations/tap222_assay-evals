"""Where Assay's evaluation connects to a pipeline (assay/workflow_eval.py, GET /v1/workflow)."""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from assay.api import create_app
from assay.config import Settings


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'w.db'}")))


def test_the_evaluation_is_drawn_onto_this_pipelines_own_steps(client):
    now = datetime.utcnow() - timedelta(hours=2)
    docs, runs = [], []
    for i in range(6):
        d = f"inv-{i}"
        docs.append({"document_id": d, "received_at": now.isoformat(), "completed_at": (now + timedelta(minutes=9)).isoformat()})
        for k, (stage, out) in enumerate((("ocr", {"text": "…"}), ("extract", {"total": "27.61", "date": "2026-09-01"}),
                                           ("review", None))):
            runs.append({"document_id": d, "stage": stage, "status": "success", "sequence": k, "outputs": out,
                         "started_at": (now + timedelta(minutes=k)).isoformat()})
    results = [{"run_id": "nightly", "case_id": f"inv-{i}", "document_id": f"inv-{i}", "field": "total",
                "status": "pass", "evaluator": "exact@1"} for i in range(6)]
    r = client.post("/v1/events", json={"documents": docs, "stage_runs": runs, "eval_results": results},
                    headers={"X-Tenant": "t"})
    assert r.status_code in (200, 201), r.text
    client.post("/v1/contracts", json={"source": "events:t", "kind": "before", "step": "extract", "other": "review"})

    ev = client.get("/v1/workflow", params={"source": "events:t", "days": 1}).json()["evaluation"]
    assert ev["connection"]["kind"] == "events" and "18 step runs" in ev["connection"]["detail"]
    assert ev["steps"]["extract"]["checked_fields"] == ["total"]  # a field this step produces, checked
    assert ev["steps"]["ocr"]["checked_fields"] == []
    assert ev["steps"]["review"]["contracts"] == ["extract runs before review"]
    by = {c["kind"]: c for c in ev["components"]}
    assert by["checks"]["steps"] == ["extract"] and by["checks"]["tone"] == "good"
    assert by["contracts"]["steps"] == ["extract", "review"] and by["contracts"]["status"] == "all hold"
    # What isn't connected says what to send.
    assert by["errors"]["status"] == "none reported" and "assay.correction()" in by["errors"]["hint"]
    assert by["prompts"]["status"] == "none recorded" and by["gates"]["status"] == "no decisions yet"
    assert by["measures"]["status"] == "not run yet"
