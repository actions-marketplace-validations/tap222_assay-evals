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
    d = gates.evaluate_slice(GateRule("acc", min_n=30), "Cook IL", [1.0] * 10, [1.0] * 10, floor=0.0)
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
    samples = {"acc": {"Harris TX": {"baseline": base, "candidate": list(base)}}}
    d = gates.evaluate([GateRule("acc")], samples, {"acc": 0.01}, required_slices={"acc": ["Harris TX", "Cook IL"]})
    assert d.outcome == HOLD
    cook = next(s for s in d.slices if s.slice == "Cook IL")
    assert "unmeasured" in cook.reason


def test_worst_slice_decides():
    good = outcomes(0.9, 300, 7)
    samples = {"acc": {"A": {"baseline": good, "candidate": list(good)},
                       "B": {"baseline": outcomes(0.9, 300, 8), "candidate": outcomes(0.5, 300, 9)}}}
    assert gates.evaluate([GateRule("acc")], samples, {"acc": 0.01}).outcome == ROLLBACK
