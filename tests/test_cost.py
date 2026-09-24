from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from assay.api import create_app
from assay.config import Settings
from assay.cost import breakdown, build_ledger, is_fallback, spend_by_model
from assay.measures import REGISTRY
from assay.models import CallRecord, DocumentRecord, ReviewRecord, Window

T0 = datetime(2026, 9, 1)
WIN = Window(T0 - timedelta(days=1), T0 + timedelta(days=1))


class Src:
    name = "fake"

    def __init__(self, docs, calls=(), reviews=None, rates=None):
        self._d, self._c, self._r = docs, list(calls), reviews
        self.cost_rates = rates or {}

    def documents(self, w): return self._d
    def calls(self, w): return self._c
    def reviews(self, w): return self._r


def doc(i, **kw):
    return DocumentRecord(str(i), T0, T0, **kw)


def call(i, doc_id, cost=None, stage="extract", model="m", declared="m", layer=None):
    return CallRecord(str(i), stage, T0, document_id=str(doc_id), cost_usd=cost, model_served=model,
                      model_declared=declared, resolving_layer=layer)


def by_component(ledger):
    out = {}
    for ln in ledger.lines:
        out[ln.component] = out.get(ln.component, 0) + ln.usd
    return out


def test_fallback_detection():
    assert is_fallback(call(1, 1, layer="fallback_1"))
    assert is_fallback(call(1, 1, model="big", declared="small"))
    assert not is_fallback(call(1, 1, layer="primary"))
    assert not is_fallback(call(1, 1))


def test_ledger_prices_every_component():
    docs = [doc(1, page_count=10), doc(2, page_count=2)]
    calls = [call(1, 1, 0.10), call(2, 1, 0.50, model="big", declared="m"), call(3, 2, 0.20)]
    reviews = [ReviewRecord("r1", "1", T0, "review", minutes=30), ReviewRecord("r2", "1", T0, "rework", cost_usd=5.0)]
    rates = {"review_per_hour": 40, "platform_per_document": 0.01, "platform_per_page": 0.001}
    comp = by_component(build_ledger(Src(docs, calls, reviews), WIN, rates))
    assert comp["ai_inference"] == pytest.approx(0.30)
    assert comp["fallback"] == pytest.approx(0.50)
    assert comp["review"] == pytest.approx(20.0)  # 30 min at $40/h
    assert comp["rework"] == pytest.approx(5.0)   # as recorded
    assert comp["platform"] == pytest.approx(0.02 + 0.012)


def test_unpriced_calls_are_estimated_separately_or_left_as_a_floor():
    docs = [doc(1)]
    calls = [call(1, 1, 0.10), call(2, 1, 0.30), call(3, 1, None),          # same stage+model: median 0.20
             call(4, 1, None, stage="other"),                                 # same model other stage: 0.20
             call(5, 1, None, model="never_priced", declared="never_priced")]  # nothing to go on
    ledger = build_ledger(Src(docs, calls), WIN, {})
    comp = by_component(ledger)
    assert comp["ai_inference"] == pytest.approx(0.40)
    assert comp["ai_estimated"] == pytest.approx(0.40)
    assert ledger.coverage["estimated_calls"] == 2 and ledger.coverage["unpriced_calls"] == 1
    assert any("floor" in n for n in ledger.notes())


def test_missing_review_data_is_called_out_not_zeroed():
    ledger = build_ledger(Src([doc(1)], [call(1, 1, 0.1)], reviews=None), WIN, {})
    assert any("people cost is missing" in n for n in ledger.notes())
    minutes_no_rate = build_ledger(Src([doc(1)], [], [ReviewRecord("r", "1", T0, minutes=10)]), WIN, {})
    assert minutes_no_rate.coverage["unpriced_review_minutes"] == 10
    assert "review" not in by_component(minutes_no_rate)


def test_breakdown_by_category_and_model():
    docs = [doc(1, document_type="contract"), doc(2, document_type="invoice"), doc(3, document_type="invoice")]
    calls = [call(1, 1, 3.0), call(2, 2, 0.2), call(3, 3, 0.4, model="big", layer="fallback_1")]
    ledger = build_ledger(Src(docs, calls), WIN, {})
    rows = {r["value"]: r for r in breakdown(ledger, "document_type")}
    assert rows["contract"]["total_per_document"] == pytest.approx(3.0)
    assert rows["invoice"]["total_per_document"] == pytest.approx(0.3)
    assert rows["invoice"]["per_document"]["fallback"] == pytest.approx(0.2)
    models = {m["model"]: m for m in spend_by_model(ledger)}
    assert models["big"]["fallback_usd"] == pytest.approx(0.4)


