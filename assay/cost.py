"""Fully loaded cost per document, component by component.

The price per model call is the number everyone quotes, and usually the
smallest part of what a document costs. The ledger prices every component
it can see and says plainly which ones it can't:

  AI inference         calls answered by the model they declared
  AI inference (est.)  unpriced calls, estimated from the median price of
                       priced calls to the same model at the same stage
                       (else the same model at any stage); never silent
  Fallback escalation  calls answered by a fallback tier or another model
  Human review         review minutes x hourly rate (or cost as recorded)
  Rework               correction minutes x hourly rate (or as recorded)
  Platform             per-document + per-page overhead from the rate card

Rates for what the pipeline can't price itself (people time, platform) come
from the rate card, which is stored per source and editable in the dashboard.
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from statistics import median
from typing import Dict, List, Optional

from assay.models import CallRecord, Window

COMPONENTS = [
    # key, label, group
    ("ai_inference", "AI inference", "AI"),
    ("ai_estimated", "AI inference, estimated", "AI"),
    ("fallback", "Fallback escalation", "AI"),
    ("review", "Human review", "People"),
    ("rework", "Rework", "People"),
    ("platform", "Platform", "Platform"),
]
COMPONENT_LABEL = {k: label for k, label, _ in COMPONENTS}

RATE_KEYS = {
    "review_per_hour": "Reviewer cost per hour (USD)",
    "rework_per_hour": "Rework cost per hour (USD); defaults to the review rate",
    "platform_per_document": "Platform cost per document (USD)",
    "platform_per_page": "Platform cost per page (USD)",
}

PRIMARY_LAYERS = {"primary", "tier_0", "tier0", "0", "default"}


@dataclass
class CostLine:
    document_id: Optional[str]
    component: str
    usd: float
    segment: Optional[str] = None
    document_type: Optional[str] = None
    processing_mode: Optional[str] = None
    stage: Optional[str] = None
    model_served: Optional[str] = None
    prompt: Optional[str] = None


@dataclass
class Ledger:
    lines: List[CostLine]
    documents: Dict[str, object]  # document_id -> DocumentRecord, the denominator
    coverage: Dict[str, object] = field(default_factory=dict)

    def notes(self) -> List[str]:
        """Plain statements of what the totals leave out or estimate."""
        c, out = self.coverage, []
        if c.get("calls"):
            if c["estimated_calls"]:
                out.append(f"{c['estimated_calls']:,} of {c['calls']:,} AI calls had no price and were estimated "
                           "from similar priced calls.")
            if c["unpriced_calls"]:
                out.append(f"{c['unpriced_calls']:,} AI calls have no price and nothing to estimate from, so AI "
                           "cost is a floor.")
        if not c.get("reviews_recorded"):
            out.append("Review and rework time isn't recorded, so people cost is missing, not zero.")
        elif c.get("unpriced_review_minutes"):
            out.append(f"{c['unpriced_review_minutes']:,.0f} review minutes have no hourly rate set.")
        if not c.get("platform_priced"):
            out.append("No platform rate is set.")
        return out


def is_fallback(c: CallRecord) -> bool:
    if c.resolving_layer and c.resolving_layer.strip().lower() not in PRIMARY_LAYERS:
        return True
    return bool(c.model_declared and c.model_served and c.model_declared != c.model_served)


def _estimator(calls: List[CallRecord]):
    by_stage_model, by_model = defaultdict(list), defaultdict(list)
    for c in calls:
        if c.cost_usd is not None:
            by_stage_model[(c.stage, c.model_served)].append(c.cost_usd)
            by_model[c.model_served].append(c.cost_usd)
    med_sm = {k: median(v) for k, v in by_stage_model.items()}
    med_m = {k: median(v) for k, v in by_model.items()}

    def estimate(c: CallRecord) -> Optional[float]:
        return med_sm.get((c.stage, c.model_served), med_m.get(c.model_served))

    return estimate


def build_ledger(source, window: Window, rates: Optional[Dict[str, float]] = None) -> Optional[Ledger]:
    """None if the source provides no documents (nothing to divide by)."""
    rates = rates or {}
    docs_list = source.documents(window)
    if docs_list is None:
        return None
    docs = {d.document_id: d for d in docs_list}
    calls = list(source.calls(window) or [])
    reviews = source.reviews(window) if hasattr(source, "reviews") else None
    estimate = _estimator(calls)
    lines: List[CostLine] = []
    cov = {"calls": len(calls), "priced_calls": 0, "estimated_calls": 0, "unpriced_calls": 0,
           "reviews_recorded": reviews is not None, "unpriced_review_minutes": 0.0,
           "platform_priced": bool(rates.get("platform_per_document") or rates.get("platform_per_page")),
           "unattributed_usd": 0.0}

    def line(doc_id, component, usd, **kw):
        d = docs.get(doc_id)
        if d is None:
            cov["unattributed_usd"] += usd  # spend on documents outside this window
            return
        lines.append(CostLine(doc_id, component, usd, d.segment, d.document_type, d.processing_mode, **kw))

    for c in calls:
        usd, component = c.cost_usd, "fallback" if is_fallback(c) else "ai_inference"
        if usd is None:
            usd = estimate(c)
            if usd is None:
                cov["unpriced_calls"] += 1
                continue
            cov["estimated_calls"] += 1
            if component == "ai_inference":
                component = "ai_estimated"
        else:
            cov["priced_calls"] += 1
        line(c.document_id, component, usd, stage=c.stage, model_served=c.model_served, prompt=c.prompt)

    for r in reviews or []:
        kind = "rework" if (r.kind or "").lower() == "rework" else "review"
        usd = r.cost_usd
        if usd is None and r.minutes is not None:
            rate = rates.get(f"{kind}_per_hour", rates.get("review_per_hour"))
            if rate is None:
                cov["unpriced_review_minutes"] += r.minutes
                continue
            usd = r.minutes / 60 * rate
        if usd is not None:
            line(r.document_id, kind, usd, stage=r.stage, model_served="(people)")

    per_doc, per_page = rates.get("platform_per_document"), rates.get("platform_per_page")
    for d in docs.values():
        usd = (per_doc or 0.0) + (per_page or 0.0) * (d.page_count or 0)
        if usd:
            line(d.document_id, "platform", usd, model_served="(platform)")

    reviewed = {r.document_id for r in reviews or [] if r.document_id in docs}
    cov["documents"] = len(docs)
    cov["touched_documents"] = len(reviewed) if reviews is not None else None
    return Ledger(lines, docs, cov)


def breakdown(ledger: Ledger, by: str) -> List[dict]:
    """Cost per document for each value of `by`, split by component."""
    docs_per = defaultdict(set)
    for d in ledger.documents.values():
        docs_per[getattr(d, by, None) or "(unrecorded)"].add(d.document_id)
    usd = defaultdict(lambda: defaultdict(float))
    for ln in ledger.lines:
        usd[getattr(ln, by, None) or "(unrecorded)"][ln.component] += ln.usd
    rows = []
    for value, ids in docs_per.items():
        n = len(ids)
        comps = {k: usd[value].get(k, 0.0) / n for k, _, _ in COMPONENTS}
        rows.append({"value": value, "documents": n, "per_document": comps,
                     "total_per_document": sum(comps.values()), "total_usd": sum(usd[value].values())})
    rows.sort(key=lambda r: -r["total_per_document"])
    return rows


def spend_by_model(ledger: Ledger) -> List[dict]:
    agg = defaultdict(lambda: {"usd": 0.0, "fallback_usd": 0.0, "lines": 0})
    for ln in ledger.lines:
        if ln.component in ("ai_inference", "ai_estimated", "fallback"):
            a = agg[ln.model_served or "(unrecorded)"]
            a["usd"] += ln.usd
            a["lines"] += 1
            if ln.component == "fallback":
                a["fallback_usd"] += ln.usd
    return sorted(({"model": m, **v} for m, v in agg.items()), key=lambda r: -r["usd"])
