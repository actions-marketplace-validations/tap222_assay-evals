"""Document quality over time, from the checks assay_sdk.documents records: OCR (characters and
digits wrong, reading order), fields read from the right place, table cells, field cells, and
the escape rate from spot checks of published output. Each is unmeasured until its first check arrives,
and says which call sends it."""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, List, Optional, Tuple

from assay.measures.base import Measure, MeasureOutput, SliceResult, unmeasured
from assay.models import UNRECORDED, Window


def facet_slices(rows: List[dict], one: Callable) -> List[SliceResult]:
    """One slice per facet value the rows carry (score_document facets=, or sent with the
    document): source, quality, stamps, handwriting, language, currency, template_seen,
    template_new. Only facets that were sent: a row without one isn't an "unrecorded" slice.
    The template id itself is left out (a slice per supplier): template_seen and template_new
    are its slices."""
    names = sorted({k for r in rows for k in (r.get("facets") or {}) if k != "template"})
    out = []
    for k in names:
        by = defaultdict(list)
        for r in rows:
            v = (r.get("facets") or {}).get(k)
            if v not in (None, ""):
                by[v].append(r)
        out += [one(k, v, g) for v, g in sorted(by.items())]
    return out


def _slices(measure_id: str, rows: List[dict], dimensions, ratio: Callable[[List[dict]], Tuple[float, float]]
            ) -> MeasureOutput:
    """A ratio (numerator, denominator) overall and per slice, n being the documents in it, with a
    slice per facet value the rows carry."""
    def one(dim, val, group):
        num, den = ratio(group)
        return SliceResult(dim, val, num / den if den else None, len({r["document_id"] for r in group}),
                           numerator=num, denominator=den)
    results = [one(None, None, rows)]
    for dim in dimensions:
        by = defaultdict(list)
        for r in rows:
            by[r.get(dim) if r.get(dim) not in (None, "") else UNRECORDED].append(r)
        results += [one(dim, v, g) for v, g in sorted(by.items())]
    return MeasureOutput(measure_id, "measured", results + facet_slices(rows, one))


class _FromChecks(Measure):
    tag = "Accuracy"
    kind: str = ""
    evaluator: str = "assay.documents@1"
    sent_by: str = ""
    dimensions = ("document_type", "segment")

    def ratio(self, rows: List[dict]) -> Tuple[float, float]:  # pragma: no cover
        raise NotImplementedError

    def rows(self, source, window: Window) -> Optional[List[dict]]:
        return source.document_checks(window, self.kind, self.evaluator) if hasattr(source, "document_checks") \
            else None

    def compute(self, source, window: Window) -> MeasureOutput:
        rows = self.rows(source, window)
        if rows is None:
            return unmeasured(self.id, f"Nothing sent yet: {self.sent_by}.")
        return _slices(self.id, rows, self.dimensions, self.ratio)


class OcrCharacterErrors(_FromChecks):
    id = "ocr_cer"
    name = "OCR characters wrong"
    question = "Of the characters on the pages, what share did OCR read wrong?"
    higher_is_better = False
    kind = "ocr"
    sent_by = "OCR text scored against what the page says (assay_sdk.documents.score_ocr)"

    def ratio(self, rows):
        return sum(r["raw"].get("char_errors") or 0 for r in rows), sum(r["raw"].get("chars") or 0 for r in rows)


class OcrDigitErrors(OcrCharacterErrors):
    id = "ocr_digit_error_rate"
    name = "OCR digits wrong"
    question = "Of the digits on the pages, what share did OCR read wrong? A wrong digit is a wrong amount."

    def ratio(self, rows):
        return sum(r["raw"].get("digit_errors") or 0 for r in rows), sum(r["raw"].get("digits") or 0 for r in rows)


class OcrLetterErrors(OcrCharacterErrors):
    id = "ocr_letter_error_rate"
    name = "OCR letters wrong"
    question = "Of the letters on the pages, what share did OCR read wrong? Names and codes live here."

    def rows(self, source, window):
        rows = super().rows(source, window)
        return None if rows is None else [r for r in rows if "letters" in r["raw"]]  # sent before letters were

    def ratio(self, rows):
        return sum(r["raw"].get("letter_errors") or 0 for r in rows), sum(r["raw"].get("letters") or 0 for r in rows)


class OcrReadingOrder(OcrCharacterErrors):
    id = "ocr_reading_order"
    name = "OCR reading order"
    question = "What share of lines did OCR read in the page's order?"
    higher_is_better = True

    def ratio(self, rows):  # lines weighted by the page's characters: a long page counts for more
        scored = [r for r in rows if r["raw"].get("order") is not None]
        return (sum(r["raw"]["order"] * (r["raw"].get("chars") or 1) for r in scored),
                sum(r["raw"].get("chars") or 1 for r in scored))


class LocationAccuracy(_FromChecks):
    id = "location_accuracy"
    name = "Fields read from the right place"
    question = "What share of fields came from the right page and box?"
    kind = "location"
    sent_by = "field locations scored against the correct boxes (assay_sdk.documents.score_locations)"
    dimensions = ("document_type", "segment", "field")

    def rows(self, source, window):
        rows = super().rows(source, window)
        for r in rows or []:  # "location: total" is the field total
            r["field"] = r["field"].split(": ", 1)[-1]
        return rows

    def ratio(self, rows):
        return sum(r["passed"] for r in rows), len(rows)


