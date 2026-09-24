"""Event ingest: validated models, idempotent writes, and OpenTelemetry mapping.

Every write is an upsert keyed by (tenant, id), so a pipeline can retry a
batch as often as it likes without creating duplicates. Records that don't
carry an id get one derived from their content (a stage run from document,
stage and start time; an extraction from document and field).
"""
from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import func
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from assay import store

MAX_BATCH = 5000  # records per request, across all types
MAX_OUTPUTS_BYTES = 64 * 1024


class Event(BaseModel):
    # Unknown fields are refused, so a typo like "documentType" fails loudly
    # instead of silently dropping data.
    model_config = ConfigDict(extra="forbid")

    @model_validator(mode="after")
    def _utc(self):
        """Store every timestamp as naive UTC, whatever offset it was sent with."""
        for name in type(self).model_fields:
            v = getattr(self, name)
            if isinstance(v, datetime) and v.tzinfo is not None:
                object.__setattr__(self, name, v.astimezone(timezone.utc).replace(tzinfo=None))
        return self


class DocumentEvent(Event):
    document_id: str = Field(..., max_length=128)
    received_at: datetime
    completed_at: Optional[datetime] = None
    status: Optional[str] = None
    processing_mode: Optional[str] = Field(None, description="e.g. realtime or batch")
    file_hash: Optional[str] = Field(None, description="Used to check delivery to the downstream system")
    segment: Optional[str] = Field(None, description="What to break failures out by: customer, region, …")
    document_type: Optional[str] = None
    page_count: Optional[int] = Field(None, ge=0)
    delivered_downstream: Optional[bool] = None


class StageRunEvent(Event):
    run_id: Optional[str] = Field(None, max_length=128,
                                  description="Omit to derive one from document_id, stage and started_at")
    document_id: str
    stage: str
    status: str = Field(..., description="success, failed, error, timeout, …")
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    did_work: Optional[bool] = Field(None, description="false if the stage reported success but did nothing")
    outputs: Optional[Dict[str, Any]] = Field(
        None, description="What the step produced, as named values (nested allowed). Long strings, e.g. "
                          "OCR text under '_text', are used as evidence. Max 64 KB.")
    sequence: Optional[int] = Field(None, ge=0, description="Step position in the pipeline; else start time orders steps")
    prompt_id: Optional[str] = Field(None, max_length=128, description="Which prompt, e.g. extract_invoice_fields")
    prompt_version: Optional[str] = Field(None, max_length=64,
                                          description="A label (v13) or content hash; see prompt_version() in the client")

    @model_validator(mode="after")
    def _outputs_size(self):
        if self.outputs is not None and len(json.dumps(self.outputs, default=str)) > MAX_OUTPUTS_BYTES:
            raise ValueError(f"outputs is larger than {MAX_OUTPUTS_BYTES // 1024} KB; send the fields, "
                             "and trim long text")
        return self


class ErrorEvent(Event):
    error_id: Optional[str] = Field(None, max_length=128,
                                    description="Omit to derive one from document, field and expected value")
    document_id: str
    field: str = Field(..., description="The output field that was wrong; dotted paths like line_items.0.total work")
    expected: Optional[str] = Field(None, description="The correct value (omit when kind is extra)")
    observed: Optional[str] = Field(None, description="What the pipeline output")
    kind: str = Field("wrong", pattern="^(wrong|missing|extra)$")
    reported_at: Optional[datetime] = None
    reporter: Optional[str] = None
    source: Optional[str] = Field(None, description="review, qa, customer, …")


class CallEvent(Event):
    call_id: str = Field(..., max_length=128)
    stage: str
    ts: datetime
    document_id: Optional[str] = None
    model_declared: Optional[str] = Field(None, description="The model the pipeline asked for")
    model_served: Optional[str] = Field(None, description="The model that actually answered")
    resolving_layer: Optional[str] = Field(None, description="primary, fallback_1, …")
    gate_reason: Optional[str] = Field(None, description="Why this layer answered")
    cost_usd: Optional[float] = Field(None, ge=0)
    code_revision: Optional[str] = None
    segment: Optional[str] = None
    document_type: Optional[str] = None
    latency_ms: Optional[float] = Field(None, ge=0)
    status: Optional[str] = None
    prompt_id: Optional[str] = Field(None, max_length=128, description="Which prompt, e.g. extract_invoice_fields")
    prompt_version: Optional[str] = Field(None, max_length=64,
                                          description="A label (v13) or content hash; see prompt_version() in the client")


