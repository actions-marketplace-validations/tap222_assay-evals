import math
from datetime import datetime, timedelta
from assay.alerts import learn_band, outside, sampling_error
from assay.measures.base import SliceResult, bootstrap_quantile_stderr
from assay.measures.pipeline import TimeToComplete
from assay.models import DocumentRecord, Window


def test_bootstrap_quantile_stderr_computation():
    # 20 timing samples with jitter
    times = [10.0, 11.2, 9.8, 10.5, 12.0, 14.5, 9.2, 10.1, 11.0, 15.2,
             10.8, 11.4, 9.9, 13.1, 10.2, 11.9, 10.4, 16.0, 10.3, 11.1]
    se = bootstrap_quantile_stderr(times, q=0.9, iters=400, seed=123)
    assert se is not None
    assert 0.4 < se < 2.5  # reasonable bootstrap standard error range

    # Fewer than 2 samples cannot compute bootstrap stderr
    assert bootstrap_quantile_stderr([10.0], q=0.9) is None
    assert bootstrap_quantile_stderr([], q=0.9) is None


def test_time_to_complete_attaches_bootstrap_stderr():
    now = datetime.utcnow()
    # Mock documents with completion timestamps
    docs = [
        DocumentRecord(document_id=f"doc-{i}", received_at=now - timedelta(seconds=100 + i * 5),
                       completed_at=now, processing_mode="batch", segment="corp")
        for i in range(15)
    ]

    class MockSource:
        def documents(self, window):
            return docs

    m = TimeToComplete()
    out = m.compute(MockSource(), Window(now - timedelta(days=1), now))
    assert out.status == "measured"
    assert out.overall is not None
    assert out.overall.stderr is not None
    assert out.overall.stderr > 0.0


def test_timing_noise_widens_alert_band_and_prevents_false_alarm():
    # History of p90 completion times: ~12.0 seconds
    history = [12.0, 12.2, 11.8, 12.1, 12.3]
    # Without stderr on a small slice (n=10):
    band_no_stderr = learn_band(history, unit="seconds", n=10, stderr=0.0)

    # With bootstrap stderr calculated on a small jittery slice (stderr = 2.0s):
    band_with_stderr = learn_band(history, unit="seconds", n=10, stderr=2.0)

    assert band_with_stderr.high > band_no_stderr.high

    # A candidate observation at 16.0s on small noisy slice:
    # Triggers alert without bootstrap error estimate, but stays inside band when timing noise is accounted for!
    candidate_val = 16.0
    assert outside(candidate_val, band_no_stderr, higher_is_better=False) is True
    assert outside(candidate_val, band_with_stderr, higher_is_better=False) is False
