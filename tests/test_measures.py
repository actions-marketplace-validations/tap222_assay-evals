from datetime import datetime, timedelta

from assay.measures import REGISTRY
from assay.models import UNRECORDED, CallRecord, DocumentRecord, IndexedRecord, StageRun, Window

T0 = datetime(2026, 9, 1)
WIN = Window(T0 - timedelta(days=30), T0 + timedelta(days=30))


class FakeSource:
    name = "fake"

    def __init__(self, calls=None, documents=None, stage_runs=None, indexed=None, downstream=None):
        self._calls, self._docs, self._runs, self._idx, self._down = calls, documents, stage_runs, indexed, downstream

    def calls(self, w): return self._calls
    def documents(self, w): return self._docs
    def stage_runs(self, w): return self._runs
    def indexed(self, w): return self._idx
    def downstream_hashes(self): return self._down


def call(i, stage="indexing", **kw):
    return CallRecord(call_id=str(i), stage=stage, ts=T0, **kw)


def overall(out):
    return out.overall.value


def slice_of(out, dim, val):
    return next(r for r in out.results if r.dimension == dim and r.slice_value == val)


def test_fallback_attribution_needs_both_fields_and_slices_by_stage():
    src = FakeSource(calls=[
        call(1, "record_splitting", resolving_layer="primary", gate_reason="ok"),
        call(2, "record_splitting", resolving_layer="primary"),  # no reason: not attributed
        call(3, "indexing"), call(4, "indexing"),
    ])
    out = REGISTRY["fallback_attribution"].compute(src, WIN)
    assert out.status == "measured"
    assert overall(out) == 0.25
    assert slice_of(out, "stage", "record_splitting").value == 0.5
    assert slice_of(out, "stage", "indexing").value == 0.0


def test_missing_dimension_is_a_visible_slice_not_dropped():
    src = FakeSource(indexed=[IndexedRecord("a", True, county="Cook IL"), IndexedRecord("b", False)])
    out = REGISTRY["source_positions"].compute(src, WIN)
    unrec = slice_of(out, "county", UNRECORDED)
    assert unrec.n == 1 and unrec.value == 0.0


def test_cost_coverage_reports_spend_as_a_floor():
    src = FakeSource(calls=[call(1, cost_usd=0.5), call(2, cost_usd=None), call(3, cost_usd=1.0)])
    out = REGISTRY["cost_coverage"].compute(src, WIN)
    assert round(overall(out), 4) == round(2 / 3, 4)
    assert "$1.50" in out.overall.note


def test_model_mismatch_only_counts_calls_that_declare_both():
    src = FakeSource(calls=[call(1, model_declared="a", model_served="b"),
                            call(2, model_declared="a", model_served="a"),
                            call(3, model_declared="a")])
    out = REGISTRY["model_mismatch"].compute(src, WIN)
    assert overall(out) == 0.5 and out.overall.n == 2


def test_noop_stages_counts_success_without_work():
    runs = [StageRun("d", "indexing", "success", did_work=True),
            StageRun("d", "highlighting", "success", did_work=False),
            StageRun("d", "redaction", "failed", did_work=False),  # not a success: excluded
            StageRun("d", "x", "success", did_work=None)]          # unknown: excluded
    out = REGISTRY["noop_stage_rate"].compute(FakeSource(stage_runs=runs), WIN)
    assert overall(out) == 0.5 and out.overall.n == 2


def test_handoff_is_unmeasured_without_downstream_not_zero():
    docs = [DocumentRecord("a", T0, T0, file_hash="h1")]
    out = REGISTRY["handoff_loss"].compute(FakeSource(documents=docs, downstream=None), WIN)
    assert out.status == "unmeasured" and "downstream" in out.reason


def test_handoff_joins_on_file_hash():
    docs = [DocumentRecord("a", T0, T0, file_hash="h1"), DocumentRecord("b", T0, T0, file_hash="h2"),
            DocumentRecord("c", T0, None, file_hash="h3")]  # unfinished: excluded
    out = REGISTRY["handoff_loss"].compute(FakeSource(documents=docs, downstream={"h1"}), WIN)
    assert overall(out) == 0.5 and out.overall.n == 2


