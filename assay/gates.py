"""Release gates: advance, hold or roll back on real signal.

The decision rule:
- the noise floor between identical runs is known before a threshold means anything;
- a change only counts when it exceeds that noise, with a confidence interval;
- the sample must be big enough for the slice being gated;
- an unmeasured slice never defaults to pass;
- high-severity slices get tighter tolerances than low-risk metadata.

Samples are per-unit outcomes (e.g. 1/0 per document for "split correctly",
or a per-document score) for baseline and candidate on the same corpus.
A rule compares their mean by default, or a percentile ("p95") for measures
like latency, where a slow tail can grow while the average holds. A rule can
also set an absolute limit ("p95 under 20 s") the candidate must keep whatever
the baseline did, and give a high-risk slice its own margin.
"""
from __future__ import annotations

import random
from dataclasses import asdict, dataclass, field, replace
from statistics import fmean, stdev
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

ADVANCE, ADVANCE_WITH_CONDITIONS, HOLD, ROLLBACK = (
    "advance", "advance_with_conditions", "hold", "rollback"
)
_RANK = {ADVANCE: 0, ADVANCE_WITH_CONDITIONS: 1, HOLD: 2, ROLLBACK: 3}
# A percentile rests on the values beyond it: p99 of 100 values is one value.
MIN_TAIL = 5
# What a slice can set for itself, in place of the measure's value.
SLICE_FIELDS = ("tolerance", "severity", "min_n", "absolute_limit", "fallback_stability")


def percentile_of(statistic: str) -> Optional[float]:
    """50.0 for "p50", None for "mean"; anything else is rejected."""
    if statistic == "mean":
        return None
    try:
        q = float(statistic[1:]) if statistic[:1] == "p" else None
    except ValueError:
        q = None
    if q is None or not 0 < q < 100:
        raise ValueError(f'statistic must be "mean" or a percentile such as "p95", not {statistic!r}')
    return q


def percentile(values: Sequence[float], q: float) -> float:
    """The q-th percentile, interpolating between the two nearest values."""
    v = sorted(values)
    pos = (len(v) - 1) * q / 100
    lo = int(pos)
    hi = min(lo + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def stat_fn(statistic: str) -> Callable[[Sequence[float]], float]:
    q = percentile_of(statistic)
    return fmean if q is None else (lambda xs: percentile(xs, q))


@dataclass
class GateRule:
    measure_id: str
    higher_is_better: bool = True
    tolerance: float = 0.01  # largest acceptable worsening, in the metric's units
    min_n: int = 30
    severity: str = "normal"  # "high" halves the tolerance
    statistic: str = "mean"  # or a percentile, "p95"
    # The worst value the candidate's statistic may have, whatever the baseline did: a maximum
    # when lower is better, a minimum when higher is better. None: no absolute limit.
    absolute_limit: Optional[float] = None
    # A slice's own margin, agreed before the runs: {"high_value": {"tolerance": 0.005}}. A slice
    # named here must be measured, or the release holds.
    slices: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    # When True (or when None and severity == "high"), slices under min_n fall back to
    # the stability gate's per-case rules: no case that passed reliably may now fail, and any collapse blocks.
    fallback_stability: Optional[bool] = None

    def __post_init__(self) -> None:
        percentile_of(self.statistic)
        for name, own in self.slices.items():
            unknown = set(own) - set(SLICE_FIELDS)
            if unknown:
                raise ValueError(f"Slice {name!r} can set {', '.join(SLICE_FIELDS)}, not {', '.join(sorted(unknown))}")

    def for_slice(self, name: str) -> "GateRule":
        """The rule as it applies to one slice: the measure's values, with that slice's own on top."""
        own = self.slices.get(name)
        return replace(self, **own, slices={}) if own else self

    @property
    def key(self) -> str:
        """Names the rule's noise floor: two rules can gate one measure on different statistics."""
        return self.measure_id if self.statistic == "mean" else f"{self.measure_id}:{self.statistic}"

    @property
    def effective_tolerance(self) -> float:
        return self.tolerance / 2 if self.severity == "high" else self.tolerance

    @property
    def is_high_risk(self) -> bool:
        if self.fallback_stability is not None:
            return self.fallback_stability
        return self.severity == "high"


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
    statistic: str = "mean"
    absolute_limit: Optional[float] = None
    tolerance: Optional[float] = None  # the margin applied, after severity
    own_margin: bool = False  # the slice set its own margin, in place of the measure's
    candidate_value: Optional[float] = None  # the candidate's statistic, when checked against the limit
    candidate_ci: Optional[Tuple[float, float]] = None


@dataclass
class GateDecision:
    outcome: str
    slices: List[SliceDecision] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"outcome": self.outcome, "slices": [asdict(s) for s in self.slices]}