class ReviewEvent(Event):
    review_id: str = Field(..., max_length=128)
    document_id: str
    ts: datetime
    kind: str = Field("review", pattern="^(review|rework)$", description="review, or rework to fix an error")
    minutes: Optional[float] = Field(None, ge=0, description="Priced at the rate card's hourly rate")
    cost_usd: Optional[float] = Field(None, ge=0, description="Use instead of minutes if you know the cost")
    reviewer: Optional[str] = None
    stage: Optional[str] = None


class ExtractionEvent(Event):
    extraction_id: Optional[str] = Field(None, max_length=128,
                                         description="Omit to derive one from document_id and field")
    document_id: str
    field: Optional[str] = Field(None, description="Field name; makes retries idempotent without an id")
    has_positions: bool = Field(..., description="True if the value carries a source location")
    segment: Optional[str] = None
    document_type: Optional[str] = None


class PromptEvent(Event):
    """Register a prompt version (from CI, when it's released). Optional: versions
    are also discovered from traffic. Registering adds the template, for diffs."""
    prompt_id: str = Field(..., max_length=128)
    version: Optional[str] = Field(None, max_length=64, description="Omit to use a hash of the template")
    template: Optional[str] = Field(None, max_length=65536)
    note: Optional[str] = Field(None, max_length=1024, description="What changed")
    author: Optional[str] = None

    @model_validator(mode="after")
    def _version(self):
        if not self.version:
            if not self.template:
                raise ValueError("Give a version, or a template to derive one from.")
            object.__setattr__(self, "version", content_version(self.template))
        return self


def content_version(template: str) -> str:
    """A stable version for prompt text: the first 12 hex digits of its SHA-256."""
    return hashlib.sha256(template.encode()).hexdigest()[:12]


class EventBatch(Event):
    documents: List[DocumentEvent] = []
    stage_runs: List[StageRunEvent] = []
    calls: List[CallEvent] = []
    reviews: List[ReviewEvent] = []
    extractions: List[ExtractionEvent] = []
    errors: List[ErrorEvent] = []
    prompts: List[PromptEvent] = []


def _derive(*parts) -> str:
    return hashlib.sha1("|".join("" if p is None else str(p) for p in parts).encode()).hexdigest()[:32]


TABLES = {
    "documents": (store.event_documents, "document_id"),
    "stage_runs": (store.event_stage_runs, "run_id"),
    "calls": (store.event_calls, "call_id"),
    "reviews": (store.event_reviews, "review_id"),
    "extractions": (store.event_indexed, "extraction_id"),
    "errors": (store.event_errors, "error_id"),
}


def _rows(kind: str, events: List[Event], tenant: str) -> List[dict]:
    out = []
    for e in events:
        # Documents update only the fields sent (a completion mustn't blank the type);
        # other records are written whole.
        r = e.model_dump(exclude_unset=True) if kind == "documents" else e.model_dump()
        if kind == "stage_runs" and not r.get("run_id"):
            r["run_id"] = _derive(r["document_id"], r["stage"], r.get("started_at"))
        if kind == "errors":
            r.setdefault("reported_at", None)
            r["reported_at"] = r["reported_at"] or datetime.utcnow()
            if not r.get("error_id"):
                r["error_id"] = _derive(r["document_id"], r["field"], r.get("expected"), r.get("kind"))
        if kind == "extractions" and not r.get("extraction_id"):
            r["extraction_id"] = _derive(r["document_id"], r["field"]) if r.get("field") else uuid.uuid4().hex
        out.append(r | {"tenant": tenant})
    return out


def upsert(engine: Engine, table, rows: List[dict], key: str) -> int:
    """Insert, or update only the columns each row carries."""
    if not rows:
        return 0
    insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    groups: Dict[tuple, List[dict]] = {}
    for r in rows:
        groups.setdefault(tuple(sorted(r)), []).append(r)
    with engine.begin() as conn:
        for cols, batch in groups.items():
            stmt = insert(table)
            updates = {c: stmt.excluded[c] for c in cols if c not in (key, "tenant")}
            conflict = ["tenant", key]
            stmt = stmt.on_conflict_do_update(index_elements=conflict, set_=updates) if updates \
                else stmt.on_conflict_do_nothing(index_elements=conflict)
            conn.execute(stmt, batch)
    return len(rows)


