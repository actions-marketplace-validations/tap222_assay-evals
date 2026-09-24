"""HTTP API and dashboard."""
from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, desc, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from assay import alerts, cost, coverage, gates, runner, store, trace
from assay.config import Settings
from assay.measures import GROUPS, REGISTRY
from assay.scheduler import Scheduler

STATIC = Path(__file__).parent / "static"


# ---------- request bodies ----------

class CallEvent(BaseModel):
    call_id: str
    stage: str
    ts: datetime
    document_id: Optional[str] = None
    model_declared: Optional[str] = None
    model_served: Optional[str] = None
    resolving_layer: Optional[str] = None
    gate_reason: Optional[str] = None
    cost_usd: Optional[float] = None
    code_revision: Optional[str] = None
    segment: Optional[str] = None
    document_type: Optional[str] = None
    latency_ms: Optional[float] = None
    status: Optional[str] = None


class DocumentEvent(BaseModel):
    document_id: str
    received_at: datetime
    completed_at: Optional[datetime] = None
    status: Optional[str] = None
    processing_mode: Optional[str] = None
    file_hash: Optional[str] = None
    segment: Optional[str] = None
    document_type: Optional[str] = None
    delivered_downstream: Optional[bool] = None
    page_count: Optional[int] = None


class StageRunEvent(BaseModel):
    document_id: str
    stage: str
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    did_work: Optional[bool] = None


class IndexedEvent(BaseModel):
    document_id: str
    has_positions: bool
    segment: Optional[str] = None
    document_type: Optional[str] = None


class ReviewEvent(BaseModel):
    review_id: str
    document_id: str
    ts: datetime
    kind: str = Field("review", description="review, or rework for fixing an error")
    minutes: Optional[float] = Field(None, description="Priced at the rate card's hourly rate")
    cost_usd: Optional[float] = Field(None, description="Use instead of minutes if you know the cost")
    reviewer: Optional[str] = None
    stage: Optional[str] = None


class RateIn(BaseModel):
    source: str = Field("*", description='A source name, or "*" for every source')
    rates: Dict[str, Optional[float]] = Field(..., description="Rate key → value; null removes it")


class BackfillRequest(BaseModel):
    source: str
    days: int = Field(30, ge=1, le=180, description="How many past days to replay, one run per day")
    window_days: float = Field(1.0, gt=0, le=30)


class RunRequest(BaseModel):
    source: str = Field(..., examples=["sql", "events:acme"])
    days: int = Field(7, ge=1, le=365)
    measures: Optional[List[str]] = None


class RuleIn(BaseModel):
    measure_id: str
    higher_is_better: bool = True
    tolerance: float = 0.01
    min_n: int = 30
    severity: str = "normal"


class GateRequest(BaseModel):
    lineage: Dict[str, str] = Field(..., description="prompt, schema, model, provider, preprocessor, build, corpus versions")
    rules: List[RuleIn]
    samples: Dict[str, Dict[str, Dict[str, List[float]]]]
    identical_runs: Dict[str, List[List[float]]] = Field(
        default_factory=dict, description="Per measure: repeated baseline runs used to measure the noise floor")
    required_slices: Dict[str, List[str]] = Field(default_factory=dict)


class SLOIn(BaseModel):
    source: str = Field("*", description='A source name, or "*" for every source')
    measure_id: str
    dimension: Optional[str] = Field(None, description="Omit for the overall value")
    slice_value: Optional[str] = Field(None, description="Omit to require every slice of the dimension")
    target: float
    note: Optional[str] = None


