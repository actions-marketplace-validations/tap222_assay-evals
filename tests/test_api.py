from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from assay.api import create_app
from assay.config import Settings
from assay.sources.sql import SQLSource


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}"))
    return TestClient(app)


def test_ingest_then_run_then_read(client):
    now = datetime.utcnow()
    calls = [{"call_id": f"c{i}", "stage": "indexing", "ts": (now - timedelta(hours=1)).isoformat(),
              "cost_usd": 0.01 if i % 2 else None, "segment": "acme"} for i in range(10)]
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
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", admin_key="k")))
    assert c.get("/v1/measures").status_code == 401
    assert c.get("/v1/measures", headers={"X-API-Key": "k"}).status_code == 200
    assert c.get("/v1/measures", headers={"Authorization": "Bearer k"}).status_code == 200


def _reference_db(path):
    """A pipeline database laid out in Assay's reference schema."""
    eng = create_engine(f"sqlite:///{path}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE documents (document_id TEXT PRIMARY KEY, received_at TIMESTAMP,"
                       " completed_at TIMESTAMP, status TEXT, processing_mode TEXT, file_hash TEXT,"
                       " segment TEXT, document_type TEXT, page_count INTEGER)"))
        c.execute(text("CREATE TABLE stage_runs (document_id TEXT, stage TEXT, status TEXT,"
                       " started_at TIMESTAMP, finished_at TIMESTAMP, did_work BOOLEAN)"))
        c.execute(text("CREATE TABLE model_calls (call_id TEXT, stage TEXT, ts TIMESTAMP, document_id TEXT,"
                       " model_declared TEXT, model_served TEXT, resolving_layer TEXT, gate_reason TEXT,"
                       " cost_usd NUMERIC, code_revision TEXT, latency_ms NUMERIC, status TEXT)"))
        c.execute(text("CREATE TABLE extractions (document_id TEXT, source_positions TEXT)"))
        c.execute(text("INSERT INTO documents VALUES ('1', :t, :t, 'completed', 'realtime', 'h1', 'acme', 'invoice', 3)"),
                  {"t": datetime(2026, 9, 1)})
        c.execute(text("INSERT INTO stage_runs VALUES ('1', 'extraction', 'success', :a, :b, 1),"
                       " ('1', 'highlighting', 'success', :b, :b, NULL)"),
                  {"a": datetime(2026, 9, 1, 0, 1), "b": datetime(2026, 9, 1, 0, 2)})
        c.execute(text("INSERT INTO model_calls VALUES ('c1', 'extraction', :t, '1', 'm', 'm', NULL, NULL, 0.02, NULL, 900, 'success'),"
                       " ('c2', 'extraction', :t, '1', 'm', 'n', 'fallback_1', 'timeout', NULL, NULL, 30000, 'timeout')"),
                  {"t": datetime(2026, 9, 1, 1)})
        c.execute(text("INSERT INTO extractions VALUES ('1', '[[0,0,10,10]]'), ('1', NULL)"))
    return eng


def test_sql_source_reads_the_reference_schema(tmp_path):
    from assay.models import Window
    from assay.sources.sql import load_mapping
    mapping = load_mapping()
    mapping["noop_stages"] = ["highlighting"]
    src = SQLSource("sqlite://", engine=_reference_db(tmp_path / "p.db"), mapping=mapping)
    calls = src.calls(Window(datetime(2026, 8, 1), datetime(2026, 10, 1)))
    assert len(calls) == 2 and calls[0].segment == "acme" and calls[0].cost_usd == 0.02
    assert calls[1].model_served == "n" and calls[1].gate_reason == "timeout"
    assert calls[1].latency_ms == 30000.0 and calls[1].status == "timeout"
    assert src.calls(Window(datetime(2026, 1, 1), datetime(2026, 2, 1))) == []
    assert [x.has_positions for x in src.indexed(None)] == [True, False]
    doc, runs, doc_calls = src.document_detail("1")
    assert doc.document_type == "invoice" and doc.page_count == 3 and len(doc_calls) == 2
    assert src.reviews(None) is None  # not configured: people cost is "not recorded"
    assert [(r.stage, r.did_work) for r in runs] == [("extraction", True), ("highlighting", False)]
    assert all(err is None for fields in src.check().values() for err in fields.values())


