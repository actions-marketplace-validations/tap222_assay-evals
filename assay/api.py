"""HTTP API and dashboard.

Authenticate with `Authorization: Bearer <key>` (or `X-API-Key: <key>`).
See assay/auth.py for scopes and tenant isolation.
"""
# No `from __future__ import annotations` here: the per-record-type ingest
# endpoints are built in a loop, and FastAPI needs their body types as real
# objects rather than strings to resolve later.
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field
from sqlalchemy import and_, delete, desc, or_, select

from assay import alerts, auth, cost, coverage, gates, ingest, prompts, rootcause, runner, store, trace
from assay.auth import Principal
from assay.config import Settings
from assay.ingest import (CallEvent, DocumentEvent, ErrorEvent, EventBatch, ExtractionEvent, ReviewEvent,
                          StageRunEvent)
from assay.measures import GROUPS, REGISTRY
from assay.scheduler import Scheduler

STATIC = Path(__file__).parent / "static"


# ---------- request bodies ----------

class RateIn(BaseModel):
    source: Optional[str] = Field(None, description='Defaults to your own source (all sources for a platform key); "*" needs a platform key')
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
    source: Optional[str] = Field(None, description='Defaults to your own source; "*" needs a platform key')
    measure_id: str
    dimension: Optional[str] = Field(None, description="Omit for the overall value")
    slice_value: Optional[str] = Field(None, description="Omit to require every slice of the dimension")
    target: float
    note: Optional[str] = None


class KeyIn(BaseModel):
    name: str = Field(..., max_length=128, description="What uses it, e.g. 'invoice pipeline (prod)'")
    tenant: Optional[str] = Field(None, description="Defaults to your own tenant; '*' needs a platform key")
    scopes: List[str] = Field(..., examples=[["ingest"], ["read"], ["manage"]])
    expires_in_days: Optional[int] = Field(None, ge=1, le=3650)


REQUIRED_LINEAGE = ("prompt", "model", "build", "corpus")
GROUP_OF = {mid: g for g, ids in GROUPS.items() for mid in ids}

TAGS = [
    {"name": "ingest", "description": "Send pipeline events. Scope: `ingest`. Every write is idempotent by id."},
    {"name": "results", "description": "Measures, runs, alerts, traces, cost. Scope: `read`."},
    {"name": "operate", "description": "Runs, backfill, SLOs, rates and release gates. Scope: `manage`."},
    {"name": "keys", "description": "Create and revoke API keys. Scope: `admin`."},
]