def test_cost_per_document_measure_and_standard_error():
    docs = [doc(i, document_type="a" if i < 50 else "b") for i in range(100)]
    calls = [call(i, i, 1.0 if i < 50 else 3.0) for i in range(100)]
    out = REGISTRY["cost_per_document"].compute(Src(docs, calls), WIN)
    assert out.overall.value == pytest.approx(2.0) and out.overall.stderr > 0
    b = next(r for r in out.results if r.dimension == "document_type" and r.slice_value == "b")
    assert b.value == pytest.approx(3.0) and b.stderr == pytest.approx(0.0)
    comp = {r.slice_value: r.value for r in out.results if r.dimension == "component"}
    assert comp["AI inference"] == pytest.approx(2.0) and comp["Human review"] == 0.0


def test_cost_per_page_and_touch_rate():
    docs = [doc(1, page_count=4), doc(2, page_count=6), doc(3)]
    src = Src(docs, [call(1, 1, 1.0), call(2, 2, 1.0)], [ReviewRecord("r", "1", T0, minutes=1)])
    page = REGISTRY["cost_per_page"].compute(src, WIN)
    assert page.overall.value == pytest.approx(0.2)  # $2 over 10 pages; doc 3 has no page count
    touch = REGISTRY["human_touch_rate"].compute(src, WIN)
    assert touch.overall.value == pytest.approx(1 / 3)
    assert REGISTRY["human_touch_rate"].compute(Src(docs, reviews=None), WIN).status == "unmeasured"
    assert REGISTRY["cost_per_page"].compute(Src([doc(1)]), WIN).status == "unmeasured"


# ---------- API ----------

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'cost.db'}")))


def test_rates_reviews_and_breakdown_end_to_end(client):
    now = datetime.utcnow()
    h = {"X-Tenant": "t"}
    client.post("/v1/events/documents", headers=h, json=[
        {"document_id": f"d{i}", "received_at": (now - timedelta(hours=2)).isoformat(), "page_count": 5,
         "document_type": "contract" if i < 5 else "invoice"} for i in range(10)])
    client.post("/v1/events/calls", headers=h, json=[
        {"call_id": f"c{i}", "stage": "extract", "ts": (now - timedelta(hours=1)).isoformat(),
         "document_id": f"d{i}", "cost_usd": 0.1} for i in range(10)])
    assert client.post("/v1/events/reviews", headers=h, json=[
        {"review_id": "r1", "document_id": "d0", "ts": now.isoformat(), "minutes": 60}]).json() == {"ingested": 1}
    assert client.post("/v1/events/reviews", headers=h, json=[
        {"review_id": "x", "document_id": "d0", "ts": now.isoformat(), "kind": "coffee"}]).status_code == 422

    assert client.put("/v1/cost/rates", json={"source": "events:t", "rates": {"bogus": 1}}).status_code == 422
    assert client.put("/v1/cost/rates", json={"source": "events:t", "rates": {"review_per_hour": -1}}).status_code == 422
    r = client.put("/v1/cost/rates", json={"source": "events:t", "rates": {"review_per_hour": 30}}).json()
    assert r["rates"] == {"review_per_hour": 30}

    bd = client.get("/v1/cost/breakdown", params={"source": "events:t", "by": "document_type", "days": 1}).json()
    rows = {x["value"]: x for x in bd["rows"]}
    assert rows["contract"]["total_per_document"] == pytest.approx(0.1 + 30 / 5)
    assert rows["invoice"]["total_per_document"] == pytest.approx(0.1)

    run = client.post("/v1/runs", json={"source": "events:t", "days": 1}).json()
    assert run["measures"]["cost_per_document"]["overall"]["value"] == pytest.approx((1.0 + 30) / 10)
    assert run["measures"]["human_touch_rate"]["overall"]["value"] == pytest.approx(0.1)

    # null removes a rate
    client.put("/v1/cost/rates", json={"source": "events:t", "rates": {"review_per_hour": None}})
    assert client.get("/v1/cost/rates", params={"source": "events:t"}).json()["rates"] == {}
