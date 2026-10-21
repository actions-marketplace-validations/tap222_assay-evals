from datetime import datetime, timedelta
import json
import pytest
from sqlalchemy import create_engine

from assay import store
from assay.models import Window
from assay.sources.events import EventsSource
from assay.measures.ground_truth import EvalRunAccuracy, FieldAccuracy

T0 = datetime(2026, 9, 1, 12, 0, 0)
WIN = Window(T0 - timedelta(days=1), T0 + timedelta(days=1))


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    store.upgrade(eng)
    return eng


def test_eval_scores_empty(engine):
    src = EventsSource(engine, "tenant_a")
    assert src.eval_scores(WIN) is None


def test_eval_scores_extraction_and_attribution(engine, monkeypatch):
    # Insert test eval results and doc
    now = T0
    with engine.begin() as conn:
        conn.execute(store.event_documents.insert().values(
            tenant="tenant_a", document_id="doc1", document_type="invoice", segment="emea",
            received_at=now,
        ))
        conn.execute(store.eval_results.insert().values([
            dict(tenant="tenant_a", result_id="r1", run_id="run_1", case_id="doc1", document_id="doc1",
                 field="total", evaluator="exact", status="pass", raw_output=json.dumps({"weight": 1.0}), ts=now),
            dict(tenant="tenant_a", result_id="r2", run_id="run_1", case_id="doc1", document_id="doc1",
                 field="vendor", evaluator="llm_judge", status="fail", raw_output=json.dumps({"weight": 2.0}), ts=now),
            dict(tenant="tenant_a", result_id="r3", run_id="run_1", case_id="doc2", document_id="doc2",
                 field="date", evaluator="exact", status="fail", raw_output="{}", ts=now),
        ]))

    # Mock failures.evaluation to simulate root cause grouping
    def fake_evaluation(eng, src, tenant, run_id):
        return {
            "groups": [
                {
                    "name": "LLM timeout",
                    "kind": "infrastructure",
                    "member_ids": [0],
                },
                {
                    "name": "Bad extraction logic",
                    "kind": "ai",
                    "member_ids": [1],
                }
            ],
            "failures": [
                {"case_id": "doc1", "field": "vendor", "evaluator": "llm_judge"},
                {"case_id": "doc2", "field": "date", "evaluator": "exact"},
            ]
        }

    monkeypatch.setattr("assay.failures.evaluation", fake_evaluation)

    src = EventsSource(engine, "tenant_a")
    scores = src.eval_scores(WIN)
    assert scores is not None
    assert len(scores) == 3

    pass_score = next(s for s in scores if s["field"] == "total")
    assert pass_score["right"] is True
    assert pass_score["weight"] == 1.0
    assert pass_score["document_type"] == "invoice"
    assert pass_score["segment"] == "emea"

    vendor_score = next(s for s in scores if s["field"] == "vendor")
    assert vendor_score["right"] is False
    assert vendor_score["weight"] == 2.0
    assert vendor_score["cause"] == "LLM timeout"
    assert vendor_score["role"] == "infrastructure"

    date_score = next(s for s in scores if s["field"] == "date")
    assert date_score["right"] is False
    assert date_score["cause"] == "Bad extraction logic"
    assert date_score["role"] == "ai"


def test_eval_run_accuracy_measure():
    measure = EvalRunAccuracy()

    class EmptySource:
        def eval_scores(self, window):
            return []

    res = measure.compute(EmptySource(), WIN)
    assert res.status == "unmeasured"

    class SampleSource:
        def eval_scores(self, window):
            return [
                {"document_id": "d1", "field": "total", "evaluator": "exact", "right": True, "share": 1.0, "weight": 1.0, "cause": None, "role": None},
                {"document_id": "d2", "field": "total", "evaluator": "exact", "right": True, "share": 1.0, "weight": 1.0, "cause": None, "role": None},
                {"document_id": "d3", "field": "total", "evaluator": "exact", "right": False, "share": 0.0, "weight": 1.0, "cause": "Tax format", "role": "ai"},
                {"document_id": "d4", "field": "vendor", "evaluator": "llm", "right": False, "share": 0.0, "weight": 1.0, "cause": "Network reset", "role": "infrastructure"},
                {"document_id": "d5", "field": "vendor", "evaluator": "llm", "right": False, "share": 0.0, "weight": 1.0, "cause": "Judge syntax bug", "role": "evaluator"},
            ]

    res = measure.compute(SampleSource(), WIN)
    assert res.status == "measured"
    assert res.overall.n == 5
    # Overall passed = 2 / 5 = 0.4
    assert res.overall.value == 0.4
    # Excluded infra (1) + eval (1) -> ai_denom = 5 - 2 = 3. AI accuracy = 2/3 = 66.7%
    assert "AI accuracy 66.7%" in res.overall.note
    assert "1 infra, 1 eval excluded" in res.overall.note
    assert "Tax format" in res.overall.note

    # Check slices
    exact_slice = next(s for s in res.results if s.dimension == "evaluator" and s.slice_value == "exact")
    assert exact_slice.n == 3
    assert exact_slice.value == pytest.approx(2 / 3)

    cause_slice = next(s for s in res.results if s.dimension == "cause" and s.slice_value == "Tax format")
    assert cause_slice.n == 1
    assert cause_slice.value == 0.0


def test_field_accuracy_fallback():
    measure = FieldAccuracy()

    class FallbackSource:
        def field_scores(self, window):
            return None

        def eval_scores(self, window):
            return [
                {"document_id": "d1", "document_type": "doc", "segment": "s1", "field": "total", "right": True, "share": 1.0, "weight": 1.0},
                {"document_id": "d2", "document_type": "doc", "segment": "s1", "field": "total", "right": False, "share": 0.0, "weight": 1.0},
            ]

    res = measure.compute(FallbackSource(), WIN)
    assert res.status == "measured"
    assert res.overall.n == 2
    assert res.overall.value == 0.5
