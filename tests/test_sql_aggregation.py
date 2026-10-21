from datetime import datetime, timedelta
import pytest
from sqlalchemy import create_engine

from assay import store
from assay.models import Window
from assay.sources.events import EventsSource
from assay.measures.operations import DocumentVolume, StageFailureRate

T0 = datetime(2026, 9, 1, 12, 0, 0)
WIN = Window(T0 - timedelta(days=1), T0 + timedelta(days=1))


@pytest.fixture
def engine(tmp_path):
    eng = create_engine(f"sqlite:///{tmp_path / 'sql_agg.db'}")
    store.upgrade(eng)
    return eng


def test_sql_aggregation_matches_python_results(engine):
    now = T0
    with engine.begin() as conn:
        conn.execute(store.event_documents.insert().values([
            dict(tenant="t1", document_id="d1", received_at=now, segment="finance", document_type="invoice", processing_mode="batch"),
            dict(tenant="t1", document_id="d2", received_at=now, segment="finance", document_type="invoice", processing_mode="batch"),
            dict(tenant="t1", document_id="d3", received_at=now, segment="hr", document_type="contract", processing_mode="realtime"),
            dict(tenant="t1", document_id="d4", received_at=now, segment=None, document_type="invoice", processing_mode="batch"),
        ]))
        conn.execute(store.event_stage_runs.insert().values([
            dict(tenant="t1", run_id="s1", document_id="d1", stage="ocr", status="success", started_at=now),
            dict(tenant="t1", run_id="s2", document_id="d2", stage="ocr", status="failed", started_at=now),
            dict(tenant="t1", run_id="s3", document_id="d3", stage="classify", status="success", started_at=now),
            dict(tenant="t1", run_id="s4", document_id="d4", stage="ocr", status="error", started_at=now),
        ]))

    src = EventsSource(engine, "t1")

    # DocumentVolume SQL aggregation
    m_vol = DocumentVolume()
    res_sql = m_vol.compute(src, WIN)
    assert res_sql.status == "measured"
    assert res_sql.overall.n == 4
    assert res_sql.overall.value == 4.0

    # Verify slices
    finance_slice = next(s for s in res_sql.results if s.dimension == "segment" and s.slice_value == "finance")
    assert finance_slice.n == 2
    unrec_slice = next(s for s in res_sql.results if s.dimension == "segment" and s.slice_value == "(unrecorded)")
    assert unrec_slice.n == 1

    # StageFailureRate SQL aggregation
    m_rate = StageFailureRate()
    res_rate = m_rate.compute(src, WIN)
    assert res_rate.status == "measured"
    assert res_rate.overall.n == 4
    # 2 failed (s2 and s4) out of 4 = 0.5
    assert res_rate.overall.value == 0.5
    assert res_rate.overall.numerator == 2
    assert res_rate.overall.denominator == 4

    # Slice by stage
    ocr_slice = next(s for s in res_rate.results if s.dimension == "stage" and s.slice_value == "ocr")
    assert ocr_slice.n == 3
    assert ocr_slice.numerator == 2
    assert ocr_slice.value == pytest.approx(2 / 3)
