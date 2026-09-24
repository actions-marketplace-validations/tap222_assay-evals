from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Sequence, TypeVar

from assay.models import UNRECORDED, Window
from assay.units import fmt

T = TypeVar("T")


@dataclass
class SliceResult:
    dimension: Optional[str]  # None = overall
    slice_value: Optional[str]
    value: Optional[float]
    n: int
    numerator: Optional[float] = None
    denominator: Optional[float] = None
    note: Optional[str] = None
    # Standard error of `value`, when the measure can compute one (e.g. a mean
    # of skewed per-document costs). Alert bands widen to at least 3x this.
    stderr: Optional[float] = None


@dataclass
class MeasureOutput:
    measure_id: str
    status: str  # "measured" | "unmeasured"
    results: List[SliceResult] = field(default_factory=list)
    reason: Optional[str] = None

    @property
    def overall(self) -> Optional[SliceResult]:
        return next((r for r in self.results if r.dimension is None), None)


def unmeasured(measure_id: str, reason: str) -> MeasureOutput:
    return MeasureOutput(measure_id, "unmeasured", reason=reason)


class Measure:
    """A named question about the pipeline, answered per slice.

    Subclasses set the metadata and implement compute(). Every measure reports
    an overall row plus one row per value of each dimension, so a failure in
    one segment can't hide inside a healthy aggregate.
    """

    id: str = ""
    tag: str = ""  # short category shown on the dashboard, e.g. "Errors"
    name: str = ""
    question: str = ""
    unit: str = "ratio"  # ratio | usd | seconds | ms | count | psi
    # True / False say which way is good. None = neither (e.g. volume), so an
    # anomaly in either direction is worth a look.
    higher_is_better: Optional[bool] = True
    dimensions: Sequence[str] = ("stage",)
    # False for reporting measures whose moves mostly track volume (e.g. total
    # spend): they're charted and can't raise anomaly alerts.
    anomaly_alerts: bool = True

    def compute(self, source, window: Window) -> MeasureOutput:  # pragma: no cover
        raise NotImplementedError

    def describe(self) -> dict:
        return {
            "id": self.id, "tag": self.tag, "name": self.name,
            "question": self.question, "unit": self.unit,
            "higher_is_better": self.higher_is_better, "dimensions": list(self.dimensions),
        }


def _key(record, dim: str) -> str:
    v = getattr(record, dim, None)
    return UNRECORDED if v in (None, "") else str(v)


def ratio_by_slice(measure_id: str, records: Iterable[T], hit: Callable[[T], bool],
                   dimensions: Sequence[str], base: Callable[[T], bool] = lambda r: True) -> MeasureOutput:
    """Share of `base` records for which `hit` is true, overall and per slice."""
    rows = [r for r in records if base(r)]
    if not rows:
        return unmeasured(measure_id, "No records in this window.")

    def one(dim, val, group):
        num = sum(1 for r in group if hit(r))
        return SliceResult(dim, val, num / len(group), len(group), num, len(group))

    results = [one(None, None, rows)]
    for dim in dimensions:
        groups: Dict[str, list] = defaultdict(list)
        for r in rows:
            groups[_key(r, dim)].append(r)
        results += [one(dim, val, g) for val, g in sorted(groups.items())]
    return MeasureOutput(measure_id, "measured", results)


def grouped(records: Sequence[T], dimensions: Sequence[str]):
    """Yield (dimension, slice_value, records): overall first, then each slice."""
    yield None, None, list(records)
    for dim in dimensions:
        groups: Dict[str, list] = defaultdict(list)
        for r in records:
            groups[_key(r, dim)].append(r)
        for val, g in sorted(groups.items()):
            yield dim, val, g


def count_by_slice(measure_id: str, records: Iterable[T], dimensions: Sequence[str]) -> MeasureOutput:
    """Record counts. Zero is a real measurement here (an outage looks like zero)."""
    rows = list(records)
    return MeasureOutput(measure_id, "measured",
                         [SliceResult(d, v, float(len(g)), len(g)) for d, v, g in grouped(rows, dimensions)])


def quantile_by_slice(measure_id: str, records: Iterable[T], value: Callable[[T], Optional[float]],
                      q: float, dimensions: Sequence[str], unit: str = "") -> MeasureOutput:
    """q-quantile of value(record), overall and per slice, ignoring missing values."""
    rows = [r for r in records if value(r) is not None]
    if not rows:
        return unmeasured(measure_id, "No records with a value in this window.")
    return MeasureOutput(measure_id, "measured", [
        SliceResult(d, v, quantile([value(r) for r in g], q), len(g),
                    note=f"Median {fmt(quantile([value(r) for r in g], 0.5), unit)}")
        for d, v, g in grouped(rows, dimensions)])


def quantile(values: List[float], q: float) -> float:
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)
