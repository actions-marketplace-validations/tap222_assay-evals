"""Pipeline integrity: measures computable from pipeline telemetry alone.

None of these need labelled data. They catch the failures that make every
other number untrustworthy: work that silently didn't happen, spend that
isn't recorded, models that aren't the ones declared, documents that never
reach the system downstream.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from assay.measures.base import (Measure, MeasureOutput, SliceResult, _key, quantile,
                                 ratio_by_slice, unmeasured)
from assay.models import Window
from assay.units import fmt


class FallbackAttribution(Measure):
    id = "fallback_attribution"
    tag = "Lineage"
    name = "Fallback attribution"
    question = "What share of AI calls record which tier answered and why?"
    dimensions = ("stage", "model_served")

    def compute(self, source, window: Window) -> MeasureOutput:
        calls = source.calls(window)
        if calls is None:
            return unmeasured(self.id, "Source does not provide AI calls.")
        return ratio_by_slice(self.id, calls,
                              hit=lambda c: bool(c.resolving_layer) and bool(c.gate_reason),
                              dimensions=self.dimensions)


class ModelMismatch(Measure):
    id = "model_mismatch"
    tag = "Lineage"
    name = "Declared vs served model"
    question = "How often is a call served by a different model than it declared?"
    higher_is_better = False
    dimensions = ("stage", "model_declared", "prompt")

    def compute(self, source, window: Window) -> MeasureOutput:
        calls = source.calls(window)
        if calls is None:
            return unmeasured(self.id, "Source does not provide AI calls.")
        return ratio_by_slice(self.id, calls,
                              hit=lambda c: c.model_served != c.model_declared,
                              base=lambda c: bool(c.model_declared) and bool(c.model_served),
                              dimensions=self.dimensions)


class CostCoverage(Measure):
    id = "cost_coverage"
    tag = "Cost"
    name = "Cost coverage"
    question = "What share of AI calls carry a recorded cost?"

    def compute(self, source, window: Window) -> MeasureOutput:
        calls = source.calls(window)
        if calls is None:
            return unmeasured(self.id, "Source does not provide AI calls.")
        calls = list(calls)
        out = ratio_by_slice(self.id, calls, hit=lambda c: c.cost_usd is not None,
                             dimensions=self.dimensions)
        if out.overall:
            total = sum(c.cost_usd for c in calls if c.cost_usd is not None)
            out.overall.note = f"Recorded spend ${total:,.2f} is a floor while coverage is below 100%."
        return out


class RevisionCoverage(Measure):
    id = "revision_coverage"
    tag = "Lineage"
    name = "Code revision coverage"
    question = "What share of AI calls record the code revision that made them?"

    def compute(self, source, window: Window) -> MeasureOutput:
        calls = source.calls(window)
        if calls is None:
            return unmeasured(self.id, "Source does not provide AI calls.")
        calls = list(calls)
        out = ratio_by_slice(self.id, calls, hit=lambda c: bool(c.code_revision),
                             dimensions=self.dimensions)
        if out.overall:
            weeks: Dict[str, set] = defaultdict(set)
            for c in calls:
                weeks[c.ts.strftime("%G-W%V")].add(c.code_revision)
            recorded = sorted(w for w, revs in weeks.items() if revs - {None, ""})
            out.overall.note = (f"{len(recorded)} of {len(weeks)} weeks record a revision. "
                                "A stability band needs 3 quiet adjacent week pairs.")
        return out


class NoOpStages(Measure):
    id = "noop_stage_rate"
    tag = "Integrity"
    name = "Success without work"
    question = "What share of successful stage runs did no work at all?"
    higher_is_better = False

    def compute(self, source, window: Window) -> MeasureOutput:
        runs = source.stage_runs(window)
        if runs is None:
            return unmeasured(self.id, "Source does not provide stage runs.")
        return ratio_by_slice(self.id, runs, hit=lambda r: r.did_work is False,
                              base=lambda r: r.status in ("success", "completed") and r.did_work is not None,
                              dimensions=self.dimensions)


class SourcePositions(Measure):
    id = "source_positions"
    tag = "Traceability"
    name = "Machine-traceable values"
    question = "What share of extracted values carry a source position a reviewer can click through to?"
    dimensions = ("segment", "document_type")

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.indexed(window)
        if rows is None:
            return unmeasured(self.id, "Source does not provide indexed output.")
        return ratio_by_slice(self.id, rows, hit=lambda r: r.has_positions, dimensions=self.dimensions)


class HandoffLoss(Measure):
    id = "handoff_loss"
    tag = "Delivery"
    name = "Lost at the handoff"
    question = "What share of finished documents never reached the downstream system?"
    higher_is_better = False
    dimensions = ("segment", "processing_mode")

    def compute(self, source, window: Window) -> MeasureOutput:
        downstream = source.downstream_hashes()
        if downstream is None:
            return unmeasured(self.id, "No downstream system configured to join against.")
        docs = source.documents(window)
        if docs is None:
            return unmeasured(self.id, "Source does not provide documents.")
        return ratio_by_slice(self.id, docs, hit=lambda d: d.file_hash not in downstream,
                              base=lambda d: d.completed_at is not None and bool(d.file_hash),
                              dimensions=self.dimensions)


class TimeToComplete(Measure):
    id = "time_to_complete_p90"
    tag = "Latency"
    name = "Time to complete (p90)"
    question = "How long does the slowest tenth of documents take from arrival to done?"
    unit = "seconds"
    higher_is_better = False
    dimensions = ("processing_mode", "segment")

    def compute(self, source, window: Window) -> MeasureOutput:
        docs = source.documents(window)
        if docs is None:
            return unmeasured(self.id, "Source does not provide documents.")
        docs = list(docs)
        if not docs:
            return unmeasured(self.id, "No documents in this window.")

        def one(dim, val, group) -> SliceResult:
            done = [(d.completed_at - d.received_at).total_seconds() for d in group if d.completed_at]
            if not done:
                return SliceResult(dim, val, None, len(group), 0, len(group),
                                   note="No document in this slice records a completion.")
            p50 = quantile(done, 0.5)
            return SliceResult(dim, val, quantile(done, 0.9), len(done), len(done), len(group),
                               note=f"Median {fmt(p50, 'seconds')} · {len(done) / len(group):.1%} record a completion")

        results: List[SliceResult] = [one(None, None, docs)]
        for dim in self.dimensions:
            groups: Dict[str, list] = defaultdict(list)
            for d in docs:
                groups[_key(d, dim)].append(d)
            results += [one(dim, v, g) for v, g in sorted(groups.items())]
        return MeasureOutput(self.id, "measured", results)