REQUIRED_LINEAGE = ("prompt", "model", "build", "corpus")
GROUP_OF = {mid: g for g, ids in GROUPS.items() for mid in ids}


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = store.make_engine(settings.store_url)
    scheduler = Scheduler(engine, settings)

    @asynccontextmanager
    async def lifespan(_app):
        scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(title="Assay", version="0.2.0", lifespan=lifespan,
                  description="Evaluation and observability for document-intelligence pipelines.")

    if settings.auto_demo:
        demo_lock, demo_state = threading.Lock(), {"ready": False}

        @app.middleware("http")
        async def ensure_demo(request: Request, call_next):
            # Serverless hosts start with an empty /tmp; load the demo once per instance.
            if not demo_state["ready"]:
                with demo_lock:
                    if not demo_state["ready"]:
                        with engine.connect() as conn:
                            has_runs = conn.execute(select(store.measure_runs.c.id).limit(1)).first()
                        if not has_runs:
                            from assay.demo import seed
                            seed(engine, days=42, docs_per_day=90)
                        demo_state["ready"] = True
            return await call_next(request)

    def auth(x_api_key: Optional[str] = Header(None)):
        if settings.api_key and x_api_key != settings.api_key:
            raise HTTPException(401, "Missing or wrong X-API-Key header.")

    def tenant_of(x_tenant: Optional[str] = Header(None)) -> str:
        return x_tenant or "default"

    def upsert(table, rows: List[dict], key: str):
        """Insert, or update only the fields each row actually sent, so a later
        event (say, a completion) doesn't blank fields an earlier one set."""
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
                stmt = stmt.on_conflict_do_update(index_elements=[key], set_=updates) if updates \
                    else stmt.on_conflict_do_nothing(index_elements=[key])
                conn.execute(stmt, batch)
        return len(rows)

    # ---------- dashboard ----------

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    # ---------- catalog and results ----------

    @app.get("/v1/measures", dependencies=[Depends(auth)])
    def list_measures():
        return [m.describe() | {"group": GROUP_OF.get(m.id)} for m in REGISTRY.values()]

    @app.get("/v1/sources", dependencies=[Depends(auth)])
    def list_sources():
        with engine.connect() as conn:
            seen = [r[0] for r in conn.execute(select(store.measure_runs.c.source).distinct())]
            tenants = set()
            for t in (store.event_documents, store.event_calls):
                tenants |= {r[0] for r in conn.execute(select(t.c.tenant).distinct())}
        available = (["sql"] if settings.source_url else []) + sorted(f"events:{t}" for t in tenants)
        return {"configured": available, "with_results": sorted(seen)}

    @app.post("/v1/runs", dependencies=[Depends(auth)])
    def create_run(req: RunRequest):
        unknown = [m for m in (req.measures or []) if m not in REGISTRY]
        if unknown:
            raise HTTPException(422, f"Unknown measures: {', '.join(unknown)}")
        try:
            source = runner.resolve_source(req.source, engine, settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        run_id = runner.run_measures(engine, source, runner.window_for_days(req.days), req.measures,
                                     notify=settings.notifier(), alert_min_n=settings.alert_min_n,
                                     alert_after_runs=settings.alert_after_runs)
        return runner.latest_run(engine, source.name) | {"run_id": run_id}

    @app.get("/v1/runs/latest", dependencies=[Depends(auth)])
    def get_latest(source: str):
        out = runner.latest_run(engine, source)
        if not out:
            raise HTTPException(404, f"No runs yet for source '{source}'. POST /v1/runs first.")
        return out

    @app.get("/v1/measures/{measure_id}/history", dependencies=[Depends(auth)])
    def get_history(measure_id: str, source: str, dimension: Optional[str] = None,
                    slice_value: Optional[str] = None):
        if measure_id not in REGISTRY:
            raise HTTPException(404, f"Unknown measure '{measure_id}'.")
        return runner.history(engine, source, measure_id, dimension, slice_value)


    @app.get("/v1/overview", dependencies=[Depends(auth)])
    def overview(source: str):
        """Everything the landing page needs in one call."""
        latest = runner.latest_run(engine, source)
        if not latest:
            raise HTTPException(404, f"No runs yet for source '{source}'. POST /v1/runs first.")
        a = store.alerts
        with engine.connect() as conn:
            open_alerts = [_alert(r) for r in conn.execute(
                select(a).where(and_(a.c.source == source, a.c.state == "open")).order_by(desc(a.c.opened_at)))]
            slos = alerts.load_slos(conn, source)
        slo_status = []
        for s_ in slos:
            m, res = REGISTRY.get(s_["measure_id"]), latest["measures"].get(s_["measure_id"])
            if not m or m.higher_is_better is None:
                continue
            if s_["dimension"] is None:
                rows = [res["overall"]] if res and res["overall"] else []
            else:
                rows = [r for r in (res or {}).get("slices", {}).get(s_["dimension"], [])
                        if s_["slice_value"] in (None, r["slice"]) and r["value"] is not None]
            judged = [r for r in rows if r["n"] >= settings.alert_min_n]
            failing = [r["slice"] for r in judged if alerts.breaches(r["value"], s_["target"], m.higher_is_better)]
            slo_status.append({"id": s_["id"], "measure_id": m.id, "dimension": s_["dimension"],
                               "slice_value": s_["slice_value"], "target": s_["target"], "note": s_["note"],
                               "state": "unmeasured" if not judged else ("breached" if failing else "met"),
                               "value": rows[0]["value"] if s_["dimension"] is None and rows else None,
                               "failing_slices": [f for f in failing if f], "judged": len(judged)})
        counts = {"measured": 0, "unmeasured": 0}
        for m in latest["measures"].values():
            counts[m["status"]] += 1
        with engine.connect() as conn:
            run_count = len(conn.execute(select(store.measure_runs.c.id)
                                         .where(store.measure_runs.c.source == source)).all())
        return {"run": {k: latest[k] for k in ("run_id", "source", "started_at", "window")}, "run_count": run_count,
                "counts": counts, "open_alerts": open_alerts, "slos": slo_status,
                "changes": runner.changes(engine, source, settings.alert_min_n),
                "scheduler": scheduler.status()}

    @app.get("/v1/changes", dependencies=[Depends(auth)])
    def get_changes(source: str, limit: int = 12):
        return runner.changes(engine, source, settings.alert_min_n, limit)

    # ---------- alerts and SLOs ----------

    @app.get("/v1/alerts", dependencies=[Depends(auth)])
    def list_alerts(source: Optional[str] = None, state: str = "all", limit: int = 100):
        a = store.alerts
        cond = []
        if source:
            cond.append(a.c.source == source)
        if state in ("open", "resolved"):
            cond.append(a.c.state == state)
        q = select(a).order_by(desc(a.c.last_seen_at)).limit(limit)
        if cond:
            q = q.where(and_(*cond))
        with engine.connect() as conn:
            return [_alert(r) for r in conn.execute(q)]

    @app.get("/v1/slos", dependencies=[Depends(auth)])
    def list_slos():
        with engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(store.slos).order_by(store.slos.c.measure_id))]

    @app.put("/v1/slos", dependencies=[Depends(auth)])
    def put_slo(slo: SLOIn):
        m = REGISTRY.get(slo.measure_id)
        if not m:
            raise HTTPException(404, f"Unknown measure '{slo.measure_id}'.")
        if m.higher_is_better is None:
            raise HTTPException(422, f"{m.name} has no good direction, so it can't take an SLO. "
                                     "Its anomaly band still alerts on moves either way.")
        if slo.dimension and slo.dimension not in m.dimensions:
            raise HTTPException(422, f"{m.name} is sliced by {', '.join(m.dimensions)}, not {slo.dimension}.")
        t = store.slos
        scope = and_(t.c.source == slo.source, t.c.measure_id == slo.measure_id,
                     t.c.dimension.is_(None) if slo.dimension is None else t.c.dimension == slo.dimension,
                     t.c.slice_value.is_(None) if slo.slice_value is None else t.c.slice_value == slo.slice_value)
        with engine.begin() as conn:
            conn.execute(delete(t).where(scope))
            sid = conn.execute(t.insert().values(**slo.model_dump(), updated_at=datetime.utcnow())).inserted_primary_key[0]
        return {"id": sid, **slo.model_dump()}

    @app.delete("/v1/slos/{slo_id}", dependencies=[Depends(auth)])
    def delete_slo(slo_id: int):
        with engine.begin() as conn:
            gone = conn.execute(delete(store.slos).where(store.slos.c.id == slo_id)).rowcount
        if not gone:
            raise HTTPException(404, f"No SLO with id {slo_id}.")
        return {"deleted": slo_id}

    # ---------- tracing ----------

    @app.get("/v1/documents", dependencies=[Depends(auth)])
    def list_documents(source: str, view: str = "slowest", days: float = 7, segment: Optional[str] = None,
                       limit: int = 25):
        if view not in trace.VIEWS:
            raise HTTPException(422, f"view must be one of {', '.join(trace.VIEWS)}")
        try:
            src = runner.resolve_source(source, engine, settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return trace.find_documents(src, runner.window_for_days(days), view, limit, segment)

    @app.get("/v1/trace/{document_id}", dependencies=[Depends(auth)])
    def get_trace(document_id: str, source: str):
        try:
            src = runner.resolve_source(source, engine, settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        out = trace.build_trace(src, document_id)
        if out is None:
            raise HTTPException(404, f"No document '{document_id}' in {source}.")
        return out

    @app.get("/v1/cron", include_in_schema=False)
    def cron(authorization: Optional[str] = Header(None)):
        """Run every scheduled source once. For hosts without a long-running
        process (Vercel Cron). Requires CRON_SECRET."""
        if not settings.cron_secret or authorization != f"Bearer {settings.cron_secret}":
            raise HTTPException(401, "Set CRON_SECRET and send it as a Bearer token.")
        if not settings.schedule_sources:
            raise HTTPException(422, "Set ASSAY_SCHEDULE_SOURCES to the sources to run.")
        scheduler.run_once()
        return scheduler.status()

    # ---------- onboarding ----------

    @app.get("/v1/coverage", dependencies=[Depends(auth)])
    def get_coverage(source: str, days: float = 7):
        """Which measures this source can answer, and which field would unlock the rest."""
        try:
            src = runner.resolve_source(source, engine, settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return coverage.compute(src, runner.window_for_days(days), runner.load_rates(engine, source))

    @app.post("/v1/backfill", dependencies=[Depends(auth)])
    def post_backfill(req: BackfillRequest):
        """Replay past days so baselines and alerts work from day one."""
        try:
            src = runner.resolve_source(req.source, engine, settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return runner.backfill(engine, src, req.days, req.window_days, settings.alert_min_n,
                               settings.alert_after_runs)

    # ---------- cost ----------

    @app.get("/v1/cost/rates", dependencies=[Depends(auth)])
    def get_rates(source: str = "*"):
        return {"source": source, "rates": runner.load_rates(engine, source) if source != "*" else
                {r.key: r.value for r in _rate_rows("*")},
                "keys": cost.RATE_KEYS}

    def _rate_rows(src):
        with engine.connect() as conn:
            return conn.execute(select(store.cost_rates).where(store.cost_rates.c.source == src)).all()

    @app.put("/v1/cost/rates", dependencies=[Depends(auth)])
    def put_rates(body: RateIn):
        unknown = [k for k in body.rates if k not in cost.RATE_KEYS]
        if unknown:
            raise HTTPException(422, f"Unknown rate {', '.join(unknown)}. Known: {', '.join(cost.RATE_KEYS)}.")
        if any(v is not None and v < 0 for v in body.rates.values()):
            raise HTTPException(422, "Rates can't be negative.")
        t = store.cost_rates
        with engine.begin() as conn:
            for k, v in body.rates.items():
                conn.execute(delete(t).where(and_(t.c.source == body.source, t.c.key == k)))
                if v is not None:
                    conn.execute(t.insert().values(source=body.source, key=k, value=v, updated_at=datetime.utcnow()))
        return get_rates(body.source)

    @app.get("/v1/cost/breakdown", dependencies=[Depends(auth)])
    def cost_breakdown(source: str, by: str = "document_type", days: float = 7):
        """Cost per document by component for each value of `by`, computed live."""
        if by not in ("document_type", "segment", "processing_mode"):
            raise HTTPException(422, "by must be document_type, segment or processing_mode")
        try:
            src = runner.resolve_source(source, engine, settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        ledger = cost.build_ledger(src, runner.window_for_days(days), runner.load_rates(engine, source))
        if ledger is None or not ledger.documents:
            raise HTTPException(404, "No documents in this window.")
        n = len(ledger.documents)
        totals = {k: 0.0 for k, _, _ in cost.COMPONENTS}
        for ln in ledger.lines:
            totals[ln.component] += ln.usd
        return {"by": by, "days": days, "documents": n,
                "components": [{"key": k, "label": label, "group": g} for k, label, g in cost.COMPONENTS],
                "overall": {"per_document": {k: v / n for k, v in totals.items()},
                            "total_per_document": sum(totals.values()) / n, "total_usd": sum(totals.values())},
                "rows": cost.breakdown(ledger, by), "models": cost.spend_by_model(ledger),
                "coverage": ledger.coverage, "notes": ledger.notes()}

    @app.get("/v1/scheduler", dependencies=[Depends(auth)])
    def scheduler_status():
        return scheduler.status()

    # ---------- release gates ----------

    @app.post("/v1/gates/evaluate", dependencies=[Depends(auth)])
    def evaluate_gate(req: GateRequest):
        missing = [k for k in REQUIRED_LINEAGE if not req.lineage.get(k)]
        if missing:
            raise HTTPException(422, f"Lineage must record: {', '.join(missing)}. "
                                     "Every promote or rollback has to be traceable.")
        floors = {r.measure_id: gates.noise_floor(req.identical_runs.get(r.measure_id, []))
                  for r in req.rules}
        decision = gates.evaluate([gates.GateRule(**r.model_dump()) for r in req.rules],
                                  req.samples, floors, req.required_slices)
        body = decision.to_dict()
        with engine.begin() as conn:
            gid = conn.execute(store.gate_decisions.insert().values(
                created_at=datetime.utcnow(), outcome=decision.outcome,
                lineage=req.lineage, detail=body)).inserted_primary_key[0]
        return {"id": gid, "lineage": req.lineage, **body}

    @app.get("/v1/gates", dependencies=[Depends(auth)])
    def list_gates(limit: int = 20):
        g = store.gate_decisions
        with engine.connect() as conn:
            rows = conn.execute(select(g).order_by(desc(g.c.id)).limit(limit)).all()
        return [{"id": r.id, "created_at": r.created_at.isoformat(), "outcome": r.outcome,
                 "lineage": r.lineage, **r.detail} for r in rows]

    # ---------- event ingest (multi-tenant path) ----------

    @app.post("/v1/events/calls", dependencies=[Depends(auth)])
    def ingest_calls(events: List[CallEvent], tenant: str = Depends(tenant_of)):
        return {"ingested": upsert(store.event_calls,
                                   [e.model_dump() | {"tenant": tenant} for e in events], "call_id")}

    @app.post("/v1/events/documents", dependencies=[Depends(auth)])
    def ingest_documents(events: List[DocumentEvent], tenant: str = Depends(tenant_of)):
        return {"ingested": upsert(store.event_documents,
                                   [e.model_dump(exclude_unset=True) | {"tenant": tenant} for e in events],
                                   "document_id")}

    @app.post("/v1/events/stage-runs", dependencies=[Depends(auth)])
    def ingest_stage_runs(events: List[StageRunEvent], tenant: str = Depends(tenant_of)):
        with engine.begin() as conn:
            if events:
                conn.execute(store.event_stage_runs.insert(),
                             [e.model_dump() | {"tenant": tenant} for e in events])
        return {"ingested": len(events)}

    @app.post("/v1/events/reviews", dependencies=[Depends(auth)])
    def ingest_reviews(events: List[ReviewEvent], tenant: str = Depends(tenant_of)):
        bad = [e.review_id for e in events if e.kind not in ("review", "rework")]
        if bad:
            raise HTTPException(422, f"kind must be review or rework (review_id {', '.join(bad[:5])}).")
        return {"ingested": upsert(store.event_reviews,
                                   [e.model_dump() | {"tenant": tenant} for e in events], "review_id")}

    @app.post("/v1/events/indexed", dependencies=[Depends(auth)])
    def ingest_indexed(events: List[IndexedEvent], tenant: str = Depends(tenant_of)):
        with engine.begin() as conn:
            if events:
                conn.execute(store.event_indexed.insert(),
                             [e.model_dump() | {"tenant": tenant} for e in events])
        return {"ingested": len(events)}

    app.state.engine = engine
    app.state.settings = settings
    app.state.scheduler = scheduler
    return app


def _alert(r) -> dict:
    d = dict(r._mapping)
    for k in ("opened_at", "last_seen_at", "resolved_at"):
        d[k] = d[k].isoformat() if d[k] else None
    m = REGISTRY.get(d["measure_id"])
    d["measure_name"], d["unit"] = (m.name, m.unit) if m else (d["measure_id"], "ratio")
    return d