def test_sql_source_with_a_custom_mapping(tmp_path):
    """Someone else's schema: different table and column names, some fields not recorded."""
    import json
    from assay.models import Window
    from assay.sources.sql import load_mapping
    eng = create_engine(f"sqlite:///{tmp_path / 'other.db'}")
    with eng.begin() as c:
        c.execute(text("CREATE TABLE jobs (id INTEGER, created TIMESTAMP, kind TEXT, customer TEXT)"))
        c.execute(text("CREATE TABLE llm_log (id INTEGER, job_id INTEGER, step TEXT, at TIMESTAMP, model TEXT, price REAL)"))
        c.execute(text("INSERT INTO jobs VALUES (7, :t, 'receipt', 'globex')"), {"t": datetime(2026, 9, 1)})
        c.execute(text("INSERT INTO llm_log VALUES (1, 7, 'ocr', :t, 'm', 0.5)"), {"t": datetime(2026, 9, 1, 1)})
    path = tmp_path / "mapping.json"
    path.write_text(json.dumps({
        "documents": {"from": "jobs j", "columns": {
            "document_id": "j.id", "received_at": "j.created", "completed_at": "NULL", "status": "NULL",
            "processing_mode": "NULL", "file_hash": "NULL", "segment": "j.customer", "document_type": "j.kind",
            "page_count": "NULL"}},
        "calls": {"from": "llm_log l LEFT JOIN jobs j ON j.id = l.job_id", "columns": {
            "call_id": "l.id", "stage": "l.step", "ts": "l.at", "document_id": "l.job_id",
            "model_declared": "NULL", "model_served": "l.model", "resolving_layer": "NULL", "gate_reason": "NULL",
            "cost_usd": "l.price", "code_revision": "NULL", "latency_ms": "NULL", "status": "NULL",
            "segment": "j.customer", "document_type": "j.kind"}},
    }), encoding="utf-8")
    src = SQLSource("sqlite://", engine=eng, mapping=load_mapping(str(path)))
    w = Window(datetime(2026, 8, 1), datetime(2026, 10, 1))
    [call] = src.calls(w)
    assert call.segment == "globex" and call.cost_usd == 0.5 and call.document_id == "7"
    [doc] = src.documents(w)
    assert doc.document_type == "receipt"
    report = src.check()
    assert report["calls"]["cost_usd"] is None  # mapped and working
    assert report["stage_runs"]["stage"] is not None  # stage_runs table doesn't exist: reported, not raised


def test_unknown_mapping_key_is_rejected(tmp_path):
    from assay.sources.sql import load_mapping
    path = tmp_path / "m.json"
    path.write_text('{"calls_typo": {}}', encoding="utf-8")
    with pytest.raises(ValueError, match="calls_typo"):
        load_mapping(str(path))


def _seed_small(client, tenant="t"):
    now = datetime.utcnow()
    docs = [{"document_id": f"d{i}", "received_at": (now - timedelta(hours=5)).isoformat(),
             "completed_at": (now - timedelta(hours=5 - i % 4)).isoformat() if i % 5 else None,
             "segment": "acme" if i % 2 else "globex", "file_hash": f"h{i}",
             "delivered_downstream": i % 3 != 0} for i in range(40)]
    calls = [{"call_id": f"c{i}", "stage": "extraction", "ts": (now - timedelta(hours=4)).isoformat(),
              "document_id": f"d{i}", "cost_usd": 0.01, "status": "success", "latency_ms": 900.0}
             for i in range(40)]
    h = {"X-Tenant": tenant}
    client.post("/v1/events/documents", json=docs, headers=h)
    client.post("/v1/events/calls", json=calls, headers=h)
    return client.post("/v1/runs", json={"source": f"events:{tenant}", "days": 1}).json()


def test_overview_reports_slo_state(client):
    client.put("/v1/slos", json={"source": "events:t", "measure_id": "handoff_loss",
                                 "dimension": "segment", "target": 0.05})
    _seed_small(client)
    ov = client.get("/v1/overview", params={"source": "events:t"}).json()
    slo = next(s for s in ov["slos"] if s["measure_id"] == "handoff_loss")
    assert slo["state"] == "unmeasured"  # 16 finished docs per segment is below the minimum of 30
    assert ov["counts"]["measured"] > 0 and ov["open_alerts"] == []


def test_slo_validation(client):
    assert client.put("/v1/slos", json={"measure_id": "document_volume", "target": 5}).status_code == 422
    assert client.put("/v1/slos", json={"measure_id": "cost_coverage", "dimension": "segment",
                                        "target": 0.9}).status_code == 422  # not sliced by segment
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


def test_document_update_keeps_fields_it_does_not_send(client):
    now = datetime.utcnow()
    h = {"X-Tenant": "u"}
    client.post("/v1/events/documents", headers=h, json=[
        {"document_id": "d1", "received_at": now.isoformat(), "document_type": "invoice", "segment": "acme"}])
    client.post("/v1/events/documents", headers=h, json=[
        {"document_id": "d1", "received_at": now.isoformat(), "completed_at": now.isoformat()}])
    from assay.sources.events import EventsSource
    from assay.models import Window
    [d] = EventsSource(client.app.state.engine, "u").documents(Window(now - timedelta(1), now + timedelta(1)))
    assert d.document_type == "invoice" and d.segment == "acme" and d.completed_at is not None


