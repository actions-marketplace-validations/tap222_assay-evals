import hashlib
import hmac
import json
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from assay import alerts, auth, store
from assay.api import create_app
from assay.config import Settings

ADMIN = "platform-admin-secret"


def app_client(tmp_path, **kw):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'a.db'}", **kw)))


def bearer(k):
    return {"Authorization": f"Bearer {k}"}


def make_key(c, tenant, scopes, name="test"):
    r = c.post("/v1/keys", headers=bearer(ADMIN), json={"name": name, "tenant": tenant, "scopes": scopes})
    assert r.status_code == 201, r.text
    return r.json()["key"]


def doc(i, **kw):
    return {"document_id": i, "received_at": datetime.utcnow().isoformat(), **kw}


# ---------- modes ----------

def test_open_mode_works_but_cannot_mint_keys(tmp_path):
    c = app_client(tmp_path)
    who = c.get("/v1/whoami").json()
    assert who["mode"] == "open" and who["auth_required"] is False
    assert c.post("/v1/events/documents", json=[doc("a")]).status_code == 200
    assert c.post("/v1/keys", json={"name": "x", "tenant": "t", "scopes": ["read"]}).status_code == 403


def test_first_key_turns_authentication_on(tmp_path):
    c = app_client(tmp_path)
    auth.create_key(c.app.state.engine, "acme", "cli", ["ingest"])  # what `assay keys create` does
    c.app.state  # noqa: B018
    # The "any keys yet" check is cached for a few seconds; a fresh app sees it at once.
    c2 = TestClient(create_app(c.app.state.settings))
    assert c2.get("/v1/measures").status_code == 401


def test_key_secret_is_shown_once_and_stored_hashed(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN)
    r = c.post("/v1/keys", headers=bearer(ADMIN), json={"name": "pipeline", "tenant": "acme", "scopes": ["ingest"]})
    secret = r.json()["key"]
    assert secret.startswith("ak_") and r.json()["prefix"] == secret[:10]
    listed = c.get("/v1/keys", headers=bearer(ADMIN)).json()
    assert "key" not in listed[0] and listed[0]["tenant"] == "acme"
    with c.app.state.engine.connect() as conn:
        row = conn.execute(select(store.api_keys)).first()
    assert row.key_hash == hashlib.sha256(secret.encode()).hexdigest() and secret not in str(tuple(row))


def test_unknown_scope_and_bad_tenant_rejected(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN)
    assert c.post("/v1/keys", headers=bearer(ADMIN), json={"name": "x", "tenant": "a", "scopes": ["god"]}).status_code == 422
    assert c.post("/v1/keys", headers=bearer(ADMIN), json={"name": "x", "tenant": "a b", "scopes": ["read"]}).status_code == 422


# ---------- scopes and isolation ----------

def test_scopes_are_enforced(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN)
    ing, rd, mg = (make_key(c, "acme", [s]) for s in ("ingest", "read", "manage"))
    assert c.post("/v1/events/documents", headers=bearer(ing), json=[doc("a")]).status_code == 200
    assert c.get("/v1/measures", headers=bearer(ing)).status_code == 403
    assert c.post("/v1/events/documents", headers=bearer(rd), json=[doc("b")]).status_code == 403
    assert c.post("/v1/runs", headers=bearer(rd), json={"source": "events:acme", "days": 1}).status_code == 403
    assert c.post("/v1/runs", headers=bearer(mg), json={"source": "events:acme", "days": 1}).status_code == 200
    assert c.get("/v1/runs/latest", headers=bearer(mg), params={"source": "events:acme"}).status_code == 200  # manage includes read
    assert c.post("/v1/keys", headers=bearer(mg), json={"name": "x", "scopes": ["read"]}).status_code == 403
    assert c.get("/v1/measures").status_code == 401
    assert c.get("/v1/measures", headers=bearer("ak_not_a_real_key")).status_code == 401


def test_tenant_keys_only_see_their_own_tenant(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN)
    acme_in, globex_in = make_key(c, "acme", ["ingest"]), make_key(c, "globex", ["ingest"])
    acme = make_key(c, "acme", ["manage"])
    # Same document id from two tenants: two separate records, neither overwrites the other.
    c.post("/v1/events/documents", headers=bearer(acme_in), json=[doc("inv-1", document_type="invoice")])
    c.post("/v1/events/documents", headers=bearer(globex_in), json=[doc("inv-1", document_type="receipt")])
    assert c.post("/v1/events/documents", headers=bearer(acme_in) | {"X-Tenant": "globex"},
                  json=[doc("x")]).status_code == 403
    c.post("/v1/runs", headers=bearer(ADMIN), json={"source": "events:globex", "days": 1})
    assert c.get("/v1/trace/inv-1", headers=bearer(acme), params={"source": "events:acme"}).json()["document"]["document_type"] == "invoice"
    for path in ("/v1/trace/inv-1", "/v1/coverage", "/v1/runs/latest", "/v1/overview"):
        assert c.get(path, headers=bearer(acme), params={"source": "events:globex"}).status_code == 403
    assert c.get("/v1/sources", headers=bearer(acme)).json()["configured"] == ["events:acme"]
    with c.app.state.engine.begin() as conn:
        conn.execute(store.alerts.insert().values(source="events:globex", measure_id="cost_coverage", kind="slo",
                                                  state="open", opened_at=datetime.utcnow(),
                                                  last_seen_at=datetime.utcnow(), message="globex only"))
    assert c.get("/v1/alerts", headers=bearer(acme)).json() == []
    assert len(c.get("/v1/alerts", headers=bearer(ADMIN)).json()) == 1


