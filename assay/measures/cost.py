"""Cost: what a document actually costs, and where the money goes."""
from __future__ import annotations

from collections import defaultdict
from typing import Dict, List

from assay.cost import COMPONENT_LABEL, COMPONENTS, build_ledger
from assay.measures.base import Measure, MeasureOutput, SliceResult, ratio_by_slice, unmeasured
from assay.models import UNRECORDED, Window


def _rates(source) -> Dict[str, float]:
    return getattr(source, "cost_rates", None) or {}


def _ledger_or_reason(measure_id, source, window):
    ledger = build_ledger(source, window, _rates(source))
    if ledger is None:
        return None, unmeasured(measure_id, "Source does not provide documents.")
    if not ledger.documents:
        return None, unmeasured(measure_id, "No documents in this window.")
    return ledger, None


def _key(v) -> str:
    return UNRECORDED if v in (None, "") else str(v)


def _mean_se(values: List[float]):
    n = len(values)
    mean = sum(values) / n
    if n < 2:
        return mean, None
    var = sum((v - mean) ** 2 for v in values) / (n - 1)
    return mean, (var / n) ** 0.5


class CostPerDocument(Measure):
    id = "cost_per_document"
    tag = "Cost"
    name = "Cost per document"
    question = "What does one document cost to process, fully loaded, and where does the money go?"
    unit = "usd"
    higher_is_better = False
    dimensions = ("component", "document_type", "segment", "processing_mode", "stage")

    def compute(self, source, window: Window) -> MeasureOutput:
        ledger, why = _ledger_or_reason(self.id, source, window)
        if why:
            return why
        ids = list(ledger.documents)
        n = len(ids)
        # Per-document amounts: overall, and per component / stage (zeros included,
        # so each slice is "average per document across all documents").
        total, by_comp, by_stage = defaultdict(float), defaultdict(lambda: defaultdict(float)), defaultdict(lambda: defaultdict(float))
        for ln in ledger.lines:
            total[ln.document_id] += ln.usd
            by_comp[COMPONENT_LABEL[ln.component]][ln.document_id] += ln.usd
            if ln.stage:
                by_stage[ln.stage][ln.document_id] += ln.usd

        def row(dim, val, per_doc: Dict[str, float], doc_ids, note=None):
            vals = [per_doc.get(i, 0.0) for i in doc_ids]
            mean, se = _mean_se(vals)
            return SliceResult(dim, val, mean, len(vals), sum(vals), len(vals), note=note, stderr=se)

        grand = sum(total.values())
        mix = ", ".join(f"{label} {sum(by_comp[label].values()) / grand:.0%}" for _, label, _ in COMPONENTS
                        if by_comp.get(label)) if grand else "no priced components"
        results = [row(None, None, total, ids, note=" ".join([f"Mix: {mix}."] + ledger.notes()))]
        results += [row("component", label, by_comp.get(label, {}), ids) for _, label, _ in COMPONENTS]
        results += [row("stage", st, amounts, ids, note="Averaged over every document")
                    for st, amounts in sorted(by_stage.items())]
        for dim in ("document_type", "segment", "processing_mode"):
            groups = defaultdict(list)
            for i, d in ledger.documents.items():
                groups[_key(getattr(d, dim))].append(i)
            results += [row(dim, v, total, g) for v, g in sorted(groups.items())]
        return MeasureOutput(self.id, "measured", results)


class CostPerPage(Measure):
    id = "cost_per_page"
    tag = "Cost"
    name = "Cost per page"
    question = "What does one page cost, fully loaded?"
    unit = "usd"
    higher_is_better = False
    dimensions = ("document_type", "segment")

    def compute(self, source, window: Window) -> MeasureOutput:
        ledger, why = _ledger_or_reason(self.id, source, window)
        if why:
            return why
        paged = {i: d for i, d in ledger.documents.items() if d.page_count}
        if not paged:
            return unmeasured(self.id, "No document records a page count.")
        usd = defaultdict(float)
        for ln in ledger.lines:
            usd[ln.document_id] += ln.usd

        def one(dim, val, ids):
            pages = sum(paged[i].page_count for i in ids)
            value = sum(usd[i] for i in ids) / pages
            # Ratio-estimator standard error: spread of each document's residual from the ratio.
            n = len(ids)
            se = None
            if n > 1:
                mean_pages = pages / n
                resid = [usd[i] - value * paged[i].page_count for i in ids]
                se = (sum(r * r for r in resid) / (n - 1) / n) ** 0.5 / mean_pages
            return SliceResult(dim, val, value, n, sum(usd[i] for i in ids), pages,
                               note=f"{pages:,} pages", stderr=se)

        results = [one(None, None, list(paged))]
        for dim in self.dimensions:
            groups = defaultdict(list)
            for i, d in paged.items():
                groups[_key(getattr(d, dim))].append(i)
            results += [one(dim, v, ids) for v, ids in sorted(groups.items())]
        return MeasureOutput(self.id, "measured", results)


class TotalSpend(Measure):
    id = "total_spend"
    tag = "Cost"
    name = "Total spend"
    question = "How much did processing cost in this window, and on what?"
    unit = "usd"
    higher_is_better = None  # grows with volume
    anomaly_alerts = False  # reporting measure: cost per document is what alerts
    dimensions = ("component", "model_served", "stage", "prompt", "document_type", "segment")

    def compute(self, source, window: Window) -> MeasureOutput:
        ledger, why = _ledger_or_reason(self.id, source, window)
        if why:
            return why
        results: List[SliceResult] = []
        total = sum(ln.usd for ln in ledger.lines)
        note = f"{len(ledger.documents):,} documents. " + " ".join(ledger.notes())
        results.append(SliceResult(None, None, total, len(ledger.documents), note=note))
        for dim in self.dimensions:
            usd, docs = defaultdict(float), defaultdict(set)
            for ln in ledger.lines:
                v = COMPONENT_LABEL[ln.component] if dim == "component" else _key(getattr(ln, dim))
                usd[v] += ln.usd
                docs[v].add(ln.document_id)
            results += [SliceResult(dim, v, usd[v], len(docs[v])) for v in sorted(usd)]
        return MeasureOutput(self.id, "measured", results)


class HumanTouchRate(Measure):
    id = "human_touch_rate"
    tag = "Cost"
    name = "Human touch rate"
    question = "What share of documents needed a person to review or fix them?"
    higher_is_better = False
    dimensions = ("document_type", "segment", "processing_mode")

    def compute(self, source, window: Window) -> MeasureOutput:
        reviews = source.reviews(window) if hasattr(source, "reviews") else None
        if reviews is None:
            return unmeasured(self.id, "Review time isn't recorded by this source.")
        docs = source.documents(window)
        if docs is None:
            return unmeasured(self.id, "Source does not provide documents.")
        touched = {r.document_id for r in reviews}
        return ratio_by_slice(self.id, docs, hit=lambda d: d.document_id in touched, dimensions=self.dimensions)
