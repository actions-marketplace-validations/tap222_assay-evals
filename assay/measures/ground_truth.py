"""Measures that need labelled ground truth.

Registered now so they appear in the catalog and on the dashboard as
"unmeasured" with the reason, rather than being absent. Each one gets a real
compute() once the labels it needs can be ingested.
"""
from __future__ import annotations

from collections import defaultdict
from typing import List, Optional, Tuple

from assay.measures.base import Measure, MeasureOutput, SliceResult, unmeasured
from assay.models import UNRECORDED, Window


class _AwaitingTruth(Measure):
    tag = "Accuracy"
    waiting_on: str = ""

    def compute(self, source, window: Window) -> MeasureOutput:
        return unmeasured(self.id, f"Needs ground truth: {self.waiting_on}")


class SplitStraightThrough(_AwaitingTruth):
    """From files scored against their correct boundaries (assay_sdk.documents.score_split): the share
    of files holding several documents where every document came out on the right pages. A test
    set's files had no person split them, so right is straight-through."""
    id = "split_stp"
    name = "Document splitting straight-through"
    question = "What share of multi-document files split correctly with no human touch?"
    dimensions = ("segment",)
    waiting_on = "files with human-confirmed document boundaries (assay_sdk.documents.score_split)."

    def compute(self, source, window: Window) -> MeasureOutput:
        scores = source.split_scores(window) if hasattr(source, "split_scores") else None
        if scores is None:
            return super().compute(source, window)
        multi = [s for s in scores if s["documents"] > 1]

        def one(dim, val, group):
            return SliceResult(dim, val, sum(s["right"] for s in group) / len(group) if group else None, len(group),
                               numerator=sum(s["right"] for s in group), denominator=len(group))
        results = [one(None, None, multi)]
        by = defaultdict(list)
        for s in multi:
            by[s["segment"] if s["segment"] not in (None, "") else UNRECORDED].append(s)
        results += [one("segment", v, g) for v, g in sorted(by.items())]
        return MeasureOutput(self.id, "measured", results)


class FieldAccuracy(_AwaitingTruth):
    """From fields scored against their correct values (assay_sdk.documents.score_document): the
    share right, each field weighted by what an error costs (its `weight`), line items by their
    row F1. Unmeasured until the first scored document arrives."""
    id = "field_accuracy"
    name = "Severity-weighted field accuracy"
    question = "How often is each extracted field right, weighted by what an error costs?"
    dimensions = ("segment", "document_type", "field")
    waiting_on = "a labelled evaluation set with correct values per field (assay_sdk.documents.score_document)."

    def compute(self, source, window: Window) -> MeasureOutput:
        scores = source.field_scores(window) if hasattr(source, "field_scores") else None
        if scores is None:
            return super().compute(source, window)
        scores = [s for s in scores if not s.get("part_of")]  # a group counts whole, like a table
        if not scores:
            return MeasureOutput(self.id, "measured", [SliceResult(None, None, None, 0)])

        def one(dim, val, group):
            w = sum(s["weight"] for s in group)
            right = sum(s["weight"] * s["share"] for s in group)
            return SliceResult(dim, val, right / w if w else None, len({s["document_id"] for s in group}),
                               numerator=right, denominator=w)
        results = [one(None, None, scores)]
        for dim in self.dimensions:
            by = defaultdict(list)
            for s in scores:
                by[s[dim] if s[dim] not in (None, "") else UNRECORDED].append(s)
            results += [one(dim, v, g) for v, g in sorted(by.items())]
        return MeasureOutput(self.id, "measured", results)


class DocumentAccuracy(_AwaitingTruth):
    """Documents with zero extraction errors: the number that governs automation, since one wrong
    field means a person touches the document. Stricter than field accuracy by design."""
    id = "document_accuracy"
    name = "Documents with zero errors"
    question = "What share of documents came out with every field right?"
    dimensions = ("segment", "document_type")
    waiting_on = "a labelled evaluation set with correct values per field (assay_sdk.documents.score_document)."

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.document_checks(window, "document") if hasattr(source, "document_checks") else None
        if rows is None:
            return super().compute(source, window)
        from assay.measures.documents import _slices
        return _slices(self.id, rows, self.dimensions, lambda g: (sum(r["passed"] for r in g), len(g)))