def noise_floor(identical_runs: Sequence[Sequence[float]], statistic: str = "mean") -> Optional[float]:
    """How far apart two runs of the same config on the same corpus usually land.

    With two runs, their difference. With three or more, 1.96 × √2 × the standard
    deviation of the run means: the 95% range of the difference between two runs.
    (The largest pairwise difference, used before, grows with the number of runs,
    so more evidence made the floor looser.) Returns None with fewer than two runs,
    which the gate treats as "no threshold can be trusted yet". With a percentile
    statistic, the same is measured on each run's percentile.
    """
    stat = stat_fn(statistic)
    means = [stat(r) for r in identical_runs if r]
    if len(means) < 2:
        return None
    if len(means) == 2:
        return abs(means[0] - means[1])
    return 1.96 * (2 ** 0.5) * stdev(means)


def bootstrap_diff_ci(baseline: Sequence[float], candidate: Sequence[float], *,
                      paired: bool, iters: int = 2000, alpha: float = 0.05,
                      seed: int = 7, statistic: str = "mean") -> Tuple[float, float, float]:
    """stat(candidate) - stat(baseline) with a percentile bootstrap CI.

    Paired resampling is used when both samples score the same units in the
    same order, which is much tighter than treating them as independent.
    """
    rng = random.Random(seed)
    stat = stat_fn(statistic)
    point = stat(candidate) - stat(baseline)
    diffs = []
    if statistic != "mean":
        # A percentile of differences isn't the difference of percentiles, so
        # resample units (the same ones on both sides when paired) and compare.
        nb, nc = len(baseline), len(candidate)
        for _ in range(iters):
            if paired:
                idx = [rng.randrange(nb) for _ in range(nb)]
                b, c = [baseline[i] for i in idx], [candidate[i] for i in idx]
            else:
                b = [baseline[rng.randrange(nb)] for _ in range(nb)]
                c = [candidate[rng.randrange(nc)] for _ in range(nc)]
            diffs.append(stat(c) - stat(b))
    elif paired:
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


def bootstrap_ci(values: Sequence[float], *, iters: int = 2000, alpha: float = 0.05,
                 seed: int = 7, statistic: str = "mean") -> Tuple[float, float, float]:
    """stat(values) with a percentile bootstrap CI, resampling the units."""
    rng = random.Random(seed)
    stat = stat_fn(statistic)
    n = len(values)
    est = sorted(stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(iters))
    return stat(values), est[int(alpha / 2 * iters)], est[min(int((1 - alpha / 2) * iters), iters - 1)]


def _too_few(rule: GateRule, n: int) -> Optional[str]:
    """Why n values can't be gated on this rule's statistic, or None when they can."""
    if n < rule.min_n:
        return f"Sample of {n} is below the minimum of {rule.min_n} for this slice."
    q = percentile_of(rule.statistic)
    if q is not None and n * min(q, 100 - q) / 100 < MIN_TAIL:
        return (f"{n} values put fewer than {MIN_TAIL} beyond the {rule.statistic}, too few to measure "
                f"it; this slice needs at least {MIN_TAIL * 100 / min(q, 100 - q):.0f}.")
    return None


