from datetime import datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import select

from assay import alerts, prompts, store
from assay.api import create_app
from assay.client import Assay
from assay.config import Settings
from assay.ingest import content_version

ADMIN = "root"
H = {"Authorization": f"Bearer {ADMIN}"}


def app(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'p.db'}", admin_key=ADMIN)))


def key(c, tenant, scopes):
    return {"Authorization": "Bearer " + c.post("/v1/keys", headers=H, json={
        "name": "t", "tenant": tenant, "scopes": scopes}).json()["key"]}


def traffic(c, hdr, version, n, bad_every=None, start_hours=48, doc_prefix=None):
    """n documents through one LLM step on `version`; every bad_every-th gets a reported error there."""
    now = datetime.utcnow()
    docs, runs, calls, errors = [], [], [], []
    for i in range(n):
        d = f"{doc_prefix or version}-{i}"
        t = now - timedelta(hours=start_hours - i * start_hours / n / 2)
        docs.append({"document_id": d, "received_at": t.isoformat(), "document_type": "invoice"})
        runs.append({"document_id": d, "stage": "extract", "status": "success", "sequence": 1,
                     "started_at": t.isoformat(), "outputs": {"total": "99" if bad_every and i % bad_every == 0 else "100"},
                     "prompt_id": "extract_fields", "prompt_version": version})
        calls.append({"call_id": f"c-{d}", "document_id": d, "stage": "extract", "ts": t.isoformat(),
                      "prompt_id": "extract_fields", "prompt_version": version, "latency_ms": 1000,
                      "cost_usd": 0.01, "model_served": "m", "model_declared": "m", "status": "success"})
        if bad_every and i % bad_every == 0:
            errors.append({"document_id": d, "field": "total", "expected": "100", "observed": "99"})
    r = c.post("/v1/events", headers=hdr, json={"documents": docs, "stage_runs": runs, "calls": calls, "errors": errors})
    assert r.status_code == 200, r.text


def test_content_version_is_stable():
    assert content_version("Extract the total.") == content_version("Extract the total.") != content_version("x")
    assert Assay.prompt_version("Extract the total.") == content_version("Extract the total.")