class CriticalDocumentAccuracy(_AwaitingTruth):
    """Documents with every critical field right (score_document critical=): the ones that could
    go straight through, whatever the low-stakes fields say."""
    id = "critical_document_accuracy"
    name = "Documents right on critical fields"
    question = "What share of documents had every critical field right, so could go straight through?"
    dimensions = ("segment", "document_type")
    waiting_on = "documents scored with their critical fields named (assay_sdk.documents.score_document, critical=)."

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.document_checks(window, "document") if hasattr(source, "document_checks") else None
        rows = None if rows is None else [r for r in rows if r["raw"].get("critical_correct") is not None]
        if not rows:
            return super().compute(source, window)
        from assay.measures.documents import _slices
        return _slices(self.id, rows, self.dimensions,
                       lambda g: (sum(bool(r["raw"]["critical_correct"]) for r in g), len(g)))


class CriticalFieldAccuracy(_AwaitingTruth):
    """Of the critical values (a tax number, the total), the share right: the one to hold at 99.9%
    for straight-through processing. Its n says how far to trust it: 99.9% takes thousands."""
    id = "critical_field_accuracy"
    name = "Critical field accuracy"
    question = "Of the values in critical fields, what share were extracted right?"
    dimensions = ("segment", "document_type", "field")
    waiting_on = "documents scored with their critical fields named (assay_sdk.documents.score_document, critical=)."

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.field_scores(window) if hasattr(source, "field_scores") else None
        rows = None if rows is None else [r for r in rows if r.get("critical")]
        if not rows:
            return super().compute(source, window)
        from assay.measures.documents import _slices
        return _slices(self.id, rows, self.dimensions, lambda g: (sum(r["right"] for r in g), len(g)))


class _Confidence(_AwaitingTruth):
    """From fields scored with the extractor's confidence (score_document confidence=): whether it
    can decide what skips review. Values in n; slices by segment, document type and field."""
    tag = "Confidence"
    dimensions = ("segment", "document_type", "field")
    waiting_on = "fields scored with the extractor's confidence (assay_sdk.documents.score_document, confidence=)."

    def value(self, pairs: List[tuple]) -> Tuple[Optional[float], Optional[float], Optional[float]]:
        raise NotImplementedError  # pragma: no cover

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.field_scores(window) if hasattr(source, "field_scores") else None
        rows = None if rows is None else [r for r in rows if r.get("confidence") is not None and not r.get("part_of")]
        if not rows:
            return super().compute(source, window)

        def one(dim, val, group):
            v, num, den = self.value([(max(0.0, min(1.0, float(r["confidence"]))), r["right"]) for r in group])
            return SliceResult(dim, val, v, len(group), numerator=num, denominator=den)
        results = [one(None, None, rows)]
        for dim in self.dimensions:
            by = defaultdict(list)
            for r in rows:
                by[r[dim] if r[dim] not in (None, "") else UNRECORDED].append(r)
            results += [one(dim, v, g) for v, g in sorted(by.items())]
        return MeasureOutput(self.id, "measured", results)


class ConfidenceAurc(_Confidence):
    """The area under the risk-coverage curve: approving values from the most confident down, the
    mean share wrong among those approved. Lower is better; it measures the ranking itself, so a
    confidence that sorts wrong values last scores well even if its numbers are off."""
    id = "confidence_aurc"
    name = "Risk-coverage (AURC)"
    question = "Approving values from the most confident down, how many wrong ones get through along the way?"
    higher_is_better = False

    def value(self, pairs):
        from assay.documents import risk_coverage
        return risk_coverage(pairs)["aurc"], None, None


class ConfidentErrors(_Confidence):
    """Of the values the extractor stated at 0.90 or more, the share wrong: the practical check of
    whether its high confidence can be approved on. Its n says how far to trust it (100 or more)."""
    id = "confident_error_rate"
    name = "Wrong at 0.90+ confidence"
    question = "Of the values stated at 0.90 confidence or more, what share were wrong?"
    higher_is_better = False

    def value(self, pairs):
        from assay.documents import BAND
        band = [ok for c, ok in pairs if c >= BAND]
        wrong = len(band) - sum(band)
        return (wrong / len(band) if band else None), wrong, len(band)


class ConfidenceCalibration(_Confidence):
    """Expected calibration error: the gap between the confidence stated and how often it's right,
    over ten bands. Beside AURC: calibration says whether the numbers mean what they say, AURC
    whether they rank right from wrong."""
    id = "confidence_ece"
    name = "Confidence calibration error"
    question = "How far is the confidence stated from how often values are right?"
    higher_is_better = False

    def value(self, pairs):
        from assay.documents import confidence
        return confidence([list(p) for p in pairs])["ece"], None, None