def _against_limit(rule: GateRule, candidate: Sequence[float], d: SliceDecision) -> Optional[Tuple[str, str]]:
    """(outcome, reason) for the candidate against the rule's absolute limit; None when it can't be checked."""
    if rule.absolute_limit is None or not candidate:
        return None
    too_few = _too_few(rule, len(candidate))
    if too_few:
        if not rule.is_high_risk:
            return None
        stat = stat_fn(rule.statistic)
        val = stat(candidate)
        d.candidate_value = val
        limit, what = rule.absolute_limit, f"The candidate's {rule.statistic}"
        side = "minimum" if rule.higher_is_better else "maximum"
        past = (val < limit) if rule.higher_is_better else (val > limit)
        if past:
            return ROLLBACK, f"{what} is {val:.4f}; past the absolute {side} {limit:.4f}."
        return ADVANCE, f"{what} is {val:.4f}; within the absolute {side} {limit:.4f}."
    value, lo, hi = bootstrap_ci(candidate, statistic=rule.statistic)
    d.candidate_value, d.candidate_ci = value, (lo, hi)
    limit, what = rule.absolute_limit, f"The candidate's {rule.statistic}"
    # Past the limit is above it when lower is better, below it when higher is better.
    best, worst = (hi, lo) if rule.higher_is_better else (lo, hi)
    past = (lambda x: x < limit) if rule.higher_is_better else (lambda x: x > limit)
    side = "minimum" if rule.higher_is_better else "maximum"
    if past(best):
        return ROLLBACK, (f"{what} is {value:.4f}; even the optimistic end of its CI ({best:.4f}) "
                          f"is past the absolute {side} {limit:.4f}.")
    if past(worst):
        return HOLD, (f"{what} is {value:.4f}, but its CI reaches {worst:.4f}, past the absolute {side} "
                      f"{limit:.4f}. Collect more samples before deciding.")
    return ADVANCE, f"{what} is {value:.4f}; its CI stays within the absolute {side} {limit:.4f}."


def evaluate_slice(rule: GateRule, slice_name: str, baseline: Optional[Sequence[float]],
                   candidate: Optional[Sequence[float]], floor: Optional[float],
                   paired: bool = True) -> SliceDecision:
    """The candidate against the baseline and, when the rule sets one, against its absolute limit.
    The worse of the two decides: a candidate past the limit rolls back even with no baseline."""
    d = _against_baseline(rule, slice_name, baseline, candidate, floor, paired)
    checked = _against_limit(rule, candidate or [], d)
    if checked:
        outcome, reason = checked
        if _RANK[outcome] > _RANK[d.outcome]:
            d.outcome, d.reason = outcome, f"{reason} {d.reason}"
        else:
            d.reason = f"{d.reason} {reason}"
    return d


def _stability_fallback(rule: GateRule, slice_name: str, baseline: Sequence[float],
                        candidate: Sequence[float], floor: Optional[float],
                        base: SliceDecision) -> SliceDecision:
    """Fallback for small high-risk slices under min_n:
    Per-case stability rules: no case that passed reliably may now fail, and any collapse blocks."""
    n = len(baseline)
    b_mean = fmean(baseline)
    c_mean = fmean(candidate)
    sign = -1 if rule.higher_is_better else 1
    base.worsening = sign * (c_mean - b_mean)

    # 1. Any collapse blocks immediately:
    if rule.higher_is_better:
        if c_mean == 0.0 and b_mean > 0.0:
            base.outcome = ROLLBACK
            base.reason = (f"High-risk slice {slice_name!r} ({n} cases < min_n {rule.min_n}) collapsed: "
                           f"candidate scored 0.0.")
            return base
        if b_mean > 0.0 and c_mean < 0.5 * b_mean:
            base.outcome = ROLLBACK
            base.reason = (f"High-risk slice {slice_name!r} ({n} cases < min_n {rule.min_n}) collapsed: "
                           f"score dropped from {b_mean:.4f} to {c_mean:.4f} (more than 50% loss).")
            return base
    else:
        if c_mean == 1.0 and b_mean < 1.0:
            base.outcome = ROLLBACK
            base.reason = (f"High-risk slice {slice_name!r} ({n} cases < min_n {rule.min_n}) collapsed: "
                           f"100% error rate in candidate.")
            return base

    # 2. Per-case check: no case that passed reliably may now fail
    regressions: List[Tuple[int, float, float]] = []
    for i in range(n):
        b, c = baseline[i], candidate[i]
        if rule.higher_is_better:
            if b >= 1.0 and c < 1.0:
                regressions.append((i, b, c))
        else:
            if b == 0.0 and c > 0.0:
                regressions.append((i, b, c))

    if regressions:
        base.outcome = ROLLBACK
        idx, b_val, c_val = regressions[0]
        base.reason = (f"High-risk slice {slice_name!r} ({n} cases < min_n {rule.min_n}) failed stability check: "
                       f"{len(regressions)} case(s) that passed reliably now failed "
                       f"(case {idx + 1}: baseline {b_val:.2f} -> candidate {c_val:.2f}).")
        return base

    # 3. Passed stability per-case check
    base.outcome = ADVANCE
    base.reason = (f"High-risk slice {slice_name!r} ({n} cases < min_n {rule.min_n}) passed stability per-case check: "
                   f"no case that passed reliably failed, and no collapse.")
    return base


