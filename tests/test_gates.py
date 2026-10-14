import random

from assay import gates
from assay.gates import ADVANCE, HOLD, ROLLBACK, GateRule


def outcomes(rate, n, seed):
    rng = random.Random(seed)
    return [1.0 if rng.random() < rate else 0.0 for _ in range(n)]


def test_noise_floor_needs_two_runs():
    assert gates.noise_floor([[1, 0, 1]]) is None
    assert gates.noise_floor([[1, 1, 0, 0], [1, 0, 0, 0]]) == 0.25


def test_clear_regression_rolls_back():
    base = outcomes(0.95, 400, 1)
    cand = outcomes(0.70, 400, 2)
    d = gates.evaluate_slice(GateRule("acc", tolerance=0.02), "overall", base, cand, floor=0.01, paired=False)
    assert d.outcome == ROLLBACK


def test_no_change_advances():
    base = outcomes(0.9, 500, 3)
    d = gates.evaluate_slice(GateRule("acc", tolerance=0.05), "overall", base, list(base), floor=0.01)
    assert d.outcome == ADVANCE


def test_small_sample_holds_even_if_it_looks_fine():
    d = gates.evaluate_slice(GateRule("acc", min_n=30), "acme", [1.0] * 10, [1.0] * 10, floor=0.0)
    assert d.outcome == HOLD and "below the minimum" in d.reason


def test_unknown_noise_floor_holds():
    d = gates.evaluate_slice(GateRule("acc"), "overall", [1.0] * 50, [1.0] * 50, floor=None)
    assert d.outcome == HOLD and "Noise floor" in d.reason


def test_lower_is_better_direction():
    base = outcomes(0.05, 400, 4)   # e.g. escape rate
    cand = outcomes(0.30, 400, 5)
    rule = GateRule("escape", higher_is_better=False, tolerance=0.02)
    assert gates.evaluate_slice(rule, "overall", base, cand, floor=0.0, paired=False).outcome == ROLLBACK


def test_high_severity_tightens_tolerance():
    assert GateRule("x", tolerance=0.04, severity="high").effective_tolerance == 0.02


def test_missing_required_slice_holds_whole_release():
    base = outcomes(0.9, 200, 6)
    samples = {"acc": {"globex": {"baseline": base, "candidate": list(base)}}}
    d = gates.evaluate([GateRule("acc")], samples, {"acc": 0.01}, required_slices={"acc": ["globex", "acme"]})
    assert d.outcome == HOLD
    missing = next(s for s in d.slices if s.slice == "acme")
    assert "unmeasured" in missing.reason


def test_worst_slice_decides():
    good = outcomes(0.9, 300, 7)
    samples = {"acc": {"A": {"baseline": good, "candidate": list(good)},
                       "B": {"baseline": outcomes(0.9, 300, 8), "candidate": outcomes(0.5, 300, 9)}}}
    assert gates.evaluate([GateRule("acc")], samples, {"acc": 0.01}).outcome == ROLLBACK


def test_noise_floor_is_the_typical_spread_not_the_largest_gap():
    runs = [[1] * 90 + [0] * 10, [1] * 91 + [0] * 9, [1] * 89 + [0] * 11, [1] * 90 + [0] * 10]
    floor = gates.noise_floor(runs)
    assert 0.01 < floor < 0.03
    # Ten more runs like these shouldn't make the floor looser, as the largest gap would.
    assert gates.noise_floor(runs * 3) <= floor


def latencies(n, seed, slow_share=0.0, slow_ms=4000.0):
    rng = random.Random(seed)
    return [slow_ms if rng.random() < slow_share else rng.gauss(1000, 100) for _ in range(n)]


def test_percentile_interpolates():
    assert gates.percentile([1, 2, 3, 4], 50) == 2.5
    assert gates.percentile([10.0], 95) == 10.0
    assert gates.percentile(list(range(101)), 95) == 95


def test_statistic_must_be_mean_or_a_percentile():
    for bad in ("median", "p0", "p100", "p", "pnan", "95"):
        try:
            GateRule("latency_ms", statistic=bad)
        except ValueError:
            continue
        raise AssertionError(f"{bad!r} was accepted")
    assert GateRule("latency_ms", statistic="p99.9").key == "latency_ms:p99.9"


def test_p95_catches_a_slow_tail_the_mean_misses():
    # The candidate is a little faster on most calls, but 8% of calls now take 4 s.
    base = latencies(1000, 11)
    cand = [x - 300 for x in base]
    rng = random.Random(12)
    cand = [4000.0 if rng.random() < 0.08 else x for x in cand]
    mean_rule = GateRule("latency_ms", higher_is_better=False, tolerance=100, min_n=100)
    p95_rule = GateRule("latency_ms", higher_is_better=False, tolerance=100, min_n=100, statistic="p95")
    assert gates.evaluate_slice(mean_rule, "overall", base, cand, floor=20.0).outcome == ADVANCE
    d = gates.evaluate_slice(p95_rule, "overall", base, cand, floor=20.0)
    assert d.outcome == ROLLBACK and d.statistic == "p95" and d.worsening > 2000


def test_unchanged_p95_advances():
    base = latencies(600, 13)
    rule = GateRule("latency_ms", higher_is_better=False, tolerance=100, min_n=100, statistic="p95")
    assert gates.evaluate_slice(rule, "overall", base, list(base), floor=20.0).outcome == ADVANCE


def test_percentile_holds_when_too_few_values_lie_beyond_it():
    base = latencies(60, 14)
    rule = GateRule("latency_ms", higher_is_better=False, tolerance=100, min_n=30, statistic="p95")
    d = gates.evaluate_slice(rule, "overall", base, list(base), floor=20.0)
    assert d.outcome == HOLD and "at least 100" in d.reason


def test_noise_floor_uses_the_rules_statistic():
    runs = [latencies(500, s, slow_share=0.05) for s in (21, 22, 23)]
    assert gates.noise_floor(runs, "p95") != gates.noise_floor(runs)


def test_mean_and_p95_rules_on_one_measure_keep_their_own_floors():
    base = latencies(400, 31)
    samples = {"latency_ms": {"overall": {"baseline": base, "candidate": list(base)}}}
    rules = [GateRule("latency_ms", higher_is_better=False, tolerance=50, min_n=100),
             GateRule("latency_ms", higher_is_better=False, tolerance=50, min_n=100, statistic="p95")]
    d = gates.evaluate(rules, samples, {"latency_ms": 5.0, "latency_ms:p95": 30.0})
    assert [s.noise_floor for s in d.slices] == [5.0, 30.0]
    assert d.outcome == ADVANCE