def write(engine: Engine, kind: str, events: List[Event], tenant: str) -> int:
    table, key = TABLES[kind]
    rows = _rows(kind, events, tenant)
    n = upsert(engine, table, rows, key)
    if kind in ("calls", "stage_runs"):
        discover_prompts(engine, rows, tenant, "ts" if kind == "calls" else "started_at")
    return n


def write_batch(engine: Engine, batch: EventBatch, tenant: str) -> Dict[str, int]:
    # Documents first, so everything else has something to attach to.
    out = {kind: write(engine, kind, getattr(batch, kind), tenant) for kind in TABLES}
    out["prompts"] = register_prompts(engine, batch.prompts, tenant)
    return out


def register_prompts(engine: Engine, prompts: List["PromptEvent"], tenant: str) -> int:
    t = store.prompt_versions
    now = datetime.utcnow()
    rows = [dict(tenant=tenant, prompt_id=p.prompt_id, version=p.version, template=p.template,
                 content_hash=hashlib.sha256(p.template.encode()).hexdigest() if p.template else None,
                 note=p.note, author=p.author, registered_at=now) for p in prompts]
    if not rows:
        return 0
    insert = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    with engine.begin() as conn:
        for r in rows:
            stmt = insert(t).values(**r)
            keep = lambda col: func.coalesce(stmt.excluded[col], t.c[col])  # don't erase what we know
            conn.execute(stmt.on_conflict_do_update(
                index_elements=["tenant", "prompt_id", "version"],
                set_={c: keep(c) for c in ("template", "content_hash", "note", "author", "registered_at")}))
    return len(rows)


def discover_prompts(engine: Engine, rows: List[dict], tenant: str, ts_field: str) -> None:
    """Add prompt versions seen in traffic to the registry, widening first/last seen."""
    seen: Dict[tuple, list] = {}
    for r in rows:
        if r.get("prompt_id") or r.get("prompt_version"):
            key = (r.get("prompt_id") or "(unnamed)", r.get("prompt_version") or "(unversioned)")
            ts = r.get(ts_field)
            span = seen.setdefault(key, [ts, ts])
            if ts is not None:
                span[0] = ts if span[0] is None else min(span[0], ts)
                span[1] = ts if span[1] is None else max(span[1], ts)
    if not seen:
        return
    t = store.prompt_versions
    sqlite = engine.dialect.name == "sqlite"
    insert = sqlite_insert if sqlite else pg_insert
    lo, hi = (func.min, func.max) if sqlite else (func.least, func.greatest)  # scalar min/max per dialect
    with engine.begin() as conn:
        for (pid, ver), (first, last) in seen.items():
            stmt = insert(t).values(tenant=tenant, prompt_id=pid, version=ver, first_seen=first, last_seen=last)
            conn.execute(stmt.on_conflict_do_update(
                index_elements=["tenant", "prompt_id", "version"],
                set_={"first_seen": lo(func.coalesce(t.c.first_seen, stmt.excluded.first_seen), stmt.excluded.first_seen),
                      "last_seen": hi(func.coalesce(t.c.last_seen, stmt.excluded.last_seen), stmt.excluded.last_seen)}))


# ---------- OpenTelemetry (OTLP/HTTP JSON) ----------

OTEL_MAPPING = """\
One trace is one document unless a span says otherwise.

Document   from each root span (no parent). id: assay.document_id or document.id
           attribute, else the trace id. received_at/completed_at from the span's
           start/end; completed only if the span didn't error. Also reads
           assay.document_type, assay.segment, assay.page_count, assay.processing_mode.
Model call from any span with a gen_ai.* attribute. model_declared =
           gen_ai.request.model, model_served = gen_ai.response.model (else the
           request model), latency from the span, status error if the span errored.
           stage = assay.stage, else the parent span's assay.stage or name, else the span name.
           Optional: assay.cost_usd, assay.resolving_layer, assay.gate_reason.
Stage run  from any other span with an assay.stage attribute. Optional assay.did_work,
           assay.sequence (step position), and assay.output.<field> attributes
           for what the step produced (used to localize errors).
Prompts    assay.prompt_id / assay.prompt_version on a model-call span or its
           parent stage span.
code_revision comes from the resource's service.version.
"""