def test_versions_are_discovered_from_traffic_and_registered_from_ci(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    traffic(c, ing, "v12", 5, start_hours=96)
    assert c.post("/v1/prompts", headers=ing, json={"prompt_id": "extract_fields", "version": "v12",
                                                     "template": "Extract the total.", "note": "baseline"}).status_code == 201
    with c.app.state.engine.connect() as conn:
        [row] = conn.execute(select(store.prompt_versions)).all()
    assert row.tenant == "acme" and row.first_seen is not None and row.note == "baseline" and row.template
    # sending traffic again doesn't erase the registered template or note
    traffic(c, ing, "v12", 3, start_hours=10, doc_prefix="later")
    with c.app.state.engine.connect() as conn:
        row = conn.execute(select(store.prompt_versions)).first()
    assert row.note == "baseline" and row.template == "Extract the total."


def test_template_hash_is_used_when_no_version_given(tmp_path):
    c = app(tmp_path)
    r = c.post("/v1/prompts", headers=key(c, "acme", ["ingest"]), json={"prompt_id": "p", "template": "Hello"}).json()
    assert r["version"] == content_version("Hello")
    assert c.post("/v1/prompts", headers=H | {"X-Tenant": "acme"}, json={"prompt_id": "p"}).status_code == 422


def test_a_worse_version_is_flagged_and_a_better_one_credited(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    traffic(c, ing, "v12", 200, bad_every=50, start_hours=400)   # 2% error rate
    traffic(c, ing, "v13", 200, bad_every=5, start_hours=100)    # 20%
    out = c.get("/v1/prompts", headers=key(c, "acme", ["read"]), params={"source": "events:acme"}).json()
    [p] = out["prompts"]
    v12, v13 = p["versions"]
    assert (v12["version"], v13["version"]) == ("v12", "v13") and p["live_version"] == "v13"
    assert v12["error_rate"] == 0.02 and v13["error_rate"] == 0.2
    assert v13["vs_previous"]["previous"] == "v12" and v13["vs_previous"]["error_rate"]["verdict"] == "worse"
    assert v13["vs_previous"]["error_rate"]["low"] > 0


def test_too_few_documents_says_so(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    traffic(c, ing, "v1", 10, bad_every=2, start_hours=200)
    traffic(c, ing, "v2", 10, start_hours=50)
    [p] = c.get("/v1/prompts", headers=H, params={"source": "events:acme"}).json()["prompts"]
    assert p["versions"][1]["vs_previous"]["error_rate"]["verdict"] == "too few"


def test_mix_adjustment_does_not_blame_a_version_for_harder_documents():
    # v2 gets far more contracts (hard), but within each type it's exactly as good as v1.
    a = {"invoice": [2, 100], "contract": [10, 50]}
    b = {"invoice": [1, 50], "contract": [20, 100]}
    s = prompts._standardized(a, b)
    assert abs(s["diff"]) < 1e-9 and s["low"] < 0 < s["high"]
    raw_a, raw_b = 12 / 150, 21 / 150
    assert raw_b - raw_a > 0.05  # a naive comparison would have called v2 worse


def test_regression_alert_opens_and_resolves(tmp_path):
    engine = store.make_engine(f"sqlite:///{tmp_path / 'r.db'}")
    with engine.begin() as conn:
        rid = conn.execute(store.measure_runs.insert().values(started_at=datetime.utcnow(), source="events:acme",
                                                              window_start=datetime.utcnow(),
                                                              window_end=datetime.utcnow())).inserted_primary_key[0]
    worse = {"prompts": [{"versions": [{"prompt": "p@v2", "prompt_id": "p", "version": "v2", "error_rate": 0.2,
                                         "documents": 200, "last_seen": None,
                                         "vs_previous": {"previous": "v1", "error_rate": {
                                             "verdict": "worse", "diff": 0.18, "low": 0.1, "high": 0.26}}}]}]}
    notes = []
    alerts.evaluate_prompt_regressions(engine, rid, worse, lambda e, a: notes.append(e))
    ok = {"prompts": [{"versions": [{**worse["prompts"][0]["versions"][0],
                                     "vs_previous": {"previous": "v1", "error_rate": {"verdict": "no clear difference"}}}]}]}
    too_few = {"prompts": [{"versions": [{**worse["prompts"][0]["versions"][0],
                                          "vs_previous": {"previous": "v1", "error_rate": {"verdict": "too few"}}}]}]}
    alerts.evaluate_prompt_regressions(engine, rid, too_few)  # not enough evidence to resolve: stays open
    with engine.connect() as conn:
        assert [a.state for a in conn.execute(select(store.alerts))] == ["open"]
    alerts.evaluate_prompt_regressions(engine, rid, ok, lambda e, a: notes.append(e))
    with engine.connect() as conn:
        a = conn.execute(select(store.alerts)).first()
    assert a.state == "resolved" and a.kind == "regression" and "p@v2" in a.message and notes == ["opened", "resolved"]


def test_errors_are_attributed_to_the_prompt_version_and_charts_get_markers(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    traffic(c, ing, "v12", 40, start_hours=200)
    traffic(c, ing, "v13", 40, bad_every=4, start_hours=40)
    detail = c.get("/v1/errors/v13-0", headers=H, params={"source": "events:acme"}).json()
    assert detail["errors"][0]["origin_prompt"] == "extract_fields@v13"
    assert detail["steps"][0]["prompt"] == "extract_fields@v13"
    for _ in range(2):
        c.post("/v1/runs", headers=H, json={"source": "events:acme", "days": 10})
    run = c.get("/v1/runs/latest", headers=H, params={"source": "events:acme"}).json()["measures"]
    by_prompt = {r["slice"]: r["value"] for r in run["prompt_error_rate"]["slices"]["prompt"]}
    assert by_prompt == {"extract_fields@v12": 0.0, "extract_fields@v13": 0.25}
    assert "prompt" in run["call_latency_p95"]["slices"]
    hist = c.get("/v1/measures/reported_error_rate/history", headers=H, params={"source": "events:acme"}).json()
    assert isinstance(hist["changes"], list)


def test_tenants_see_only_their_prompts(tmp_path):
    c = app(tmp_path)
    traffic(c, key(c, "acme", ["ingest"]), "v1", 3)
    assert c.get("/v1/prompts", headers=key(c, "globex", ["read"]), params={"source": "events:acme"}).status_code == 403
    assert c.get("/v1/prompts", headers=key(c, "globex", ["read"]), params={"source": "events:globex"}).json()["prompts"] == []


def test_diff_between_registered_versions(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    c.post("/v1/prompts", headers=ing, json={"prompt_id": "p", "version": "v1", "template": "Line A\nLine B"})
    c.post("/v1/prompts", headers=ing, json={"prompt_id": "p", "version": "v2", "template": "Line A\nLine C",
                                             "note": "B to C"})
    d = c.get("/v1/prompts/p/diff", headers=H, params={"a": "v1", "b": "v2", "source": "events:acme"}).json()
    assert d["available"] and "-Line B" in d["diff"] and "+Line C" in d["diff"] and d["note"] == "B to C"


def test_gate_warns_about_unregistered_prompt(tmp_path):
    c = app(tmp_path)
    mg = key(c, "acme", ["manage"])
    base = [1.0] * 60
    body = {"lineage": {"prompt": "extract_fields@v99", "model": "m", "build": "b", "corpus": "c"},
            "rules": [{"measure_id": "field_accuracy", "min_n": 30}],
            "samples": {"field_accuracy": {"overall": {"baseline": base, "candidate": base}}},
            "identical_runs": {"field_accuracy": [base, base]}}
    assert "isn't in the registry" in c.post("/v1/gates/evaluate", headers=mg, json=body).json()["warnings"][0]
    c.post("/v1/prompts", headers=key(c, "acme", ["ingest"]), json={"prompt_id": "extract_fields", "version": "v99"})
    assert c.post("/v1/gates/evaluate", headers=mg, json=body).json()["warnings"] == []


def test_client_and_otel_carry_prompt_versions(tmp_path):
    c = app(tmp_path)
    ing = key(c, "acme", ["ingest"])
    a = Assay("http://unused", strict=True, transport=lambda path, p: c.post(path, json=p, headers=ing).raise_for_status())
    v = a.register_prompt("classify", template="Classify it.", note="first")
    a.document("d1", received_at=datetime.utcnow())
    with a.stage("d1", "classification", prompt_id="classify", prompt_version=v) as step:
        step.output("document_type", "invoice")
    a.call("d1", stage="classification", prompt_id="classify", prompt_version=v)
    a.flush()
    ns = lambda s: str(int((datetime.utcnow() + timedelta(seconds=s)).timestamp() * 1e9))
    kv = lambda k, val: {"key": k, "value": {"stringValue": val}}
    spans = [{"traceId": "t", "spanId": "r", "name": "doc", "startTimeUnixNano": ns(0), "endTimeUnixNano": ns(2)},
             {"traceId": "t", "spanId": "s", "parentSpanId": "r", "name": "extract", "startTimeUnixNano": ns(0),
              "endTimeUnixNano": ns(1), "attributes": [kv("assay.stage", "extract"), kv("assay.prompt_id", "otel_prompt"),
                                                       kv("assay.prompt_version", "v3")]},
             {"traceId": "t", "spanId": "l", "parentSpanId": "s", "name": "chat", "startTimeUnixNano": ns(0),
              "endTimeUnixNano": ns(1), "attributes": [kv("gen_ai.request.model", "m")]}]
    c.post("/v1/otlp/v1/traces", headers=ing, json={"resourceSpans": [{"scopeSpans": [{"spans": spans}]}]})
    got = {(p["prompt_id"], v_["version"]) for p in c.get("/v1/prompts", headers=H, params={"source": "events:acme"}).json()["prompts"]
           for v_ in p["versions"]}
    assert got == {("classify", v), ("otel_prompt", "v3")}
