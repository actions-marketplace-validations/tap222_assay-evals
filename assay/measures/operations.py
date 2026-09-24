"""Operational health: rate, errors, duration, and input drift.

These are the signals an on-call engineer checks first. They also tell a
model problem apart from a traffic problem (roadmap V2-4): if accuracy moves
at the same time as the county mix, the model may be fine.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Dict

from assay.measures.base import (Measure, MeasureOutput, SliceResult, _key, count_by_slice,
                                 quantile_by_slice, ratio_by_slice, unmeasured)
from assay.models import FAILED_STATUSES, Window


class DocumentVolume(Measure):
    id = "document_volume"
    roadmap_ref = "Throughput"
    name = "Documents received"
    question = "How many documents arrived in the window?"
    unit = "count"
    higher_is_better = None
    dimensions = ("county", "instrument_type", "processing_mode")

    def compute(self, source, window: Window) -> MeasureOutput:
        docs = source.documents(window)
        if docs is None:
            return unmeasured(self.id, "Source does not provide documents.")
        return count_by_slice(self.id, docs, self.dimensions)


class StageFailureRate(Measure):
    id = "stage_failure_rate"
    roadmap_ref = "Errors"
    name = "Stage failure rate"
    question = "What share of stage runs failed?"
    higher_is_better = False

    def compute(self, source, window: Window) -> MeasureOutput:
        runs = source.stage_runs(window)
        if runs is None:
            return unmeasured(self.id, "Source does not provide stage runs.")
        return ratio_by_slice(self.id, runs, hit=lambda r: r.status in FAILED_STATUSES,
                              dimensions=self.dimensions)


class CallErrorRate(Measure):
    id = "call_error_rate"
    roadmap_ref = "Errors"
    name = "AI call error rate"
    question = "What share of AI calls returned an error or timed out?"
    higher_is_better = False
    dimensions = ("stage", "model_served")

    def compute(self, source, window: Window) -> MeasureOutput:
        calls = source.calls(window)
        if calls is None:
            return unmeasured(self.id, "Source does not provide AI calls.")
        return ratio_by_slice(self.id, calls, hit=lambda c: c.status in FAILED_STATUSES,
                              base=lambda c: c.status is not None, dimensions=self.dimensions)


class CallLatencyP95(Measure):
    id = "call_latency_p95"
    roadmap_ref = "Duration"
    name = "AI call latency (p95)"
    question = "How long do the slowest 5% of AI calls take?"
    unit = "ms"
    higher_is_better = False
    dimensions = ("stage", "model_served")

    def compute(self, source, window: Window) -> MeasureOutput:
        calls = source.calls(window)
        if calls is None:
            return unmeasured(self.id, "Source does not provide AI calls.")
        return quantile_by_slice(self.id, calls, lambda c: c.latency_ms, 0.95, self.dimensions, self.unit)


def psi_terms(current: Counter, previous: Counter) -> Dict[str, float]:
    """Population stability index, split into each category's contribution.

    Sum of the terms is the PSI. Rule of thumb: below 0.1 stable, 0.1 to 0.2
    moderate, above 0.2 a significant shift.
    """
    ct, pt, eps = sum(current.values()), sum(previous.values()), 1e-4
    terms = {}
    for k in set(current) | set(previous):
        c = max(current[k] / ct, eps)
        p = max(previous[k] / pt, eps)
        terms[k] = (c - p) * math.log(c / p)
    return terms


class InputMixDrift(Measure):
    id = "input_mix_drift"
    roadmap_ref = "V2-4 drift"
    name = "Input mix drift"
    question = "Has the mix of counties, instruments or modes shifted since the previous window?"
    unit = "psi"
    higher_is_better = False
    dimensions = ("county", "instrument_type", "processing_mode")

    def compute(self, source, window: Window) -> MeasureOutput:
        cur, prev = source.documents(window), source.documents(window.previous())
        if cur is None or prev is None:
            return unmeasured(self.id, "Source does not provide documents.")
        cur, prev = list(cur), list(prev)
        if not cur or not prev:
            return unmeasured(self.id, "Needs documents in both this window and the one before it.")

        results, per_dim = [], {}
        for dim in self.dimensions:
            c = Counter(_key(d, dim) for d in cur)
            p = Counter(_key(d, dim) for d in prev)
            terms = psi_terms(c, p)
            per_dim[dim] = sum(terms.values())
            for val in sorted(terms):
                results.append(SliceResult(
                    dim, val, terms[val], c[val],
                    note=f"{c[val] / len(cur):.1%} of documents, was {p[val] / len(prev):.1%}"))
        worst = max(per_dim, key=per_dim.get)
        summary = " · ".join(f"{d.replace('_', ' ')} {v:.3f}" for d, v in per_dim.items())
        overall = SliceResult(None, None, per_dim[worst], len(cur),
                              note=f"PSI {summary}. Above 0.2 is a significant shift.")
        return MeasureOutput(self.id, "measured", [overall] + results)
