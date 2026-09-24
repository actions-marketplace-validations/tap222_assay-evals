"""Release gates: advance, hold or roll back on real signal.

Implements the roadmap's release decision rule:
- the noise floor between identical runs is known before a threshold means anything;
- a change only counts when it exceeds that noise, with a confidence interval;
- the sample must be big enough for the slice being gated;
- an unmeasured slice never defaults to pass;
- high-severity slices get tighter tolerances than low-risk metadata.

Samples are per-unit outcomes (e.g. 1/0 per document for "split correctly",
or a per-document score) for baseline and candidate on the same corpus.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field
from itertools import combinations
from statistics import fmean
from typing import Dict, List, Optional, Sequence, Tuple

ADVANCE, HOLD, ROLLBACK = "advance", "hold", "rollback"
_RANK = {ADVANCE: 0, HOLD: 1, ROLLBACK: 2}


@dataclass
class GateRule:
    measure_id: str
    higher_is_better: bool = True
    tolerance: float = 0.01  # largest acceptable worsening, in the metric's units
    min_n: int = 30
    severity: str = "normal"  # "high" halves the tolerance

    @property
    def effective_tolerance(self) -> float:
        return self.tolerance / 2 if self.severity == "high" else self.tolerance


@dataclass
class SliceDecision:
    measure_id: str
    slice: str
    outcome: str
    reason: str
    n_baseline: int = 0
    n_candidate: int = 0
    worsening: Optional[float] = None
    ci: Optional[Tuple[float, float]] = None
    noise_floor: Optional[float] = None


@dataclass
class GateDecision:
    outcome: str
    slices: List[SliceDecision] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "slices": [asdict(s) for s in self.slices]}


def noise_floor(identical_runs: Sequence[Sequence[float]]) -> Optional[float]:
    """Largest mean difference seen between runs of the same config on the same corpus.

    Needs at least two runs; returns None otherwise, which the gate treats as
    "no threshold can be trusted yet".
    """
    runs = [r for r in identical_runs if r]
    if len(runs) < 2:
        return None
    return max(abs(fmean(a) - fmean(b)) for a, b in combinations(runs, 2))


def bootstrap_diff_ci(baseline: Sequence[float], candidate: Sequence[float], *,
                      paired: bool, iters: int = 2000, alpha: float = 0.05,
                      seed: int = 7) -> Tuple[float, float, float]:
    """Mean(candidate) - mean(baseline) with a percentile bootstrap CI.

    Paired resampling is used when both samples score the same units in the
    same order, which is much tighter than treating them as independent.
    """
    rng = random.Random(seed)
    point = fmean(candidate) - fmean(baseline)
    diffs = []
    if paired:
        d = [c - b for b, c in zip(baseline, candidate)]
        n = len(d)
        for _ in range(iters):
            diffs.append(fmean(d[rng.randrange(n)] for _ in range(n)))
    else:
        nb, nc = len(baseline), len(candidate)
        for _ in range(iters):
            b = fmean(baseline[rng.randrange(nb)] for _ in range(nb))
            c = fmean(candidate[rng.randrange(nc)] for _ in range(nc))
            diffs.append(c - b)
    diffs.sort()
    lo = diffs[int(alpha / 2 * iters)]
    hi = diffs[min(int((1 - alpha / 2) * iters), iters - 1)]
    return point, lo, hi


def evaluate_slice(rule: GateRule, slice_name: str, baseline: Optional[Sequence[float]],
                   candidate: Optional[Sequence[float]], floor: Optional[float],
                   paired: bool = True) -> SliceDecision:
    base = SliceDecision(rule.measure_id, slice_name, HOLD, "",
                         n_baseline=len(baseline or []), n_candidate=len(candidate or []),
                         noise_floor=floor)
    if not baseline or not candidate:
        base.reason = "Slice is unmeasured; unmeasured never passes."
        return base
    if floor is None:
        base.reason = "Noise floor unknown; run the baseline at least twice first."
        return base
    n = min(len(baseline), len(candidate))
    if n < rule.min_n:
        base.reason = f"Sample of {n} is below the minimum of {rule.min_n} for this slice."
        return base
    if paired and len(baseline) != len(candidate):
        paired = False

    point, lo, hi = bootstrap_diff_ci(baseline, candidate, paired=paired)
    # Express as worsening: positive = worse, whichever direction is good.
    sign = -1 if rule.higher_is_better else 1
    w_point = sign * point
    w_lo, w_hi = sorted((sign * lo, sign * hi))
    base.worsening, base.ci = w_point, (w_lo, w_hi)
    limit = max(rule.effective_tolerance, floor)

    if w_lo > limit:
        base.outcome, base.reason = ROLLBACK, (
            f"Worse by {w_point:.4f}; even the optimistic end of the CI ({w_lo:.4f}) "
            f"exceeds the limit {limit:.4f} (tolerance vs noise floor, whichever is larger).")
    elif w_hi > limit:
        base.outcome, base.reason = HOLD, (
            f"Could be worse by up to {w_hi:.4f}, above the limit {limit:.4f}. "
            "Collect more samples before deciding.")
    else:
        base.outcome, base.reason = ADVANCE, (
            f"Worst plausible change {w_hi:.4f} is within the limit {limit:.4f}.")
    return base


def evaluate(rules: Sequence[GateRule],
             samples: Dict[str, Dict[str, Dict[str, Sequence[float]]]],
             noise_floors: Dict[str, Optional[float]],
             required_slices: Optional[Dict[str, Sequence[str]]] = None) -> GateDecision:
    """Evaluate every rule on every slice; the worst slice decides.

    samples[measure_id][slice] = {"baseline": [...], "candidate": [...]}
    required_slices[measure_id] lists slices that must be present; a missing
    one is reported as unmeasured and holds the release.
    """
    decisions: List[SliceDecision] = []
    for rule in rules:
        by_slice = dict(samples.get(rule.measure_id, {}))
        for s in (required_slices or {}).get(rule.measure_id, []):
            by_slice.setdefault(s, {})
        if not by_slice:
            by_slice = {"overall": {}}
        for slice_name, pair in sorted(by_slice.items()):
            decisions.append(evaluate_slice(rule, slice_name, pair.get("baseline"),
                                            pair.get("candidate"), noise_floors.get(rule.measure_id)))
    outcome = max((d.outcome for d in decisions), key=_RANK.__getitem__, default=HOLD)
    return GateDecision(outcome, decisions)