def test_tenant_settings_default_to_own_source(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN)
    acme = make_key(c, "acme", ["manage"])
    r = c.put("/v1/cost/rates", headers=bearer(acme), json={"rates": {"review_per_hour": 30}}).json()
    assert r["source"] == "events:acme"
    assert c.put("/v1/cost/rates", headers=bearer(acme), json={"source": "*", "rates": {"review_per_hour": 1}}).status_code == 403
    s = c.put("/v1/slos", headers=bearer(acme), json={"measure_id": "cost_coverage", "target": 0.9}).json()
    assert s["source"] == "events:acme"


def test_tenant_admin_manages_only_its_own_keys(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN)
    acme_admin = make_key(c, "acme", ["admin"])
    other = c.post("/v1/keys", headers=bearer(ADMIN), json={"name": "g", "tenant": "globex", "scopes": ["read"]}).json()
    assert c.post("/v1/keys", headers=bearer(acme_admin), json={"name": "x", "tenant": "globex", "scopes": ["read"]}).status_code == 403
    assert c.post("/v1/keys", headers=bearer(acme_admin), json={"name": "x", "tenant": "*", "scopes": ["read"]}).status_code == 403
    assert c.post("/v1/keys", headers=bearer(acme_admin), json={"name": "ci", "scopes": ["ingest"]}).json()["tenant"] == "acme"
    assert {k["tenant"] for k in c.get("/v1/keys", headers=bearer(acme_admin)).json()} == {"acme"}
    assert c.delete(f"/v1/keys/{other['id']}", headers=bearer(acme_admin)).status_code == 404


def test_revoked_and_expired_keys_stop_working(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN)
    r = c.post("/v1/keys", headers=bearer(ADMIN), json={"name": "x", "tenant": "acme", "scopes": ["read"]}).json()
    assert c.get("/v1/measures", headers=bearer(r["key"])).status_code == 200
    assert c.delete(f"/v1/keys/{r['id']}", headers=bearer(ADMIN)).status_code == 200
    assert c.get("/v1/measures", headers=bearer(r["key"])).status_code == 401
    _, old = auth.create_key(c.app.state.engine, "acme", "old", ["read"], expires_in_days=1)
    with c.app.state.engine.begin() as conn:
        conn.execute(store.api_keys.update().where(store.api_keys.c.name == "old")
                     .values(expires_at=datetime.utcnow() - timedelta(seconds=1)))
    assert c.get("/v1/measures", headers=bearer(old)).status_code == 401


