"""Per-document traces: from an aggregate that moved to the documents behind it."""
from __future__ import annotations

from typing import List, Optional

from assay.models import FAILED_STATUSES, DocumentRecord, Window

VIEWS = ("slowest", "incomplete", "lost", "recent")


def _iso(v):
    return v.isoformat() if v else None


def _duration(d: DocumentRecord) -> Optional[float]:
    return (d.completed_at - d.received_at).total_seconds() if d.completed_at else None


def build_trace(source, document_id: str) -> Optional[dict]:
    detail = source.document_detail(document_id)
    if detail is None:
        return None
    doc, runs, calls = detail
    downstream = source.downstream_hashes()
    flags: List[dict] = []

    def flag(severity: str, text: str):
        flags.append({"severity": severity, "text": text})

    if doc.completed_at is None:
        flag("warn", "Never recorded a completion.")
    elif downstream is not None and doc.file_hash and doc.file_hash not in downstream:
        flag("bad", "Finished here but has no record downstream (joined on file_hash).")
    for r in runs:
        if r.status in FAILED_STATUSES:
            flag("bad", f"Stage {r.stage} failed ({r.status}).")
        elif r.did_work is False:
            flag("warn", f"Stage {r.stage} reported success without doing any work.")
    for c in calls:
        if c.status in FAILED_STATUSES:
            flag("bad", f"{c.stage} call {c.call_id} returned {c.status}.")
        if c.model_declared and c.model_served and c.model_declared != c.model_served:
            flag("warn", f"{c.stage} declared {c.model_declared} but was served by {c.model_served}.")
        if c.cost_usd is None:
            flag("info", f"{c.stage} call has no recorded cost.")
        if not (c.resolving_layer and c.gate_reason):
            flag("info", f"{c.stage} call doesn't record which tier answered or why.")
    # Collapse repeated info flags into one line each.
    seen, unique = set(), []
    for f in flags:
        if f["text"] not in seen:
            seen.add(f["text"])
            unique.append(f)

    return {
        "document": {
            "document_id": doc.document_id, "received_at": _iso(doc.received_at),
            "completed_at": _iso(doc.completed_at), "duration_s": _duration(doc), "status": doc.status,
            "processing_mode": doc.processing_mode, "segment": doc.segment,
            "document_type": doc.document_type, "file_hash": doc.file_hash,
            "delivered_downstream": None if downstream is None or not doc.file_hash else doc.file_hash in downstream,
        },
        "stages": [{"stage": r.stage, "status": r.status, "started_at": _iso(r.started_at),
                    "finished_at": _iso(r.finished_at), "did_work": r.did_work}
                   for r in sorted(runs, key=lambda r: (r.started_at is None, r.started_at))],
        "calls": [{"call_id": c.call_id, "stage": c.stage, "ts": _iso(c.ts), "model_declared": c.model_declared,
                   "model_served": c.model_served, "resolving_layer": c.resolving_layer,
                   "gate_reason": c.gate_reason, "cost_usd": c.cost_usd, "latency_ms": c.latency_ms,
                   "status": c.status} for c in sorted(calls, key=lambda c: c.ts)],
        "cost_usd": sum(c.cost_usd for c in calls if c.cost_usd is not None),
        "flags": unique,
    }


def find_documents(source, window: Window, view: str, limit: int = 25,
                   segment: Optional[str] = None) -> List[dict]:
    docs = source.documents(window) or []
    if segment:
        docs = [d for d in docs if (d.segment or "(unrecorded)") == segment]
    if view == "slowest":
        picked = sorted((d for d in docs if d.completed_at), key=_duration, reverse=True)
    elif view == "incomplete":
        picked = sorted((d for d in docs if not d.completed_at), key=lambda d: d.received_at)
    elif view == "lost":
        down = source.downstream_hashes()
        picked = [] if down is None else sorted(
            (d for d in docs if d.completed_at and d.file_hash and d.file_hash not in down),
            key=lambda d: d.completed_at, reverse=True)
    else:
        picked = sorted(docs, key=lambda d: d.received_at, reverse=True)
    return [{"document_id": d.document_id, "received_at": _iso(d.received_at),
             "completed_at": _iso(d.completed_at), "duration_s": _duration(d),
             "segment": d.segment, "document_type": d.document_type,
             "processing_mode": d.processing_mode} for d in picked[:limit]]