def _attr_value(v: Dict[str, Any]):
    for k in ("stringValue", "boolValue", "doubleValue"):
        if k in v:
            return v[k]
    if "intValue" in v:
        return int(v["intValue"])
    return None


def _attrs(items) -> Dict[str, Any]:
    return {a.get("key"): _attr_value(a.get("value") or {}) for a in items or []}


def _str(v) -> Optional[str]:
    return None if v is None else str(v)


def _ts(nanos) -> Optional[datetime]:
    return datetime.utcfromtimestamp(int(nanos) / 1e9) if nanos else None


def from_otlp(payload: Dict[str, Any]) -> EventBatch:
    """Map an OTLP ExportTraceServiceRequest (JSON) onto Assay records."""
    spans_by_id, trace_doc = {}, {}
    collected = []
    for rs in payload.get("resourceSpans") or []:
        res = _attrs((rs.get("resource") or {}).get("attributes"))
        for ss in rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []:
            for sp in ss.get("spans") or []:
                a = _attrs(sp.get("attributes"))
                collected.append((sp, a, res))
                spans_by_id[sp.get("spanId")] = (sp, a)
                doc = a.get("assay.document_id") or a.get("document.id")
                if doc:
                    trace_doc[sp.get("traceId")] = str(doc)

    def doc_of(sp, a):
        d = a.get("assay.document_id") or a.get("document.id")
        return str(d) if d else trace_doc.get(sp.get("traceId"), sp.get("traceId"))

    batch = EventBatch()
    for sp, a, res in collected:
        errored = (sp.get("status") or {}).get("code") in (2, "STATUS_CODE_ERROR")
        start, end = _ts(sp.get("startTimeUnixNano")), _ts(sp.get("endTimeUnixNano"))
        doc_id = doc_of(sp, a)
        parent = spans_by_id.get(sp.get("parentSpanId"))
        if not sp.get("parentSpanId"):
            batch.documents.append(DocumentEvent(
                document_id=doc_id, received_at=start or datetime.utcnow(),
                completed_at=None if errored else end, status="failed" if errored else "completed",
                document_type=a.get("assay.document_type"), segment=a.get("assay.segment"),
                page_count=a.get("assay.page_count"), processing_mode=a.get("assay.processing_mode")))
        if any(k.startswith("gen_ai.") for k in a):
            stage = (a.get("assay.stage") or (parent[1].get("assay.stage") or parent[0].get("name") if parent else None)
                     or sp.get("name"))
            batch.calls.append(CallEvent(
                call_id=sp.get("spanId") or uuid.uuid4().hex, stage=str(stage), ts=start or datetime.utcnow(),
                document_id=doc_id, model_declared=a.get("gen_ai.request.model"),
                model_served=a.get("gen_ai.response.model") or a.get("gen_ai.request.model"),
                latency_ms=(end - start).total_seconds() * 1000 if start and end else None,
                cost_usd=a.get("assay.cost_usd"), resolving_layer=a.get("assay.resolving_layer"),
                gate_reason=a.get("assay.gate_reason"), code_revision=res.get("service.version"),
                status="error" if errored else "success",
                segment=a.get("assay.segment"), document_type=a.get("assay.document_type"),
                prompt_id=_str(a.get("assay.prompt_id") or (parent[1].get("assay.prompt_id") if parent else None)),
                prompt_version=_str(a.get("assay.prompt_version")
                                    or (parent[1].get("assay.prompt_version") if parent else None))))
        elif a.get("assay.stage"):
            outputs = {k[len("assay.output."):]: v for k, v in a.items() if k.startswith("assay.output.")}
            batch.stage_runs.append(StageRunEvent(
                run_id=sp.get("spanId"), document_id=doc_id, stage=str(a["assay.stage"]),
                status="failed" if errored else "success", started_at=start, finished_at=end,
                did_work=a.get("assay.did_work"), outputs=outputs or None, sequence=a.get("assay.sequence"),
                prompt_id=_str(a.get("assay.prompt_id")), prompt_version=_str(a.get("assay.prompt_version"))))
    return batch
