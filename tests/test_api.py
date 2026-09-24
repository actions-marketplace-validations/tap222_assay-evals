from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from assay.api import create_app
from assay.config import Settings
from assay.sources.docai_core import DocAICoreSource


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}"))
    return TestClient(app)


def test_ingest_then_run_then_read(client):
    now = datetime.utcnow()
    calls = [{"call_id": f"c{i}", "stage": "indexing", "ts": (now - timedelta(hours=1)).isoformat(),
              "cost_usd": 0.01 if i % 2 else None, "county": "Cook IL"} for i in range(10)]
    assert client.post("/v1/events/calls", json=calls, headers={"X-Tenant": "acme"}).json() == {"ingested": 10}
    # Re-sending the same calls updates rather than duplicates.
    client.post("/v1/events/calls", json=calls, headers={"X-Tenant": "acme"})

    run = client.post("/v1/runs", json={"source": "events:acme", "days": 1}).json()
    cost = run["measures"]["cost_coverage"]
    assert cost["status"] == "measured" and cost["overall"]["value"] == 0.5 and cost["overall"]["n"] == 10
    assert run["measures"]["handoff_loss"]["status"] == "unmeasured"

    hist = client.get("/v1/measures/cost_coverage/history", params={"source": "events:acme"}).json()
    assert len(hist["points"]) == 1 and hist["band"] is None  # too little history for a band


def test_tenants_are_isolated(client):
    now = datetime.utcnow().isoformat()
    client.post("/v1/events/calls", json=[{"call_id": "x", "stage": "s", "ts": now}], headers={"X-Tenant": "a"})
    run = client.post("/v1/runs", json={"source": "events:b", "days": 1}).json()
    assert run["measures"]["cost_coverage"]["status"] == "unmeasured"


def test_gate_requires_lineage(client):
    r = client.post("/v1/gates/evaluate", json={"lineage": {"prompt": "v3"}, "rules": [], "samples": {}})
    assert r.status_code == 422 and "model" in r.json()["detail"]


def test_gate_decision_is_recorded(client):
    base = [1.0] * 90 + [0.0] * 10
    body = {
        "lineage": {"prompt": "idx-v12", "model": "claude-sonnet-5", "build": "abc123", "corpus": "cert-2026-09"},
        "rules": [{"measure_id": "field_accuracy", "tolerance": 0.02, "min_n": 50}],
        "samples": {"field_accuracy": {"overall": {"baseline": base, "candidate": base}}},
        "identical_runs": {"field_accuracy": [base, base[:-1] + [1.0]]},
    }
    d = client.post("/v1/gates/evaluate", json=body).json()
    assert d["outcome"] == "advance"
    listed = client.get("/v1/gates").json()
    assert listed[0]["id"] == d["id"] and listed[0]["lineage"]["build"] == "abc123"


def test_api_key_enforced(tmp_path):
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", api_key="k")))
    assert c.get("/v1/measures").status_code == 401
    assert c.get("/v1/measures", headers={"X-API-Key": "k"}).status_code == 200


def test_docai_adapter_against_a_miniature_schema(tmp_path):
    """Runs the real adapter SQL against a SQLite stand-in for the docai schema."""
    from sqlalchemy import event

    eng = create_engine(f"sqlite:///{tmp_path / 'main.db'}")

    @event.listens_for(eng, "connect")  # SQLite stands in for Postgres's "docai" schema
    def attach(dbapi_conn, _):
        dbapi_conn.execute(f"ATTACH DATABASE '{tmp_path / 'docai_schema.db'}' AS docai")

    with eng.begin() as c:
        c.execute(text("CREATE TABLE docai.documents (id INTEGER PRIMARY KEY, created_at TIMESTAMP, completed_at TIMESTAMP,"
                       " status TEXT, processing_mode TEXT, file_hash TEXT, county TEXT, document_type TEXT)"))
        c.execute(text("CREATE TABLE docai.ai_api_calls (id INTEGER PRIMARY KEY, document_id INTEGER, stage_name TEXT,"
                       " created_at TIMESTAMP, model_requested TEXT, model_used TEXT, resolving_layer TEXT,"
                       " gate_reason TEXT, estimated_cost NUMERIC, code_revision TEXT, duration_ms NUMERIC, status TEXT)"))
        c.execute(text("CREATE TABLE docai.document_stage_executions (document_id INTEGER, stage_name TEXT,"
                       " status TEXT, started_at TIMESTAMP, completed_at TIMESTAMP)"))
        c.execute(text("INSERT INTO docai.document_stage_executions VALUES (1, 'indexing', 'success', :a, :b),"
                       " (1, 'highlighting', 'success', :b, :b)"),
                  {"a": datetime(2026, 9, 1, 0, 1), "b": datetime(2026, 9, 1, 0, 2)})
        c.execute(text("INSERT INTO docai.documents VALUES (1, :t, :t, 'completed', 'realtime', 'h1', 'Cook IL', 'deed')"),
                  {"t": datetime(2026, 9, 1)})
        c.execute(text("INSERT INTO docai.ai_api_calls VALUES (1, 1, 'indexing', :t, 'm', 'm', NULL, NULL, 0.02, NULL, 900, 'success'),"
                       " (2, 1, 'indexing', :t, 'm', 'n', 'fallback_1', 'timeout', NULL, NULL, 30000, 'timeout')"),
                  {"t": datetime(2026, 9, 1, 1)})

    src = DocAICoreSource("sqlite://", engine=eng)
    from assay.models import Window
    calls = src.calls(Window(datetime(2026, 8, 1), datetime(2026, 10, 1)))
    assert len(calls) == 2 and calls[0].county == "Cook IL" and calls[0].cost_usd == 0.02
    assert calls[1].model_served == "n" and calls[1].gate_reason == "timeout"
    assert calls[1].latency_ms == 30000.0 and calls[1].status == "timeout"
    assert src.calls(Window(datetime(2026, 1, 1), datetime(2026, 2, 1))) == []
    doc, runs, doc_calls = src.document_detail("1")
    assert doc.county == "Cook IL" and len(doc_calls) == 2
    assert [(r.stage, r.did_work) for r in runs] == [("indexing", True), ("highlighting", False)]