def _against_baseline(rule: GateRule, slice_name: str, baseline: Optional[Sequence[float]],
                      candidate: Optional[Sequence[float]], floor: Optional[float],
                      paired: bool) -> SliceDecision:
    base = SliceDecision(rule.measure_id, slice_name, HOLD, "",
                         n_baseline=len(baseline or []), n_candidate=len(candidate or []),
                         noise_floor=floor, statistic=rule.statistic, absolute_limit=rule.absolute_limit,
                         tolerance=rule.effective_tolerance)
    if not baseline or not candidate:
        base.reason = "Slice is unmeasured; unmeasured never passes."
        return base
    if floor is None:
        base.reason = "Noise floor unknown; run the baseline at least twice first."
        return base
    n = min(len(baseline), len(candidate))
    why = _too_few(rule, n)
    if why:
        if rule.is_high_risk and paired and len(baseline) == len(candidate) and n > 0:
            return _stability_fallback(rule, slice_name, baseline, candidate, floor, base)
        base.reason = why
        return base
    if paired and len(baseline) != len(candidate):
        paired = False

    point, lo, hi = bootstrap_diff_ci(baseline, candidate, paired=paired, statistic=rule.statistic)
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
    noise_floors is keyed by GateRule.key ("latency_ms:p95" for a percentile rule),
    falling back to the measure id.
    required_slices[measure_id] lists slices that must be present; a missing
    one is reported as unmeasured and holds the release. So is a slice with its
    own margin in the rule.
    """
    decisions: List[SliceDecision] = []
    for rule in rules:
        by_slice = dict(samples.get(rule.measure_id, {}))
        for s in [*(required_slices or {}).get(rule.measure_id, []), *rule.slices]:
            by_slice.setdefault(s, {})
        if not by_slice:
            by_slice = {"overall": {}}
        for slice_name, pair in sorted(by_slice.items()):
            d = evaluate_slice(rule.for_slice(slice_name), slice_name, pair.get("baseline"),
                               pair.get("candidate"),
                               noise_floors.get(rule.key, noise_floors.get(rule.measure_id)))
            d.own_margin = slice_name in rule.slices
            decisions.append(d)
    outcome = max((d.outcome for d in decisions), key=_RANK.__getitem__, default=HOLD)
    return GateDecision(outcome, decisions)


@dataclass
class ReleaseGateRow:
    gate: str              # e.g. "quality:accuracy", "operation:latency_ms:p95", "stability:pass_rate", "operation:timeouts"
    kind: str              # "quality" | "operation" | "stability"
    slice: str             # e.g. "overall", "high_value"
    value: Optional[float] # measured candidate value or change
    limit: Optional[str]   # limit or tolerance description
    verdict: str           # "advance" | "advance_with_conditions" | "hold" | "rollback"
    reason: str            # explanatory note
    condition: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ReleaseDecision:
    outcome: str
    rows: List[ReleaseGateRow] = field(default_factory=list)
    conditions: List[str] = field(default_factory=list)
    summary: str = ""

    def to_dict(self) -> dict:
        return {
            "outcome": self.outcome,
            "conditions": self.conditions,
            "summary": self.summary,
            "rows": [asdict(r) for r in self.rows],
        }


def _is_operational_measure(measure_id: str, statistic: str = "mean") -> bool:
    mid = measure_id.lower()
    return (
        statistic != "mean"
        or any(k in mid for k in ("latency", "cost", "timeout", "fallback", "duration", "time", "tokens", "memory"))
    )


def build_release_decision(
    slice_decisions: Sequence[SliceDecision],
    stability: Optional[dict] = None,
    operations: Optional[Dict[str, Any]] = None,
) -> ReleaseDecision:
    """Build a unified release decision table across quality per slice, operational metrics, and stability.
    Quality and operations stay on separate rows and are never blended into one score.
    Returns one call: advance, advance_with_conditions, hold, or rollback.
    """
    rows: List[ReleaseGateRow] = []
    conditions: List[str] = []

    # 1. Quality & operational slice gate rows
    for d in slice_decisions:
        is_op = _is_operational_measure(d.measure_id, d.statistic)
        kind = "operation" if is_op else "quality"
        gate_name = f"{d.measure_id}:{d.statistic}" if d.statistic != "mean" else d.measure_id

        limit_desc = []
        if d.tolerance is not None:
            limit_desc.append(f"tolerance {d.tolerance:.4f}")
        if d.absolute_limit is not None:
            limit_desc.append(f"ceiling {d.absolute_limit:.4f}")
        limit_str = "; ".join(limit_desc) or "N/A"

        verdict = d.outcome
        row_cond = None

        # Check for advance_with_conditions:
        # e.g., an operational metric worsened, but stayed under the ceiling or within tolerance
        if verdict == ADVANCE and is_op and d.worsening is not None and d.worsening > 0:
            verdict = ADVANCE_WITH_CONDITIONS
            pct = ""
            if d.candidate_value and d.worsening:
                base_approx = d.candidate_value - d.worsening
                if base_approx > 0:
                    pct = f" (up {d.worsening / base_approx:.1%})"
            row_cond = f"{gate_name} in {d.slice} up by {d.worsening:.2f}{pct}, under ceiling: watch it in canary"
            conditions.append(row_cond)

        rows.append(ReleaseGateRow(
            gate=f"{kind}:{gate_name}",
            kind=kind,
            slice=d.slice,
            value=d.candidate_value if d.candidate_value is not None else d.worsening,
            limit=limit_str,
            verdict=verdict,
            reason=d.reason,
            condition=row_cond,
            details={"worsening": d.worsening, "ci": d.ci, "candidate_ci": d.candidate_ci},
        ))

    # 2. Stability gate row
    if stability:
        st_outcome = stability.get("outcome", HOLD)
        verdict = st_outcome
        st_cond = None
        if st_outcome == "rerun":
            verdict = HOLD
        elif st_outcome == ADVANCE:
            ch = stability.get("change") or {}
            tol = stability.get("tolerance", 0.01)
            if ch.get("low") is not None and ch.get("low") < -tol:
                verdict = ADVANCE_WITH_CONDITIONS
                st_cond = f"pass rate within tolerance, but 95% CI reaches {ch['low']:.2%}: watch in canary"
                conditions.append(st_cond)

        reasons = "; ".join(stability.get("reasons", [])) or "Pass rate evaluated."
        rows.append(ReleaseGateRow(
            gate="stability:pass_rate",
            kind="stability",
            slice="overall",
            value=stability.get("pass_rate"),
            limit=f"tolerance {stability.get('tolerance', 0.01):.2%}",
            verdict=verdict,
            reason=reasons,
            condition=st_cond,
            details={
                "change": stability.get("change"),
                "noise": stability.get("noise"),
                "checks": stability.get("checks"),
            },
        ))

    # 3. Additional operations (timeouts, fallbacks, etc.)
    if operations:
        for op_name, op_data in operations.items():
            if isinstance(op_data, dict):
                count = op_data.get("count", 0)
                limit = op_data.get("limit")
                reason = op_data.get("reason", "")
            else:
                count = float(op_data)
                limit = None
                reason = ""

            verdict = ADVANCE
            cond = None
            if limit is not None:
                if count > limit:
                    verdict = ROLLBACK
                    reason = reason or f"{op_name} count ({count}) exceeded limit ({limit})."
                elif count > 0:
                    verdict = ADVANCE_WITH_CONDITIONS
                    cond = f"{op_name} ({count}) observed, under limit ({limit}): watch in canary"
                    conditions.append(cond)
                    reason = reason or cond

            rows.append(ReleaseGateRow(
                gate=f"operation:{op_name}",
                kind="operation",
                slice="overall",
                value=count,
                limit=f"<= {limit}" if limit is not None else None,
                verdict=verdict,
                reason=reason or f"{op_name}: {count}",
                condition=cond,
            ))

    # 4. Overall outcome: worst verdict wins
    outcome = max((r.verdict for r in rows), key=_RANK.__getitem__, default=ADVANCE)

    if outcome == ADVANCE:
        summary = "All quality, operational and stability gates passed: advance."
    elif outcome == ADVANCE_WITH_CONDITIONS:
        summary = f"Advance with conditions ({len(conditions)}): {'; '.join(conditions)}."
    elif outcome == HOLD:
        summary = "Release held: one or more gates require investigation, additional samples or reruns."
    else:
        summary = "Roll back: one or more quality, operational or stability gates failed."

    return ReleaseDecision(outcome=outcome, rows=rows, conditions=conditions, summary=summary)