def create_app(settings: Optional[Settings] = None) -> FastAPI:
    settings = settings or Settings.from_env()
    engine = store.make_engine(settings.store_url)
    scheduler = Scheduler(engine, settings)
    authn = auth.Authenticator(engine, settings.admin_key, settings.auth_mode)
    limiter = auth.RateLimiter(settings.rate_limit_per_min)

    @asynccontextmanager
    async def lifespan(_app):
        scheduler.start()
        yield
        scheduler.stop()

    app = FastAPI(
        title="Assay API", version="1.0.0", lifespan=lifespan, openapi_tags=TAGS,
        description=(
            "Evaluation and observability for document-intelligence pipelines.\n\n"
            "**Authentication:** send `Authorization: Bearer <key>` (or `X-API-Key`). Keys belong to one "
            "tenant and carry scopes: `ingest`, `read`, `manage` (includes read), `admin` (everything). "
            "A tenant key can only see and write its own tenant (source `events:<tenant>`).\n\n"
            "**Connecting:** send events to `POST /v1/events` (all record types in one request, up to "
            f"{ingest.MAX_BATCH} records), or point an OpenTelemetry collector at `/v1/otlp` "
            "(OTLP/HTTP, JSON encoding). Writes are upserts by id, so retrying a batch is always safe."))

    if settings.cors_origins:
        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                           allow_headers=["Authorization", "X-API-Key", "X-Tenant", "Content-Type"])

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

    # ---------- auth ----------

    bearer = HTTPBearer(auto_error=False, description="Assay API key")
    header_key = APIKeyHeader(name="X-API-Key", auto_error=False, description="Assay API key (alternative header)")

    def principal(request: Request, cred: Optional[HTTPAuthorizationCredentials] = Security(bearer),
                  key: Optional[str] = Security(header_key)) -> Principal:
        token = cred.credentials if cred else key
        p = authn.authenticate(token)
        if p is None:
            raise HTTPException(401, "Missing, invalid, revoked or expired API key. Send it as "
                                     "'Authorization: Bearer <key>'.", headers={"WWW-Authenticate": "Bearer"})
        who = f"key:{p.key_id}" if p.key_id else f"{p.mode}:{request.client.host if request.client else '?'}"
        retry = limiter.check(who)
        if retry:
            raise HTTPException(429, "Rate limit reached for this key; slow down or ask for a higher limit.",
                                headers={"Retry-After": str(retry)})
        return p

    def require(*scopes: str):
        """Any one of `scopes` is enough."""
        def dep(p: Principal = Depends(principal)) -> Principal:
            if not any(p.can(s) for s in scopes):
                raise HTTPException(403, f"This key needs the '{' or '.join(scopes)}' scope "
                                         f"(it has: {', '.join(sorted(p.scopes))}).")
            return p
        return dep

    def check_source(p: Principal, source: str) -> None:
        if not p.can_source(source):
            raise HTTPException(403, f"This key can only access events:{p.tenant}.")

    def own_source(p: Principal, source: Optional[str]) -> str:
        """Resolve a settings scope: default to the key's own source; '*' only for platform keys."""
        if source in (None, ""):
            return "*" if p.platform else f"events:{p.tenant}"
        if source == "*" and not p.platform:
            raise HTTPException(403, "Only a platform key can change settings for every source.")
        if source != "*":
            check_source(p, source)
        return source

    def resolve(p: Principal, source: str):
        check_source(p, source)
        try:
            return runner.resolve_source(source, engine, settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    def tenant_for(p: Principal, x_tenant: Optional[str]) -> str:
        try:
            return p.write_tenant(x_tenant)
        except PermissionError as exc:
            raise HTTPException(403, str(exc))

    # ---------- dashboard and health ----------

    @app.get("/", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "index.html")

    @app.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True}

    @app.get("/v1/whoami", tags=["results"], summary="The key you're using, and whether auth is on")
    def whoami(p: Principal = Depends(principal)):
        return p.public() | {"auth_required": authn.required()}

    # ---------- catalog and results ----------

    @app.get("/v1/measures", tags=["results"])
    def list_measures(p: Principal = Depends(require("read"))):
        return [m.describe() | {"group": GROUP_OF.get(m.id)} for m in REGISTRY.values()]

    @app.get("/v1/sources", tags=["results"])
    def list_sources(p: Principal = Depends(require("read"))):
        with engine.connect() as conn:
            seen = [r[0] for r in conn.execute(select(store.measure_runs.c.source).distinct())]
            tenants = set()
            for t in (store.event_documents, store.event_calls):
                tenants |= {r[0] for r in conn.execute(select(t.c.tenant).distinct())}
        available = (["sql"] if settings.source_url else []) + sorted(f"events:{t}" for t in tenants)
        if not p.platform:
            available, seen = [f"events:{p.tenant}"], [s for s in seen if s == f"events:{p.tenant}"]
        return {"configured": available, "with_results": sorted(seen)}

    @app.get("/v1/runs/latest", tags=["results"])
    def get_latest(source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        out = runner.latest_run(engine, source)
        if not out:
            raise HTTPException(404, f"No runs yet for source '{source}'. POST /v1/runs or /v1/backfill first.")
        return out

    @app.get("/v1/measures/{measure_id}/history", tags=["results"])
    def get_history(measure_id: str, source: str, dimension: Optional[str] = None,
                    slice_value: Optional[str] = None, p: Principal = Depends(require("read"))):
        check_source(p, source)
        if measure_id not in REGISTRY:
            raise HTTPException(404, f"Unknown measure '{measure_id}'.")
        out = runner.history(engine, source, measure_id, dimension, slice_value)
        if out["points"]:  # prompt versions that went live in this range: chart markers
            start = datetime.fromisoformat(out["points"][0]["at"]) - timedelta(days=1)
            end = datetime.fromisoformat(out["points"][-1]["at"])
            out["changes"] = prompts.changes_between(engine, prompts.registry_tenant(source), start, end)
        else:
            out["changes"] = []
        return out

    @app.get("/v1/overview", tags=["results"], summary="Everything the landing page needs in one call")
    def overview(source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        latest = runner.latest_run(engine, source)
        if not latest:
            raise HTTPException(404, f"No runs yet for source '{source}'. POST /v1/runs or /v1/backfill first.")
        a = store.alerts
        with engine.connect() as conn:
            open_alerts = [_alert(r) for r in conn.execute(
                select(a).where(and_(a.c.source == source, a.c.state == "open")).order_by(desc(a.c.opened_at)))]
            slos = alerts.load_slos(conn, source)
            run_count = len(conn.execute(select(store.measure_runs.c.id)
                                         .where(store.measure_runs.c.source == source)).all())
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
        return {"run": {k: latest[k] for k in ("run_id", "source", "started_at", "window")}, "run_count": run_count,
                "counts": counts, "open_alerts": open_alerts, "slos": slo_status,
                "changes": runner.changes(engine, source, settings.alert_min_n),
                "scheduler": scheduler.status() if p.platform else {"enabled": scheduler.enabled}}

    @app.get("/v1/changes", tags=["results"])
    def get_changes(source: str, limit: int = 12, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return runner.changes(engine, source, settings.alert_min_n, limit)

    @app.get("/v1/alerts", tags=["results"])
    def list_alerts(source: Optional[str] = None, state: str = "all", limit: int = 100,
                    p: Principal = Depends(require("read"))):
        a = store.alerts
        cond = []
        if source:
            check_source(p, source)
            cond.append(a.c.source == source)
        elif not p.platform:
            cond.append(a.c.source == f"events:{p.tenant}")
        if state in ("open", "resolved", "pending"):
            cond.append(a.c.state == state)
        q = select(a).order_by(desc(a.c.last_seen_at)).limit(min(int(limit), 1000))
        if cond:
            q = q.where(and_(*cond))
        with engine.connect() as conn:
            return [_alert(r) for r in conn.execute(q)]

    @app.get("/v1/documents", tags=["results"], summary="Documents to investigate: slowest, stuck, lost, recent")
    def list_documents(source: str, view: str = "slowest", days: float = 7, segment: Optional[str] = None,
                       limit: int = 25, p: Principal = Depends(require("read"))):
        if view not in trace.VIEWS:
            raise HTTPException(422, f"view must be one of {', '.join(trace.VIEWS)}")
        return trace.find_documents(resolve(p, source), runner.window_for_days(days), view, min(limit, 500), segment)

    @app.get("/v1/trace/{document_id}", tags=["results"], summary="One document through every stage and call")
    def get_trace(document_id: str, source: str, p: Principal = Depends(require("read"))):
        out = trace.build_trace(resolve(p, source), document_id)
        if out is None:
            raise HTTPException(404, f"No document '{document_id}' in {source}.")
        return out

    @app.get("/v1/errors", tags=["results"], summary="Where reported errors come from, across documents")
    def error_summary(source: str, days: float = 30, p: Principal = Depends(require("read"))):
        out = rootcause.summarize(resolve(p, source), runner.window_for_days(days))
        if out is None:
            raise HTTPException(404, "No error reports from this source yet. Report one with POST /v1/errors.")
        return out

    @app.get("/v1/errors/{document_id}", tags=["results"],
             summary="One document: every step's values, and where each reported error started")
    def error_detail(document_id: str, source: str, p: Principal = Depends(require("read"))):
        out = rootcause.analyze_document(resolve(p, source), document_id)
        if out is None:
            raise HTTPException(404, f"No document '{document_id}' in {source}.")
        d = out["document"]
        return out | {"document": {"document_id": d.document_id, "document_type": d.document_type,
                                   "segment": d.segment, "received_at": d.received_at.isoformat() if d.received_at else None}}

    @app.post("/v1/errors", tags=["ingest"], status_code=201,
              summary="Report a wrong output value; returns where it went wrong")
    def report_error(body: ErrorEvent, x_tenant: Optional[str] = Header(None),
                     p: Principal = Depends(require("ingest", "manage"))):
        tenant = tenant_for(p, x_tenant)
        ingest.write(engine, "errors", [body], tenant)
        src = runner.resolve_source(f"events:{tenant}", engine, settings)
        out = rootcause.analyze_document(src, body.document_id)
        if out is None:
            return {"recorded": True, "localized": None,
                    "detail": f"Recorded, but document '{body.document_id}' isn't known yet, so it can't be traced."}
        mine = [e for e in out["errors"] if e["field"] == body.field]
        return {"recorded": True, "localized": mine[-1] if mine else None}

    @app.get("/v1/coverage", tags=["results"], summary="Which measures your data can answer, and what unlocks the rest")
    def get_coverage(source: str, days: float = 7, p: Principal = Depends(require("read"))):
        return coverage.compute(resolve(p, source), runner.window_for_days(days), runner.load_rates(engine, source))

    @app.get("/v1/cost/breakdown", tags=["results"], summary="Cost per document by component, per category")
    def cost_breakdown(source: str, by: str = "document_type", days: float = 7,
                       p: Principal = Depends(require("read"))):
        if by not in ("document_type", "segment", "processing_mode"):
            raise HTTPException(422, "by must be document_type, segment or processing_mode")
        ledger = cost.build_ledger(resolve(p, source), runner.window_for_days(days), runner.load_rates(engine, source))
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

    @app.get("/v1/cost/rates", tags=["results"])
    def get_rates(source: Optional[str] = None, p: Principal = Depends(require("read"))):
        src = own_source(p, source)
        if src == "*":
            with engine.connect() as conn:
                rates = {r.key: r.value for r in conn.execute(
                    select(store.cost_rates).where(store.cost_rates.c.source == "*"))}
        else:
            rates = runner.load_rates(engine, src)
        return {"source": src, "rates": rates, "keys": cost.RATE_KEYS}

    @app.get("/v1/slos", tags=["results"])
    def list_slos(p: Principal = Depends(require("read"))):
        t = store.slos
        q = select(t).order_by(t.c.measure_id)
        if not p.platform:
            q = q.where(or_(t.c.source == f"events:{p.tenant}", t.c.source == "*"))
        with engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(q)]

    @app.get("/v1/gates", tags=["results"])
    def list_gates(limit: int = 20, p: Principal = Depends(require("read"))):
        g = store.gate_decisions
        q = select(g).order_by(desc(g.c.id)).limit(min(limit, 500))
        if not p.platform:
            q = q.where(g.c.tenant == p.tenant)
        with engine.connect() as conn:
            rows = conn.execute(q).all()
        return [{"id": r.id, "created_at": r.created_at.isoformat(), "outcome": r.outcome,
                 "lineage": r.lineage, **r.detail} for r in rows]

    # ---------- prompts ----------

    @app.get("/v1/prompts", tags=["results"], summary="Every prompt version: what it did, and vs the one before")
    def list_prompts(source: str, days: float = 30, p: Principal = Depends(require("read"))):
        src = runner.CachedSource(resolve(p, source))
        window = runner.window_for_days(days)
        summary = rootcause.summarize(src, window, include_all=True) if hasattr(src, "errors") else None
        return prompts.analyze(src, window, engine, summary)

    @app.get("/v1/prompts/{prompt_id}/diff", tags=["results"], summary="What changed between two versions")
    def prompt_diff(prompt_id: str, a: str, b: str, source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        out = prompts.diff(engine, prompts.registry_tenant(source), prompt_id, a, b)
        if out is None:
            raise HTTPException(404, f"{prompt_id}@{a} or @{b} isn't in the registry.")
        return out

    @app.post("/v1/prompts", tags=["ingest"], status_code=201,
              summary="Register a prompt version (from CI, on release): template and what changed")
    def register_prompt(body: ingest.PromptEvent, source: Optional[str] = None, x_tenant: Optional[str] = Header(None),
                        p: Principal = Depends(require("ingest", "manage"))):
        tenant = prompts.registry_tenant(source) if source and p.platform else tenant_for(p, x_tenant)
        ingest.register_prompts(engine, [body], tenant)
        return {"prompt_id": body.prompt_id, "version": body.version, "tenant": tenant}

    # ---------- operate ----------

    @app.post("/v1/runs", tags=["operate"], summary="Compute every measure now")
    def create_run(req: RunRequest, p: Principal = Depends(require("manage"))):
        unknown = [m for m in (req.measures or []) if m not in REGISTRY]
        if unknown:
            raise HTTPException(422, f"Unknown measures: {', '.join(unknown)}")
        source = resolve(p, req.source)
        run_id = runner.run_measures(engine, source, runner.window_for_days(req.days), req.measures,
                                     notify=settings.notifier(), alert_min_n=settings.alert_min_n,
                                     alert_after_runs=settings.alert_after_runs)
        return runner.latest_run(engine, source.name) | {"run_id": run_id}

    @app.post("/v1/backfill", tags=["operate"], summary="Replay past days so baselines work from day one")
    def post_backfill(req: BackfillRequest, p: Principal = Depends(require("manage"))):
        return runner.backfill(engine, resolve(p, req.source), req.days, req.window_days, settings.alert_min_n,
                               settings.alert_after_runs)

    @app.put("/v1/slos", tags=["operate"])
    def put_slo(slo: SLOIn, p: Principal = Depends(require("manage"))):
        source = own_source(p, slo.source)
        m = REGISTRY.get(slo.measure_id)
        if not m:
            raise HTTPException(404, f"Unknown measure '{slo.measure_id}'.")
        if m.higher_is_better is None:
            raise HTTPException(422, f"{m.name} has no good direction, so it can't take an SLO. "
                                     "Its anomaly band still alerts on moves either way.")
        if slo.dimension and slo.dimension not in m.dimensions:
            raise HTTPException(422, f"{m.name} is sliced by {', '.join(m.dimensions)}, not {slo.dimension}.")
        t = store.slos
        body = slo.model_dump() | {"source": source}
        scope = and_(t.c.source == source, t.c.measure_id == slo.measure_id,
                     t.c.dimension.is_(None) if slo.dimension is None else t.c.dimension == slo.dimension,
                     t.c.slice_value.is_(None) if slo.slice_value is None else t.c.slice_value == slo.slice_value)
        with engine.begin() as conn:
            conn.execute(delete(t).where(scope))
            sid = conn.execute(t.insert().values(**body, updated_at=datetime.utcnow())).inserted_primary_key[0]
        return {"id": sid, **body}

    @app.delete("/v1/slos/{slo_id}", tags=["operate"])
    def delete_slo(slo_id: int, p: Principal = Depends(require("manage"))):
        t = store.slos
        cond = [t.c.id == slo_id]
        if not p.platform:
            cond.append(t.c.source == f"events:{p.tenant}")
        with engine.begin() as conn:
            gone = conn.execute(delete(t).where(and_(*cond))).rowcount
        if not gone:
            raise HTTPException(404, f"No SLO with id {slo_id} that this key can change.")
        return {"deleted": slo_id}

    @app.put("/v1/cost/rates", tags=["operate"])
    def put_rates(body: RateIn, p: Principal = Depends(require("manage"))):
        source = own_source(p, body.source)
        unknown = [k for k in body.rates if k not in cost.RATE_KEYS]
        if unknown:
            raise HTTPException(422, f"Unknown rate {', '.join(unknown)}. Known: {', '.join(cost.RATE_KEYS)}.")
        if any(v is not None and v < 0 for v in body.rates.values()):
            raise HTTPException(422, "Rates can't be negative.")
        t = store.cost_rates
        with engine.begin() as conn:
            for k, v in body.rates.items():
                conn.execute(delete(t).where(and_(t.c.source == source, t.c.key == k)))
                if v is not None:
                    conn.execute(t.insert().values(source=source, key=k, value=v, updated_at=datetime.utcnow()))
        return get_rates(source, p)

    @app.post("/v1/gates/evaluate", tags=["operate"], summary="Advance, hold or roll back a release")
    def evaluate_gate(req: GateRequest, x_tenant: Optional[str] = Header(None),
                      p: Principal = Depends(require("manage"))):
        tenant = tenant_for(p, x_tenant) if not p.platform or x_tenant else "*"
        missing = [k for k in REQUIRED_LINEAGE if not req.lineage.get(k)]
        if missing:
            raise HTTPException(422, f"Lineage must record: {', '.join(missing)}. "
                                     "Every promote or rollback has to be traceable.")
        floors = {r.measure_id: gates.noise_floor(req.identical_runs.get(r.measure_id, []))
                  for r in req.rules}
        decision = gates.evaluate([gates.GateRule(**r.model_dump()) for r in req.rules],
                                  req.samples, floors, req.required_slices)
        body = decision.to_dict()
        warnings = []
        pid, _, ver = req.lineage["prompt"].partition("@")
        if ver and tenant != "*" and (pid, ver) not in prompts.registry(engine, tenant):
            warnings.append(f"Prompt {req.lineage['prompt']} isn't in the registry: register it (POST /v1/prompts) "
                            "so this decision links to its template and production results.")
        body["warnings"] = warnings
        with engine.begin() as conn:
            gid = conn.execute(store.gate_decisions.insert().values(
                created_at=datetime.utcnow(), outcome=decision.outcome, tenant=tenant,
                lineage=req.lineage, detail=body)).inserted_primary_key[0]
        return {"id": gid, "lineage": req.lineage, **body}

    @app.get("/v1/scheduler", tags=["operate"])
    def scheduler_status(p: Principal = Depends(require("manage"))):
        if not p.platform:
            raise HTTPException(403, "The scheduler is instance-wide; only a platform key can see it.")
        return scheduler.status()

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

    # ---------- keys ----------

    @app.post("/v1/keys", tags=["keys"], status_code=201,
              summary="Create a key. The secret is returned once; store it now.")
    def create_key(body: KeyIn, p: Principal = Depends(require("admin"))):
        tenant = body.tenant or p.tenant
        if not p.platform and tenant != p.tenant:
            raise HTTPException(403, f"This key can only create keys for tenant '{p.tenant}'.")
        try:
            row, secret = auth.create_key(engine, tenant, body.name, body.scopes, body.expires_in_days)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return row | {"key": secret}

    @app.get("/v1/keys", tags=["keys"])
    def get_keys(p: Principal = Depends(require("admin"))):
        return auth.list_keys(engine, p.tenant)

    @app.delete("/v1/keys/{key_id}", tags=["keys"], summary="Revoke a key immediately")
    def delete_key(key_id: int, p: Principal = Depends(require("admin"))):
        if not auth.revoke_key(engine, key_id, p.tenant):
            raise HTTPException(404, f"No active key {key_id} that this key can revoke.")
        return {"revoked": key_id}

    # ---------- ingest ----------

    def too_big(n: int):
        if n > ingest.MAX_BATCH:
            raise HTTPException(413, f"{n} records in one request; the limit is {ingest.MAX_BATCH}. Split the batch.")

    @app.post("/v1/events", tags=["ingest"], summary="Send any mix of record types in one request")
    def ingest_batch(batch: EventBatch, x_tenant: Optional[str] = Header(None),
                     p: Principal = Depends(require("ingest"))):
        too_big(sum(len(getattr(batch, k)) for k in ingest.TABLES))
        return {"tenant": (t := tenant_for(p, x_tenant)), "ingested": ingest.write_batch(engine, batch, t)}

    def one_kind(kind: str, model):
        def endpoint(events: List[model], x_tenant: Optional[str] = Header(None),
                     p: Principal = Depends(require("ingest"))):
            too_big(len(events))
            return {"ingested": ingest.write(engine, kind, events, tenant_for(p, x_tenant))}
        return endpoint

    for path, kind, model in [("documents", "documents", DocumentEvent), ("stage-runs", "stage_runs", StageRunEvent),
                              ("calls", "calls", CallEvent), ("reviews", "reviews", ReviewEvent),
                              ("extractions", "extractions", ExtractionEvent), ("errors", "errors", ErrorEvent),
                              ("indexed", "extractions", ExtractionEvent)]:
        app.add_api_route(f"/v1/events/{path}", one_kind(kind, model), methods=["POST"], tags=["ingest"],
                          summary=f"Send {path.replace('-', ' ')}", include_in_schema=path != "indexed")

    @app.post("/v1/otlp/v1/traces", tags=["ingest"],
              summary="OpenTelemetry traces (OTLP/HTTP, JSON). Point a collector's otlphttp exporter at /v1/otlp",
              description=ingest.OTEL_MAPPING)
    async def otlp_traces(request: Request, x_tenant: Optional[str] = Header(None),
                          p: Principal = Depends(require("ingest"))):
        if "json" not in (request.headers.get("content-type") or ""):
            return JSONResponse({"detail": "Send OTLP/HTTP with JSON encoding (collector: encoding: json)."},
                                status_code=415)
        try:
            batch = ingest.from_otlp(await request.json())
        except Exception as exc:
            raise HTTPException(400, f"Couldn't read the OTLP payload: {exc}")
        too_big(sum(len(getattr(batch, k)) for k in ingest.TABLES))
        counts = ingest.write_batch(engine, batch, tenant_for(p, x_tenant))
        return {"partialSuccess": {}, "ingested": counts}

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