def test_coverage_says_what_to_add(client):
    now = datetime.utcnow()
    client.post("/v1/events/documents", headers={"X-Tenant": "c"}, json=[
        {"document_id": f"d{i}", "received_at": now.isoformat()} for i in range(5)])
    client.post("/v1/events/calls", headers={"X-Tenant": "c"}, json=[
        {"call_id": "x", "stage": "ocr", "ts": now.isoformat(), "document_id": "d0"}])
    rep = client.get("/v1/coverage", params={"source": "events:c", "days": 1}).json()
    m = {x["id"]: x for x in rep["measures"]}
    assert m["document_volume"]["status"] == "partial"  # works, but no segment / document type
    assert any(i["field"] == "documents.segment" for i in m["document_volume"]["improve"])
    assert m["call_latency_p95"]["status"] == "blocked" and m["call_latency_p95"]["missing"] == ["calls.latency_ms"]
    assert m["human_touch_rate"]["missing"] == ["reviews"]
    assert m["handoff_loss"]["status"] == "blocked"
    assert rep["records"]["documents"]["rows"] == 5


def test_backfill_builds_history_once_and_quietly(tmp_path):
    notified = []
    s = Settings(store_url=f"sqlite:///{tmp_path / 'b.db'}")
    c = TestClient(create_app(s))
    now = datetime.utcnow()
    c.post("/v1/events/documents", headers={"X-Tenant": "b"}, json=[
        {"document_id": f"d{i}", "received_at": (now - timedelta(days=i % 10, hours=1)).isoformat()}
        for i in range(200)])
    out = c.post("/v1/backfill", json={"source": "events:b", "days": 10}).json()
    assert out["runs_created"] == 10
    again = c.post("/v1/backfill", json={"source": "events:b", "days": 10}).json()
    assert again["runs_created"] == 0 and again["skipped"] == 10
    hist = c.get("/v1/measures/document_volume/history", params={"source": "events:b"}).json()
    assert len(hist["points"]) == 10 and hist["band"] is not None  # a baseline exists on day one
    assert hist["points"] == sorted(hist["points"], key=lambda p: p["at"])


def test_views_start_with_core_tabs_and_save_for_everyone(client):
    got = client.get("/v1/views", params={"source": "events:acme"}).json()
    assert not got["saved"] and got["read_only"] is None
    assert got["config"]["views"]["agents"] == {"enabled": None, "tabs": ["overview", "diff", "failures", "agents", "alerts"]}
    assert "learn" in got["catalog"]["views"]["agents"]["tabs"]

    cfg = {"views": {"agents": {"enabled": True, "tabs": ["learn", "overview"]}, "documents": {"enabled": False}},
           "measure_groups": ["Cost"]}
    saved = client.put("/v1/views", json={"source": "events:acme", "config": cfg}).json()
    assert saved["saved"] and saved["config"]["views"]["agents"]["tabs"] == ["overview", "learn"]  # the dashboard's order
    assert saved["config"]["measure_groups"] == ["Cost"]
    assert client.get("/v1/views", params={"source": "events:acme"}).json()["config"] == saved["config"]
    assert not client.get("/v1/views", params={"source": "events:other"}).json()["saved"]  # per source

    bad = lambda c: client.put("/v1/views", json={"source": "events:acme", "config": c})
    assert bad({"views": {"agents": {"tabs": ["measures"]}}}).status_code == 422  # a documents tab
    assert bad({"views": {"agents": {"enabled": False}, "documents": {"enabled": False}}}).status_code == 422
    assert bad({"views": {"agents": {"enabled": True, "tabs": []}}}).status_code == 422
    assert bad({"views": {"billing": {}}}).status_code == 422

    assert not client.delete("/v1/views", params={"source": "events:acme"}).json()["saved"]


def test_an_open_server_keeps_the_demo_views_as_they_are(client):
    got = client.get("/v1/views", params={"source": "events:demo-agent"}).json()
    assert "open demo" in got["read_only"]
    r = client.put("/v1/views", json={"source": "events:demo-agent", "config": {"views": {}}})
    assert r.status_code == 403 and "open demo" in r.json()["detail"]
    assert client.put("/v1/views", json={"source": "events:mine", "config": {"views": {}}}).status_code == 200


def test_the_dashboard_is_sent_compressed(client):
    r = client.get("/", headers={"Accept-Encoding": "gzip"})
    assert r.status_code == 200 and r.headers["content-encoding"] == "gzip" and "<title>Assay</title>" in r.text