class TableCellAccuracy(_FromChecks):
    id = "table_cell_f1"
    name = "Table cells right"
    question = "Of table cells, how many were read right, counting those lost and those made up (F1)?"
    kind = "table"
    sent_by = "tables scored against the correct ones (assay_sdk.documents.score_table)"

    def ratio(self, rows):
        right = sum(r["raw"].get("cells_right") or 0 for r in rows)
        return 2 * right, sum((r["raw"].get("cells") or 0) + (r["raw"].get("cells_read") or 0) for r in rows)


class FieldCellF1(_FromChecks):
    """ExtractBench's single number: each document flattened into cells, one per field and one per
    line-item cell in an aligned row, and F1 over them. Unweighted, beside field_accuracy's
    weighted share: headers and line items under one definition."""
    id = "field_cell_f1"
    name = "Field cells right"
    question = "Of the values documents hold, headers and line items alike, how many were extracted right (F1)?"
    kind = "document"
    sent_by = "fields scored against their correct values (assay_sdk.documents.score_document)"

    def rows(self, source, window):
        rows = super().rows(source, window)
        return None if rows is None else [r for r in rows if "cells" in r["raw"]]  # sent before cells were

    def ratio(self, rows):
        c = {k: sum((r["raw"].get("cells") or {}).get(k) or 0 for r in rows) for k in ("tp", "fp", "fn")}
        return 2 * c["tp"], 2 * c["tp"] + c["fp"] + c["fn"]


class TableTeds(_FromChecks):
    """TEDS, the standard table score: the tables as trees of rows and cells, 1 - their edit
    distance over the larger's size. Structure and text together; the mean over tables."""
    id = "table_teds"
    name = "Table similarity (TEDS)"
    question = "How close is each table read to the correct one, in structure and text (TEDS)?"
    kind = "table"
    sent_by = "tables scored against the correct ones (assay_sdk.documents.score_table)"

    def rows(self, source, window):
        rows = super().rows(source, window)
        return None if rows is None else [r for r in rows if r["raw"].get("teds") is not None]

    def ratio(self, rows):
        return sum(r["raw"]["teds"] for r in rows), len(rows)


class _SplitChecks(_FromChecks):
    kind = "split"
    sent_by = "files scored against their correct boundaries (assay_sdk.documents.score_split)"
    dimensions = ("segment", "document_type")

    def rows(self, source, window):
        rows = super().rows(source, window)
        return None if rows is None else [r for r in rows if r["raw"].get("panoptic")]  # sent before these were


class SplitPanopticQuality(_SplitChecks):
    """Panoptic quality, borrowed from image segmentation and found the most fitting metric for
    page stream segmentation: documents matched when they share over half their pages, each match
    weighted by how much, over matches plus half the documents unmatched on either side."""
    id = "split_pq"
    name = "Splitting panoptic quality"
    question = "How well do the documents a file was split into match the real ones (panoptic quality)?"

    def ratio(self, rows):
        p = {k: sum(r["raw"]["panoptic"].get(k) or 0 for r in rows) for k in ("iou", "tp", "fp", "fn")}
        return p["iou"], p["tp"] + 0.5 * p["fp"] + 0.5 * p["fn"]


class SplitDragRate(_SplitChecks):
    """The fewest pages a reviewer must drag to put each split right (minimum drags and drops), as a
    share of pages: what split errors cost in human time."""
    id = "split_drag_rate"
    name = "Pages moved by hand"
    question = "Of the pages in split files, how many would a reviewer have to drag to put the split right?"
    higher_is_better = False

    def ratio(self, rows):
        return sum(r["raw"].get("drags") or 0 for r in rows), sum(r["raw"].get("pages") or 0 for r in rows)


class SplitReworkCost(_SplitChecks):
    """What wrong splits cost in reviewer time, per file split: the pages to drag (minimum drags
    and drops) x seconds_per_drag x the rework rate (the review rate when there's none), from the
    rate card. An estimate from scored files, apart from cost_per_document: recorded rework minutes
    already count there, and would be counted twice."""
    id = "split_rework_cost"
    tag = "Cost"
    name = "Split rework cost per file"
    question = "What does fixing wrong splits cost a reviewer, per file (USD)?"
    higher_is_better = False

    def compute(self, source, window: Window) -> MeasureOutput:
        rates = getattr(source, "cost_rates", None) or {}
        sec = rates.get("seconds_per_drag")
        per_hour = rates.get("rework_per_hour", rates.get("review_per_hour"))
        rows = self.rows(source, window)
        if rows is None:
            return unmeasured(self.id, f"Nothing sent yet: {self.sent_by}.")
        need = (["seconds_per_drag"] if not sec else []) + \
            (["rework_per_hour (or review_per_hour)"] if per_hour is None else [])
        if need:
            return unmeasured(self.id, f"Set {' and '.join(need)} on the rate card to price the pages moved by hand.")
        usd = sec / 3600 * per_hour
        for r in rows:
            r["raw"]["usd"] = (r["raw"].get("drags") or 0) * usd
        return _slices(self.id, rows, self.dimensions, lambda g: (sum(r["raw"]["usd"] for r in g), len(g)))
