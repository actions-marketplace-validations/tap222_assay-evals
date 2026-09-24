from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from assay.api import create_app
from assay.client import Assay
from assay.config import Settings

ADMIN = "root"
TEXT = ("INVOICE INV-7 Northwind Traders Date: 01 Sep 2026 Line items ... Subtotal 1,100.00 Tax 140.00 "
        "Total due $1,240.00 Page 1 of 1 Remit to Northwind Traders")


def app(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'e.db'}", admin_key=ADMIN)))


def key(c, tenant, scopes):
    return {"Authorization": "Bearer " + c.post("/v1/keys", headers={"Authorization": f"Bearer {ADMIN}"},
                                                  json={"name": "t", "tenant": tenant, "scopes": scopes}).json()["key"]}


def pipeline_batch(doc="inv-7", validated_total="1.24"):
    t = datetime.utcnow() - timedelta(hours=1)
    run = lambda i, stage, outputs: {"document_id": doc, "stage": stage, "status": "success", "sequence": i,
                                     "started_at": (t + timedelta(seconds=i)).isoformat(), "outputs": outputs}
    return {"documents": [{"document_id": doc, "received_at": t.isoformat(), "document_type": "invoice"}],
            "stage_runs": [run(0, "ocr", {"_text": TEXT}), run(1, "extract", {"total": "1,240.00"}),
                           run(2, "validate", {"total": validated_total})]}


def test_report_an_error_and_get_the_root_cause_back(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    c.post("/v1/events", headers=ing, json=pipeline_batch())
    r = c.post("/v1/errors", headers=ing, json={"document_id": "inv-7", "field": "total", "expected": "1240.00",
                                                 "observed": "1.24", "source": "customer"})
    assert r.status_code == 201
    loc = r.json()["localized"]
    assert loc["verdict"] == "corrupted" and loc["origin_stage"] == "validate"
    assert [t["state"] for t in loc["timeline"]] == ["absent", "correct", "wrong"]

    rd = key(c, "acme", ["read"])
    detail = c.get("/v1/errors/inv-7", headers=rd, params={"source": "events:acme"}).json()
    assert detail["steps"][2]["stage"] == "validate" and detail["errors"][0]["verdict"] == "corrupted"
    assert {"field": "total", "values": [None, "1,240.00", "1.24"]} in detail["lineage"]
    summary = c.get("/v1/errors", headers=rd, params={"source": "events:acme"}).json()
    assert summary["by_origin_stage"] == [["validate", 1]] and summary["stages"] == ["ocr", "extract", "validate"]


def test_who_can_report_and_see_errors(tmp_path):
    c = app(tmp_path)
    c.post("/v1/events", headers=key(c, "acme", ["ingest"]), json=pipeline_batch())
    body = {"document_id": "inv-7", "field": "total", "expected": "1240"}
    assert c.post("/v1/errors", headers=key(c, "acme", ["read"]), json=body).status_code == 403
    assert c.post("/v1/errors", headers=key(c, "acme", ["manage"]), json=body).status_code == 201  # a reviewer
    other = key(c, "globex", ["read"])
    assert c.get("/v1/errors", headers=other, params={"source": "events:acme"}).status_code == 403
    assert c.get("/v1/errors/inv-7", headers=other, params={"source": "events:acme"}).status_code == 403


def test_reporting_twice_is_one_error(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    c.post("/v1/events", headers=ing, json=pipeline_batch())
    for _ in range(2):
        c.post("/v1/errors", headers=ing, json={"document_id": "inv-7", "field": "total", "expected": "1240"})
    rd = key(c, "acme", ["read"])
    assert c.get("/v1/errors", headers=rd, params={"source": "events:acme"}).json()["errors"] == 1


def test_error_rate_and_origin_measures(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    c.post("/v1/events", headers=ing, json=pipeline_batch())
    c.post("/v1/events", headers=ing, json=pipeline_batch(doc="inv-8", validated_total="1240.00"))
    c.post("/v1/errors", headers=ing, json={"document_id": "inv-7", "field": "total", "expected": "1240"})
    run = c.post("/v1/runs", headers={"Authorization": f"Bearer {ADMIN}"},
                 json={"source": "events:acme", "days": 1}).json()["measures"]
    assert run["reported_error_rate"]["overall"]["value"] == 0.5
    origin = {r["slice"]: r["value"] for r in run["errors_by_origin"]["slices"]["origin_stage"]}
    assert origin == {"ocr": 0, "extract": 0, "validate": 1}  # every step gets a row, so a jump can alert


def test_unknown_document_is_recorded_but_not_traced(tmp_path):
    c = app(tmp_path)
    r = c.post("/v1/errors", headers=key(c, "acme", ["ingest"]),
               json={"document_id": "nope", "field": "total", "expected": "1"}).json()
    assert r["recorded"] and r["localized"] is None


def test_otel_span_attributes_carry_step_outputs(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    ns = lambda s: str(int((datetime(2026, 9, 1) + timedelta(seconds=s)).timestamp() * 1e9))
    kv = lambda k, v: {"key": k, "value": {"stringValue": v} if isinstance(v, str) else {"intValue": str(v)}}
    spans = [{"traceId": "t", "spanId": "root", "name": "doc", "startTimeUnixNano": ns(0), "endTimeUnixNano": ns(9),
              "attributes": [kv("assay.document_id", "inv-9")]},
             {"traceId": "t", "spanId": "a", "parentSpanId": "root", "name": "ocr", "startTimeUnixNano": ns(1),
              "endTimeUnixNano": ns(2), "attributes": [kv("assay.stage", "ocr"), kv("assay.sequence", 1),
                                                       kv("assay.output._text", TEXT)]},
             {"traceId": "t", "spanId": "b", "parentSpanId": "root", "name": "extract", "startTimeUnixNano": ns(3),
              "endTimeUnixNano": ns(4), "attributes": [kv("assay.stage", "extract"), kv("assay.sequence", 2),
                                                       kv("assay.output.total", "1,420.00")]}]
    c.post("/v1/otlp/v1/traces", headers=ing, json={"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]})
    loc = c.post("/v1/errors", headers=ing, json={"document_id": "inv-9", "field": "total",
                                                   "expected": "1240.00"}).json()["localized"]
    assert loc["verdict"] == "introduced" and loc["origin_stage"] == "extract"


def test_client_step_outputs_and_report_error(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    a = Assay("http://unused", strict=True,
              transport=lambda path, payload: c.post(path, json=payload, headers=ing).raise_for_status())
    a.document("inv-1", received_at=datetime.utcnow() - timedelta(minutes=5))
    with a.stage("inv-1", "ocr", sequence=1) as step:
        step.output("_text", TEXT)
    with a.stage("inv-1", "extract", sequence=2) as step:
        step.outputs.update(total="1,420.00", vendor="Northwind Traders")
    a.report_error("inv-1", "total", expected="1,240.00", observed="1,420.00", source="qa")
    a.flush()
    detail = c.get("/v1/errors/inv-1", headers={"Authorization": f"Bearer {ADMIN}"},
                   params={"source": "events:acme"}).json()
    assert detail["errors"][0]["verdict"] == "introduced" and detail["errors"][0]["origin_stage"] == "extract"