def test_time_to_complete_reports_p90_and_completion_share():
    docs = [DocumentRecord(str(i), T0, T0 + timedelta(minutes=10 * (i + 1)), processing_mode="realtime")
            for i in range(10)]
    docs.append(DocumentRecord("never", T0, None, processing_mode="batch"))
    out = REGISTRY["time_to_complete_p90"].compute(FakeSource(documents=docs), WIN)
    assert round(overall(out)) == 91 * 60  # p90 of 10..100 minutes
    batch = slice_of(out, "processing_mode", "batch")
    assert batch.value is None and "No document" in batch.note


def test_source_without_data_is_unmeasured():
    for mid in ("fallback_attribution", "cost_coverage", "noop_stage_rate", "source_positions"):
        assert REGISTRY[mid].compute(FakeSource(), WIN).status == "unmeasured"


def test_ground_truth_measures_say_what_they_wait_for():
    out = REGISTRY["split_stp"].compute(FakeSource(), WIN)
    assert out.status == "unmeasured" and "DEV-NEW-2" in out.reason


# ---------- operational measures ----------

def test_volume_counts_slices_and_zero_is_measured():
    docs = [DocumentRecord("a", T0, county="Cook IL"), DocumentRecord("b", T0, county="Cook IL"),
            DocumentRecord("c", T0)]
    out = REGISTRY["document_volume"].compute(FakeSource(documents=docs), WIN)
    assert overall(out) == 3 and slice_of(out, "county", "Cook IL").value == 2
    empty = REGISTRY["document_volume"].compute(FakeSource(documents=[]), WIN)
    assert empty.status == "measured" and overall(empty) == 0


def test_call_error_rate_ignores_calls_without_status():
    calls = [call(1, status="success"), call(2, status="timeout"), call(3)]
    out = REGISTRY["call_error_rate"].compute(FakeSource(calls=calls), WIN)
    assert overall(out) == 0.5 and out.overall.n == 2


def test_latency_p95_by_model():
    calls = [call(i, model_served="fast", latency_ms=100.0) for i in range(20)]
    calls += [call(100 + i, model_served="slow", latency_ms=5000.0) for i in range(20)]
    out = REGISTRY["call_latency_p95"].compute(FakeSource(calls=calls), WIN)
    assert slice_of(out, "model_served", "slow").value == 5000.0
    assert slice_of(out, "model_served", "fast").value == 100.0


class TwoWindowSource(FakeSource):
    def __init__(self, cur, prev):
        super().__init__()
        self.cur, self.prev = cur, prev

    def documents(self, w):
        return self.cur if w == WIN else self.prev


def test_drift_is_zero_for_same_mix_and_flags_new_county():
    mix = [DocumentRecord(str(i), T0, county=c) for i, c in enumerate(["A", "B"] * 50)]
    same = REGISTRY["input_mix_drift"].compute(TwoWindowSource(mix, mix), WIN)
    assert overall(same) < 1e-9

    shifted = mix + [DocumentRecord(f"n{i}", T0, county="New") for i in range(60)]
    out = REGISTRY["input_mix_drift"].compute(TwoWindowSource(shifted, mix), WIN)
    assert overall(out) > 0.2
    new = slice_of(out, "county", "New")
    assert new.value == max(r.value for r in out.results if r.dimension == "county")


def test_drift_needs_both_windows():
    mix = [DocumentRecord("a", T0, county="A")]
    assert REGISTRY["input_mix_drift"].compute(TwoWindowSource(mix, []), WIN).status == "unmeasured"


# ---------- tracing ----------

def test_trace_flags_what_went_wrong():
    from assay.trace import build_trace

    class S(FakeSource):
        def document_detail(self, doc_id):
            doc = DocumentRecord(doc_id, T0, T0 + timedelta(hours=2), file_hash="h1")
            runs = [StageRun(doc_id, "indexing", "failed", T0, T0, True),
                    StageRun(doc_id, "highlighting", "success", T0, T0, False)]
            calls = [call(1, model_declared="a", model_served="b", cost_usd=0.1, latency_ms=10.0,
                          status="success", document_id=doc_id)]
            return doc, runs, calls

    out = build_trace(S(downstream=set()), "d1")
    texts = " | ".join(f["text"] for f in out["flags"])
    assert "no record downstream" in texts
    assert "indexing failed" in texts
    assert "highlighting reported success without doing any work" in texts
    assert "declared a but was served by b" in texts
    assert out["document"]["duration_s"] == 7200 and out["cost_usd"] == 0.1