def _seed_small(client, tenant="t"):
    now = datetime.utcnow()
    docs = [{"document_id": f"d{i}", "received_at": (now - timedelta(hours=5)).isoformat(),
             "completed_at": (now - timedelta(hours=5 - i % 4)).isoformat() if i % 5 else None,
             "county": "Cook IL" if i % 2 else "Harris TX", "file_hash": f"h{i}",
             "delivered_downstream": i % 3 != 0} for i in range(40)]
    calls = [{"call_id": f"c{i}", "stage": "indexing", "ts": (now - timedelta(hours=4)).isoformat(),
              "document_id": f"d{i}", "cost_usd": 0.01, "status": "success", "latency_ms": 900.0}
             for i in range(40)]
    h = {"X-Tenant": tenant}
    client.post("/v1/events/documents", json=docs, headers=h)
    client.post("/v1/events/calls", json=calls, headers=h)
    return client.post("/v1/runs", json={"source": f"events:{tenant}", "days": 1}).json()


def test_overview_reports_slo_state(client):
    client.put("/v1/slos", json={"source": "events:t", "measure_id": "handoff_loss",
                                 "dimension": "county", "target": 0.05})
    _seed_small(client)
    ov = client.get("/v1/overview", params={"source": "events:t"}).json()
    slo = next(s for s in ov["slos"] if s["measure_id"] == "handoff_loss")
    assert slo["state"] == "unmeasured"  # 16 finished docs per county is below the minimum of 30
    assert ov["counts"]["measured"] > 0 and ov["open_alerts"] == []


def test_slo_validation(client):
    assert client.put("/v1/slos", json={"measure_id": "document_volume", "target": 5}).status_code == 422
    assert client.put("/v1/slos", json={"measure_id": "cost_coverage", "dimension": "county",
                                        "target": 0.9}).status_code == 422  # not sliced by county
    client.put("/v1/slos", json={"measure_id": "cost_coverage", "target": 0.9})
    again = client.put("/v1/slos", json={"measure_id": "cost_coverage", "target": 0.95}).json()
    slos = client.get("/v1/slos").json()
    assert len(slos) == 1 and slos[0]["target"] == 0.95  # replaced, not duplicated
    assert client.delete(f"/v1/slos/{again['id']}").status_code == 200


def test_trace_and_document_finder(client):
    _seed_small(client)
    lost = client.get("/v1/documents", params={"source": "events:t", "view": "lost", "days": 1}).json()
    assert lost and all(int(d["document_id"][1:]) % 3 == 0 for d in lost)
    tr = client.get(f"/v1/trace/{lost[0]['document_id']}", params={"source": "events:t"}).json()
    assert any("no record downstream" in f["text"] for f in tr["flags"])
    assert len(tr["calls"]) == 1
    assert client.get("/v1/trace/nope", params={"source": "events:t"}).status_code == 404


def test_auto_demo_seeds_once_on_first_request(tmp_path):
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'd.db'}", auto_demo=True)))
    assert "events:demo" in c.get("/v1/sources").json()["with_results"]
    runs = c.get("/v1/overview", params={"source": "events:demo"}).json()["run"]["run_id"]
    c.get("/v1/sources")  # later requests don't reseed
    assert c.get("/v1/overview", params={"source": "events:demo"}).json()["run"]["run_id"] == runs


def test_cron_requires_secret_and_runs_sources(tmp_path):
    s = Settings(store_url=f"sqlite:///{tmp_path / 'c.db'}", cron_secret="shh", schedule_sources=["events:x"])
    c = TestClient(create_app(s))
    assert c.get("/v1/cron").status_code == 401
    assert c.get("/v1/cron", headers={"Authorization": "Bearer wrong"}).status_code == 401
    out = c.get("/v1/cron", headers={"Authorization": "Bearer shh"}).json()
    assert out["last"]["events:x"]["ok"] is True