class _MadeUp(_AwaitingTruth):
    """Of the values extracted, the share made up this way (assay_sdk.documents.score_document).
    Line items aren't sorted, so their tables aren't counted; a group counts by its parts."""
    higher_is_better = False
    dimensions = ("segment", "document_type", "field")
    made_up: str = ""
    grounded = True  # told apart only when the document's text was given

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.field_scores(window, grounded=self.grounded) if hasattr(source, "field_scores") else None
        if rows is None:
            return super().compute(source, window)
        if self.grounded and not rows:
            return unmeasured(self.id, "No fields scored with the document's text yet: "
                                       "score_document(..., text=ocr_text).")
        from assay.measures.documents import _slices
        values = [r for r in rows if r["extracted"] and not r["table"] and not r.get("group")]
        return _slices(self.id, values, self.dimensions,
                       lambda g: (sum(r["made_up"] == self.made_up for r in g), len(g)))


class FabricatedValues(_MadeUp):
    """Values nowhere in the document: invented outright."""
    id = "fabricated_value_rate"
    name = "Fabricated values"
    question = "Of the values extracted, how many appear nowhere in the document?"
    made_up = "fabricated"
    waiting_on = "fields scored with the document's text (assay_sdk.documents.score_document, text=)."


class InferredValues(_MadeUp):
    """Values that are in the document, just not as this field: a guess from context (the state
    mentioned most for governing law, a county, the seller as the buyer). Right-looking, so the
    costliest to miss in review."""
    id = "inferred_value_rate"
    name = "Inferred values"
    question = "Of the values extracted, how many are guesses from context: in the document, but not this field?"
    made_up = "inferred"
    waiting_on = "fields scored with the document's text (assay_sdk.documents.score_document, text=)."


class FormatErrors(_MadeUp):
    """The right value in the wrong shape: day and month swapped, a decimal separator read wrong.
    Told apart without the document's text."""
    id = "format_error_rate"
    name = "Format errors"
    question = "Of the values extracted, how many hold the right information in the wrong shape?"
    made_up = "format"
    grounded = False
    waiting_on = "a labelled evaluation set with correct values per field (assay_sdk.documents.score_document)."


class SupersededValues(_AwaitingTruth):
    """From documents checked against the later ones that amend or replace them
    (assay_sdk.documents.superseded_values): of the values a later document changed, the share
    that output still holds, unmarked. Updated values and ones flagged as superseded are fine."""
    id = "superseded_value_rate"
    name = "Superseded values reaching output"
    question = "How often does a value that a later document replaced reach output unflagged?"
    higher_is_better = False
    dimensions = ("segment", "document_type", "link", "field")
    waiting_on = "links between documents that amend or replace each other (assay_sdk.documents.superseded_values)."

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.document_checks(window, "superseded", "assay.superseded@1") \
            if hasattr(source, "document_checks") else None
        if rows is None:
            return super().compute(source, window)
        for r in rows:
            r["link"] = r["raw"].get("link")
        from assay.measures.documents import _slices
        return _slices(self.id, rows, self.dimensions,
                       lambda g: (sum(r["raw"].get("outcome") == "escaped" for r in g), len(g)))


class EscapeRate(_AwaitingTruth):
    """From spot checks of published output (assay_sdk.documents.spot_check): the share of values
    checked that were wrong. Everything published has cleared automation and, where it applied,
    review; `path` says which way each went out (reviewed, or auto-approved), so the slices say
    which one lets more through. A sample: its n says how far to trust it."""
    id = "escape_rate"
    name = "Escape rate"
    question = "How often does a wrong value clear both automation and human review?"
    higher_is_better = False
    dimensions = ("segment", "document_type", "path", "field")
    waiting_on = "a re-verified spot-check sample of published output (assay_sdk.documents.spot_check)."

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = source.document_checks(window, "spot_check", "assay.spotcheck@1") \
            if hasattr(source, "document_checks") else None
        if rows is None:
            return super().compute(source, window)
        for r in rows:
            r["path"] = r["raw"].get("path")
        from assay.measures.documents import _slices
        return _slices(self.id, rows, self.dimensions, lambda g: (sum(not r["passed"] for r in g), len(g)))