def test_rate_limit(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN, rate_limit_per_min=3)
    k = make_key(c, "acme", ["read"])
    codes = [c.get("/v1/measures", headers=bearer(k)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert int(c.get("/v1/measures", headers=bearer(k)).headers["Retry-After"]) > 0


# ---------- ingest contract ----------

def test_batch_endpoint_is_idempotent(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN)
    k = make_key(c, "acme", ["ingest"])
    now = datetime.utcnow().isoformat()
    batch = {"documents": [doc("d1")],
             "stage_runs": [{"document_id": "d1", "stage": "ocr", "status": "success", "started_at": now}],
             "calls": [{"call_id": "c1", "stage": "ocr", "ts": now, "document_id": "d1"}],
             "extractions": [{"document_id": "d1", "field": "total", "has_positions": True}],
             "reviews": [{"review_id": "r1", "document_id": "d1", "ts": now, "minutes": 2}]}
    first = c.post("/v1/events", headers=bearer(k), json=batch).json()
    assert first["tenant"] == "acme" and first["ingested"] == {"documents": 1, "stage_runs": 1, "calls": 1,
                                                               "reviews": 1, "extractions": 1, "errors": 0,
                                                               "eval_results": 0, "prompts": 0}
    c.post("/v1/events", headers=bearer(k), json=batch)  # the pipeline retried
    with c.app.state.engine.connect() as conn:
        for t in (store.event_documents, store.event_stage_runs, store.event_calls, store.event_indexed,
                  store.event_reviews):
            assert len(conn.execute(select(t)).all()) == 1, t.name


def test_validation_is_strict_and_timestamps_are_utc(tmp_path):
    c = app_client(tmp_path)
    r = c.post("/v1/events/documents", json=[{"document_id": "a", "received_at": "2026-09-01T10:00:00Z",
                                               "documentType": "invoice"}])
    assert r.status_code == 422 and "documentType" in r.text
    assert c.post("/v1/events/calls", json=[{"call_id": "c", "stage": "s", "ts": "2026-09-01T10:00:00",
                                             "cost_usd": -1}]).status_code == 422
    c.post("/v1/events/documents", json=[{"document_id": "a", "received_at": "2026-09-01T12:00:00+02:00"}])
    with c.app.state.engine.connect() as conn:
        row = conn.execute(select(store.event_documents)).first()
    assert row.received_at == datetime(2026, 9, 1, 10, 0)


def test_oversized_batch_is_refused(tmp_path, monkeypatch):
    from assay import ingest
    monkeypatch.setattr(ingest, "MAX_BATCH", 2)
    c = app_client(tmp_path)
    assert c.post("/v1/events/documents", json=[doc(str(i)) for i in range(3)]).status_code == 413


# ---------- OpenTelemetry ----------

def otlp_payload(trace_id="t1"):
    ns = lambda s: str(int((datetime(2026, 9, 1) + timedelta(seconds=s)).timestamp() * 1e9))
    kv = lambda k, v: {"key": k, "value": {"stringValue": v} if isinstance(v, str) else {"intValue": str(v)}}
    return {"resourceSpans": [{"resource": {"attributes": [kv("service.version", "abc123")]}, "scopeSpans": [{"spans": [
        {"traceId": trace_id, "spanId": "root", "name": "process_document", "startTimeUnixNano": ns(0),
         "endTimeUnixNano": ns(60), "attributes": [kv("assay.document_id", "inv-9"), kv("assay.document_type", "invoice"),
                                                    kv("assay.page_count", 3)]},
        {"traceId": trace_id, "spanId": "s1", "parentSpanId": "root", "name": "extract", "startTimeUnixNano": ns(1),
         "endTimeUnixNano": ns(50), "attributes": [kv("assay.stage", "field_extraction")]},
        {"traceId": trace_id, "spanId": "llm1", "parentSpanId": "s1", "name": "chat", "startTimeUnixNano": ns(2),
         "endTimeUnixNano": ns(4), "attributes": [kv("gen_ai.request.model", "claude-sonnet-5"),
                                                  kv("gen_ai.response.model", "claude-opus-5-5")],
         "status": {"code": 2}},
    ]}]}]}


def test_otlp_traces_become_documents_stages_and_calls(tmp_path):
    c = app_client(tmp_path, admin_key=ADMIN)
    k = make_key(c, "acme", ["ingest"])
    r = c.post("/v1/otlp/v1/traces", headers=bearer(k), json=otlp_payload())
    assert r.status_code == 200 and r.json()["ingested"]["calls"] == 1
    tr = c.get("/v1/trace/inv-9", headers=bearer(ADMIN), params={"source": "events:acme"}).json()
    assert tr["document"]["document_type"] == "invoice" and tr["document"]["duration_s"] == 60
    [call] = tr["calls"]
    assert call["stage"] == "field_extraction" and call["model_served"] == "claude-opus-5-5"
    assert call["status"] == "error" and call["latency_ms"] == pytest.approx(2000)
    assert [s["stage"] for s in tr["stages"]] == ["field_extraction"]
    assert any("served by claude-opus-5-5" in f["text"] for f in tr["flags"])
    # an exporter retry doesn't duplicate anything
    c.post("/v1/otlp/v1/traces", headers=bearer(k), json=otlp_payload())
    assert len(c.get("/v1/trace/inv-9", headers=bearer(ADMIN), params={"source": "events:acme"}).json()["calls"]) == 1


def test_otlp_protobuf_is_refused_with_a_hint(tmp_path):
    c = app_client(tmp_path)
    r = c.post("/v1/otlp/v1/traces", content=b"\x0a\x00", headers={"Content-Type": "application/x-protobuf"})
    assert r.status_code == 415 and "json" in r.json()["detail"].lower()


# ---------- webhooks ----------

def test_alert_webhooks_are_signed():
    got = {}

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            got["body"] = self.rfile.read(int(self.headers["Content-Length"]))
            got["sig"], got["ts"] = self.headers["X-Assay-Signature"], self.headers["X-Assay-Timestamp"]
            self.send_response(200)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.handle_request, daemon=True).start()
    send = alerts.webhook_notifier(f"http://127.0.0.1:{srv.server_port}/", secret="s3cret")
    send("opened", dict(id=1, kind="slo", source="s", measure_id="cost_coverage", dimension=None,
                        slice_value=None, value=0.5, message="x"))
    srv.server_close()
    expected = hmac.new(b"s3cret", got["ts"].encode() + b"." + got["body"], hashlib.sha256).hexdigest()
    assert got["sig"] == f"sha256={expected}"
    assert json.loads(got["body"])["text"]
