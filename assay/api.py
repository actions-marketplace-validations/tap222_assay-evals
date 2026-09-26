"""HTTP API and dashboard.

Authenticate with `Authorization: Bearer <key>` (or `X-API-Key: <key>`).
See assay/auth.py for scopes and tenant isolation.
"""
# No `from __future__ import annotations` here: the per-record-type ingest
# endpoints are built in a loop, and FastAPI needs their body types as real
# objects rather than strings to resolve later.
import logging
import threading
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Security
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, delete, desc, or_, select

from assay import (agents, alerts, audit, auth, connect, schema, contracts, cost, coverage, failures, gates, integrations, learn, ingest, lifecycle, prompts, rootcause, runner, store, trace, workflow)

log = logging.getLogger("assay.api")
from assay.auth import Principal
from assay.config import Settings
from assay.ingest import (CallEvent, DocumentEvent, ErrorEvent, EvalResultEvent, EventBatch, ExtractionEvent,
                          FeedbackEvent, InputEvent, ReferenceEvent, ReviewEvent, StageRunEvent, TrajectoryEvent)
from assay.measures import GROUPS, REGISTRY
from assay.scheduler import Scheduler

STATIC = Path(__file__).parent / "static"


# ---------- request bodies ----------

class RateIn(BaseModel):
    source: Optional[str] = Field(None, description='Defaults to your own source (all sources for a platform key); "*" needs a platform key')
    rates: Dict[str, Optional[float]] = Field(..., description="Rate key → value; null removes it")


class LimitsIn(BaseModel):
    source: Optional[str] = Field(None, description="Defaults to your own source")
    abandon_minutes: Dict[str, Optional[float]] = Field(
        ..., description='Agent (the run\'s task) → minutes a run can go quiet before it\'s marked abandoned; '
                         '"*" for every other agent of this source; null removes it')


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


class ContractIn(BaseModel):
    source: Optional[str] = Field(None, description='Defaults to your own source; "*" needs a platform key')
    kind: str = Field(..., description="must_include | never | before | only_after | max_runs | allowed_steps")
    step: Optional[str] = Field(None, max_length=64)
    other: Optional[str] = Field(None, max_length=64, description="before / only_after: the other step")
    max_runs: Optional[int] = Field(None, ge=1, le=1000)
    steps: Optional[List[str]] = Field(None, description="allowed_steps: every step a document may run")
    when: Optional[Dict[str, List[str]]] = Field(
        None, description="Applies only to documents matching every attribute (segment, document_type, processing_mode)")
    unless: Optional[Dict[str, List[str]]] = Field(None, description="Documents matching any attribute are exempt")
    where: Optional[Dict[str, Any]] = Field(
        None, description='Conditions on the step\'s arguments (agent tool calls): {"confirmed": {"not": true}}, '
                          '{"region": {"in": ["eu"]}}, {"mode": "dry_run"}')
    same: Optional[List[str]] = Field(None, description="before / only_after: arguments the other step must share, "
                                                        'e.g. ["order_id"]')
    identical: Optional[bool] = Field(None, description="max_runs: count only calls with identical arguments")
    claim: Optional[str] = Field(None, max_length=512, description="claim: what the answer says, as a regular expression")
    needs: Optional[str] = Field(None, max_length=64, description="claim: the tool that must have succeeded")
    state: Optional[Dict[str, Any]] = Field(None, description='claim: {"name": "order:*", "field": "status", "is": "refunded"}')
    claim_in: Optional[str] = Field(None, description="claim: answer (default) or any (the model's own text too)")
    severity: str = Field("critical", description="critical | warning")
    note: Optional[str] = Field(None, max_length=512)


class EvalGateIn(BaseModel):
    source: str
    baseline: Optional[str] = Field(None, description="Run to compare with; defaults to the run before")
    tolerance: float = Field(0.01, ge=0, le=1, description="Largest acceptable drop in the pass rate")


class ApproveIn(BaseModel):
    source: str
    suite: str = Field("production-regressions", max_length=128)
    case: Optional[Dict[str, Any]] = Field(None, description="Edits to the drafted case: reference, properties, input")
    redact_pii: bool = Field(True, description="Replace emails, phone and card numbers in the input with placeholders")


class RejectIn(BaseModel):
    source: str
    note: Optional[str] = Field(None, max_length=1024)


class PatternStatusIn(BaseModel):
    source: str
    key: str
    status: str = Field(..., pattern="^(open|dismissed)$")


class SheetIn(BaseModel):
    text: str = Field(..., max_length=20_000_000, description="CSV or tab-separated text, first row headers")
    kind: Optional[str] = Field(None, description="errors | eval_results | feedback | documents | inputs; "
                                                  "omit to detect")
    mapping: Optional[Dict[str, Optional[str]]] = Field(None, description="field → column header")
    defaults: Optional[Dict[str, Any]] = Field(None, description="Values for every row, e.g. {\"run_id\": \"may-tests\"}")


class IntegrationIn(BaseModel):
    source: str
    config: Dict[str, Any]


class DecisionIn(BaseModel):
    source: str
    key: str = Field(..., max_length=512, description="The group's key, from GET /v1/failures")
    decision: Optional[str] = Field(None, description="accepted_change | not_a_problem | confirmed; null clears it")
    note: Optional[str] = Field(None, max_length=512)


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
    from assay import sso
    sso_cfg = sso.from_settings(settings)  # raises on a half-set-up SSO: better at start than at sign-in
    authn.sso = sso_cfg
    provider = sso.Provider(sso_cfg) if sso_cfg else None

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
        p = None if token else authn.session(request.cookies.get(sso.SESSION))
        request.state.principal = p  # a refusal below is still theirs, in the audit log
        if p is not None and request.method not in ("GET", "HEAD", "OPTIONS"):
            # A change made with a session needs its CSRF token too: another site can send the cookie,
            # but can't read it to set the header.
            v = sso.unsign(sso_cfg.session_secret, request.cookies.get(sso.SESSION)) or {}
            if not v.get("csrf") or request.headers.get("X-CSRF-Token") != v["csrf"]:
                raise HTTPException(403, "A change made while signed in needs the X-CSRF-Token header "
                                         "(the assay_csrf cookie's value).")
        if p is None:
            p = authn.authenticate(token)
        request.state.principal = p
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

    # ---------- single sign-on, people, and the audit log ----------

    @app.middleware("http")
    async def audit_changes(request: Request, call_next):
        response = await call_next(request)
        if sso.audited(request.method, request.url.path):
            p = getattr(request.state, "principal", None)
            try:
                sso.audit(engine, sso.actor_of(p), f"{request.method} {request.url.path}",
                          tenant=p.tenant if p else None, status=response.status_code,
                          detail={"query": dict(request.query_params)} if request.query_params else None)
            except Exception:  # the audit log must never break the change it records
                log.exception("couldn't write the audit log")
        return response

    @app.get("/auth/config", include_in_schema=False)
    def auth_config():
        return {"sso": sso_cfg is not None, "login": "/auth/login" if sso_cfg else None}

    @app.get("/auth/login", include_in_schema=False)
    def sso_login(next: str = "/"):
        if provider is None:
            raise HTTPException(404, "SSO isn't set up on this server (ASSAY_OIDC_ISSUER).")
        try:
            cookie, flow = sso.new_flow(sso_cfg, next)
            url = provider.login_url(flow["state"], flow["nonce"], flow["verifier"])
        except sso.SSOError as exc:
            raise HTTPException(502, str(exc))
        r = RedirectResponse(url, status_code=302)
        r.set_cookie(sso.FLOW, cookie, max_age=sso.FLOW_SECONDS, httponly=True, secure=sso_cfg.secure,
                     samesite="lax", path="/auth")
        return r

    @app.get("/auth/callback", include_in_schema=False)
    def sso_callback(request: Request, code: Optional[str] = None, state: Optional[str] = None,
                     error: Optional[str] = None, error_description: Optional[str] = None):
        if provider is None:
            raise HTTPException(404, "SSO isn't set up on this server.")

        def refuse(why: str, who: Optional[str] = None):
            sso.audit(engine, f"user:{who}" if who else "anonymous", "sign-in refused", tenant=sso_cfg.tenant,
                      status=403, detail={"why": why})  # the tenant they tried to sign in to
            raise HTTPException(403, f"Sign-in refused: {why}")
        flow = sso.unsign(sso_cfg.session_secret, request.cookies.get(sso.FLOW))
        if error:
            refuse(f"the provider said {error}" + (f": {error_description}" if error_description else ""))
        if not flow or not code or not state or state != flow["state"]:
            refuse("this sign-in wasn't started here, or it took longer than 10 minutes. Start again.")
        try:
            claims = provider.verify(provider.exchange(code, flow["verifier"]), flow["nonce"])
        except sso.SSOError as exc:
            refuse(str(exc))
        why = sso.admitted(sso_cfg, claims)
        if why:
            refuse(why, claims.get("email"))
        user = sso.upsert_user(engine, sso_cfg, claims)
        if user["disabled"]:
            refuse("this account is disabled here", user["email"])
        cookie, csrf = sso.new_session(sso_cfg, user)
        sso.audit(engine, f"user:{user['email'] or user['id']}", "sign-in", tenant=user["tenant"], status=200,
                  detail={"role": user["role"]})
        r = RedirectResponse(flow["next"], status_code=302)
        age = int(sso_cfg.session_hours * 3600)
        r.set_cookie(sso.SESSION, cookie, max_age=age, httponly=True, secure=sso_cfg.secure, samesite="lax")
        r.set_cookie(sso.CSRF, csrf, max_age=age, httponly=False, secure=sso_cfg.secure, samesite="lax")
        r.delete_cookie(sso.FLOW, path="/auth")
        return r

    @app.post("/auth/logout", include_in_schema=False)
    def sso_logout():
        r = JSONResponse({"signed_out": True})
        r.delete_cookie(sso.SESSION)
        r.delete_cookie(sso.CSRF)
        return r

    class UserIn(BaseModel):
        role: Optional[str] = Field("", description="read | manage | admin; null goes back to the provider's claims")
        disabled: Optional[bool] = None

    @app.get("/v1/users", tags=["admin"], summary="People who have signed in with SSO, and their roles")
    def users(p: Principal = Depends(require("admin"))):
        return sso.list_users(engine, p.tenant)

    @app.put("/v1/users/{user_id}", tags=["admin"], summary="Set a person's role over their claims, or disable them")
    def set_user(user_id: str, body: UserIn, p: Principal = Depends(require("admin"))):
        if p.user_id == user_id and (body.disabled or (body.role not in ("", None, "admin"))):
            raise HTTPException(400, "You can't take away your own admin role or disable yourself.")
        try:
            u = sso.set_user(engine, user_id, body.role, body.disabled, p.tenant)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if u is None:
            raise HTTPException(404, "No such person here.")
        return u

    @app.get("/v1/audit", tags=["admin"], summary="Who changed what, and when: every change, sign-in and refusal")
    def audit_log(actor: Optional[str] = None, limit: int = 200, p: Principal = Depends(require("admin"))):
        return sso.read_audit(engine, p.tenant, actor, limit)

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

    @app.get("/v1/trace/{document_id:path}", tags=["results"], summary="One document through every stage and call")
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

    @app.get("/v1/errors/{document_id:path}", tags=["results"],
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

    # ---------- workflow ----------

    @app.get("/v1/workflow", tags=["results"],
             summary="The pipeline as a graph, inferred from traffic, with each step's health, errors and broken contracts")
    def get_workflow(source: str, days: float = 7, p: Principal = Depends(require("read"))):
        src, window = runner.CachedSource(resolve(p, source)), runner.window_for_days(days)
        graph = workflow.build(src, window, engine, source)
        rules = contracts.load(engine, source)
        if rules:
            contracts.annotate(graph, contracts.check(src, window, rules))
        from assay import workflow_eval  # where Assay's evaluation connects to this pipeline
        graph["evaluation"] = workflow_eval.describe(engine, src, source, window, graph, settings)
        return graph

    @app.get("/v1/workflow/steps/{stage}/errors", tags=["results"],
             summary="Reported errors that started at one step ('(done)' for after the pipeline)")
    def get_step_errors(stage: str, source: str, days: float = 7, p: Principal = Depends(require("read"))):
        return workflow.stage_errors(runner.CachedSource(resolve(p, source)), runner.window_for_days(days), stage)

    @app.get("/v1/workflow/documents/{document_id:path}", tags=["results"],
             summary="One document's path through the workflow, with where each error started")
    def get_document_path(document_id: str, source: str, p: Principal = Depends(require("read"))):
        src = resolve(p, source)
        out = workflow.document_path(src, document_id)
        if out is None:
            raise HTTPException(404, f"No document '{document_id}' in {source}.")
        return out | {"contract_violations": contracts.check_document(src, document_id,
                                                                      contracts.load(engine, source)) or []}

    # ---------- agents ----------

    @app.post("/v1/events/trajectories", tags=["ingest"],
              summary="Send agent trajectories: reasoning, tool calls, state changes and the answer, in order")
    def ingest_trajectories(events: List[TrajectoryEvent], x_tenant: Optional[str] = Header(None),
                            p: Principal = Depends(require("ingest"))):
        too_big(sum(len(e.steps) + 1 for e in events))
        tenant = tenant_for(p, x_tenant)
        n = ingest.write_trajectories(engine, events, tenant)
        lifecycle.after_ingest(engine, tenant, [e.trajectory_id for e in events], settings.abandon_minutes)
        return {"ingested": n}

    @app.post("/v1/agents/references", tags=["ingest"],
              summary="What test cases expect: tool calls, answer and end state (upserts by case_id)")
    def put_references(refs: List[ReferenceEvent], x_tenant: Optional[str] = Header(None),
                       p: Principal = Depends(require("ingest"))):
        too_big(len(refs))
        return {"ingested": ingest.write_references(engine, refs, tenant_for(p, x_tenant))}

    @app.get("/v1/agents/references", tags=["results"])
    def get_references(source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return list(agents.references(engine, _tenant(source)).values())

    @app.get("/v1/agents/runs", tags=["results"], summary="Agent evaluation runs, newest first")
    def list_agent_runs(source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return agents.agent_runs(engine, _tenant(source))

    @app.post("/v1/agents/runs/{run_id}/evaluate", tags=["operate"],
              summary="Check every trajectory in a run against its reference and contracts; stored as eval results")
    def evaluate_agent_run(run_id: str, source: str, p: Principal = Depends(require("manage"))):
        out = agents.evaluate_run(engine, resolve(p, source), _tenant(source), run_id)
        if not out["trajectories"] and not out["running"]:
            raise HTTPException(404, f"No trajectories in run '{run_id}' for {source}.")
        return out

    @app.get("/v1/agents/runs/{run_id}", tags=["results"],
             summary="Pass rate per check, tool precision and recall, first bad steps, and efficiency vs the baseline")
    def agent_run_summary(run_id: str, source: str, baseline: Optional[str] = None,
                          p: Principal = Depends(require("read"))):
        out = agents.summary(engine, resolve(p, source), _tenant(source), run_id, baseline)
        if out is None:
            raise HTTPException(404, f"No trajectories in run '{run_id}' for {source}.")
        return out

    @app.get("/v1/agents/trajectories/{trajectory_id:path}", tags=["results"],
             summary="One trajectory step by step, against its reference: divergence, end state, contracts, cost")
    def get_trajectory(trajectory_id: str, source: str, p: Principal = Depends(require("read"))):
        out = agents.detail(engine, resolve(p, source), _tenant(source), trajectory_id)
        if out is None:
            raise HTTPException(404, f"No trajectory '{trajectory_id}' in {source}.")
        return out | {"evaluation": lifecycle.get(engine, _tenant(source), trajectory_id)}

    @app.post("/v1/agents/runs/{run_id}/judge", tags=["operate"],
              summary="Have an LLM judge each run's plan quality and consistency (a model call per run)")
    def judge_run(run_id: str, source: str, limit: Optional[int] = None, p: Principal = Depends(require("manage"))):
        check_source(p, source)
        from assay import judge
        try:
            rt = judge.runtime({"concurrency": settings.judge_concurrency, "rate_limit": settings.judge_rate_limit,
                                "max_time": settings.judge_max_time, "budget_usd": settings.judge_budget_usd})
        except ValueError as exc:  # ASSAY_PRICES that doesn't read
            raise HTTPException(500, str(exc))
        out = judge.judge_run(engine, _tenant(source), run_id, settings.judge_model, limit=limit,
                              redact=settings.judge_redact, provider=settings.judge_provider, rt=rt)
        out.pop("report", None)  # the summary is in "summary"
        if not out["judged"] and not out["not_run"]:
            raise HTTPException(404, f"No ended agent runs in test run '{run_id}' in {source}.")
        return out | {"model": settings.judge_model}

    @app.get("/v1/agents/conversations", tags=["results"],
             summary="Conversations, newest first: their turns, and whether any failed a check")
    def list_conversations(source: str, limit: int = 50, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return agents.conversations(engine, _tenant(source), max(1, min(limit, 500)))

    @app.get("/v1/agents/conversations/{conversation_id}", tags=["results"],
             summary="One conversation turn by turn: input, answer, steps and evaluation of each")
    def get_conversation(conversation_id: str, source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        out = agents.conversation(engine, _tenant(source), conversation_id)
        if out is None:
            raise HTTPException(404, f"No conversation '{conversation_id}' in {source}.")
        return out

    @app.get("/v1/agents/lifecycle", tags=["results"],
             summary="Agent runs by lifecycle: running, awaiting evaluation, evaluated, abandoned; latest failures")
    def agent_lifecycle(source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return lifecycle.overview(engine, _tenant(source)) | {
            "abandon_minutes": settings.abandon_minutes, "backlog_minutes": settings.backlog_minutes,
            "abandon_limits": _limits(source),
            "sweep": scheduler.last_sweep and {k: v for k, v in scheduler.last_sweep.items() if k != "backlog"}}

    def _limits(source: str) -> Dict[str, float]:
        return {task: m for (_, task), m in sorted(lifecycle.limits(engine, _tenant(source)).items())}

    @app.get("/v1/agents/limits", tags=["results"],
             summary="How long each agent's runs can go quiet before they're marked abandoned")
    def get_agent_limits(source: Optional[str] = None, p: Principal = Depends(require("read"))):
        src = source or f"events:{p.tenant}"
        check_source(p, src)
        return {"source": src, "default": settings.abandon_minutes, "abandon_minutes": _limits(src)}

    @app.put("/v1/agents/limits", tags=["operate"],
             summary="Give a slow agent longer before its quiet runs are marked abandoned")
    def put_agent_limits(body: LimitsIn, p: Principal = Depends(require("manage"))):
        src = body.source or f"events:{p.tenant}"
        check_source(p, src)
        if not src.startswith("events:"):
            raise HTTPException(422, "Abandon limits are for agent runs, sent as events: source events:<tenant>.")
        bad = [k for k, m in body.abandon_minutes.items() if m is not None and not 0 < m <= 7 * 24 * 60]
        if bad:
            raise HTTPException(422, f"Limits are minutes, more than 0 and at most a week (10080): {', '.join(bad)}.")
        long = [k for k in body.abandon_minutes if not k or len(k) > 128]
        if long:
            raise HTTPException(422, "An agent is its runs' task: 1 to 128 characters.")
        lifecycle.set_limits(engine, _tenant(src), body.abandon_minutes)
        return get_agent_limits(src, p)

    # ---------- the v1 event schema ----------

    @app.post("/v1/ingest", tags=["ingest"],
              summary="Send events in the v1 schema: runs, steps, and outcomes (docs/event-schema.md)")
    async def ingest_v1(request: Request, x_tenant: Optional[str] = Header(None),
                        p: Principal = Depends(require("ingest"))):
        tenant = tenant_for(p, x_tenant)
        body = await request.json()
        items = body.get("events") if isinstance(body, dict) else body
        if not isinstance(items, list):
            raise HTTPException(422, 'Send a list of events, or {"events": [...]}.')
        too_big(len(items))
        try:
            events = schema.EVENTS.validate_python(items)
        except ValidationError as e:
            raise HTTPException(422, [{"event": err["loc"][0] if err["loc"] else None,
                                       "field": ".".join(str(x) for x in err["loc"][2:]) or None,
                                       "problem": err["msg"]} for err in e.errors()[:50]])
        counts = schema.ingest(engine, events, tenant)
        # Runs this batch ended (or added late data to) are evaluated now, not after a delay.
        lifecycle.after_ingest(engine, tenant, {e.run_id for e in events if getattr(e, "run_id", None)},
                               settings.abandon_minutes)
        dup = counts.pop("duplicate_checks", 0)
        return {"accepted": len(events), "by_type": counts} | ({"duplicate_checks": dup} if dup else {})

    @app.get("/v1/schema", tags=["ingest"], summary="The v1 event schema, as JSON Schema")
    def get_schema():
        return schema.json_schema()

    # ---------- connecting without code ----------

    def base_url(request: Request) -> str:
        return (settings.public_url or str(request.base_url)).rstrip("/")

    @app.get("/v1/connect/status", tags=["results"],
             summary="What has arrived, and which features that switches on, in plain words")
    def connect_status(source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return connect.status(engine, source)

    @app.post("/v1/connect/preview", tags=["ingest"],
              summary="Read a spreadsheet (CSV or pasted cells): what it holds, which column is which, what's wrong")
    def connect_preview(body: SheetIn, p: Principal = Depends(require("ingest"))):
        if body.kind and body.kind not in connect.SHEETS:
            raise HTTPException(422, f"kind is one of: {', '.join(connect.SHEETS)}.")
        return connect.preview(body.text, body.kind)

    @app.post("/v1/connect/import", tags=["ingest"], summary="Import a spreadsheet")
    def connect_import(body: SheetIn, x_tenant: Optional[str] = Header(None), p: Principal = Depends(require("ingest"))):
        kind = body.kind or connect.preview(body.text)["kind"]
        if kind not in connect.SHEETS:
            raise HTTPException(422, f"kind is one of: {', '.join(connect.SHEETS)}.")
        return connect.import_sheet(engine, tenant_for(p, x_tenant), kind, body.text, body.mapping, body.defaults)

    @app.get("/v1/connect/handoff", tags=["results"],
             summary="Instructions to send whoever will wire it up (otel | python | http | database)")
    def connect_handoff(request: Request, source: str, method: str = "otel", p: Principal = Depends(require("read"))):
        check_source(p, source)
        return {"text": connect.handoff(base_url(request), source, None, method)}

    @app.get("/v1/integrations", tags=["operate"], summary="Slack, Jira and Linear, as set up (secrets masked)")
    def get_integrations(source: str, p: Principal = Depends(require("manage"))):
        check_source(p, source)
        return {"configured": integrations.list_(engine, source),
                "kinds": {k: {"label": v["label"], "fields": v["fields"]} for k, v in integrations.KINDS.items()}}

    @app.put("/v1/integrations/{kind}", tags=["operate"])
    def put_integration(kind: str, body: IntegrationIn, p: Principal = Depends(require("manage"))):
        check_source(p, body.source)
        if kind not in integrations.KINDS:
            raise HTTPException(404, f"Unknown integration '{kind}'.")
        try:
            return integrations.save(engine, body.source, kind, body.config)
        except ValueError as e:
            raise HTTPException(422, str(e))

    @app.delete("/v1/integrations/{kind}", tags=["operate"])
    def delete_integration(kind: str, source: str, p: Principal = Depends(require("manage"))):
        check_source(p, source)
        integrations.remove(engine, source, kind)
        return {"removed": kind}

    @app.post("/v1/integrations/{kind}/test", tags=["operate"], summary="Check the connection works")
    def test_integration(kind: str, source: str, p: Principal = Depends(require("manage"))):
        check_source(p, source)
        try:
            return {"ok": True, "message": integrations.test(engine, source, kind)}
        except (ValueError, RuntimeError) as e:
            return {"ok": False, "message": str(e)}

    @app.get("/v1/integrations/ci", tags=["operate"],
             summary="A ready-to-paste CI job that blocks a release unless Assay says advance")
    def ci_config(request: Request, source: str, system: str = "github", tolerance: float = 0.01,
                  p: Principal = Depends(require("read"))):
        check_source(p, source)
        return {"system": system, "text": integrations.ci_config(system, base_url(request), source, tolerance)}

    # ---------- learning from production ----------

    @app.get("/v1/learn/anomalies", tags=["results"],
             summary="Traces that look wrong without anyone saying so, each with the signals behind its score")
    def learn_anomalies(source: str, days: float = 7, limit: int = 200, threshold: float = learn.THRESHOLD,
                        p: Principal = Depends(require("read"))):
        sc = learn.score(runner.CachedSource(resolve(p, source)), runner.window_for_days(days), engine, threshold)
        return {"traces": sc["traces"], "anomalous": len(sc["anomalous"]), "threshold": threshold,
                "items": [a | {"received_at": a["received_at"].isoformat()} for a in sc["anomalous"][:min(limit, 2000)]]}

    @app.get("/v1/learn/patterns", tags=["results"],
             summary="Anomalous traces clustered into patterns, each with where it is in the loop")
    def learn_patterns(source: str, days: float = 7, threshold: float = learn.THRESHOLD,
                       p: Principal = Depends(require("read"))):
        return learn.patterns(runner.CachedSource(resolve(p, source)), runner.window_for_days(days), engine, threshold)

    @app.post("/v1/learn/patterns/candidates", tags=["operate"],
              summary="Draft test cases from a pattern's most typical and most different traces")
    def learn_propose(source: str, key: str, days: float = 7, threshold: float = learn.THRESHOLD,
                      p: Principal = Depends(require("manage"))):
        return learn.propose(runner.CachedSource(resolve(p, source)), runner.window_for_days(days), engine, key,
                             threshold)

    @app.put("/v1/learn/patterns/status", tags=["operate"], summary="Dismiss a pattern (not a bug), or reopen it")
    def learn_pattern_status(body: PatternStatusIn, p: Principal = Depends(require("manage"))):
        check_source(p, body.source)
        learn.set_status(engine, body.source, body.key, body.status)
        return {"key": body.key, "status": body.status}

    @app.post("/v1/learn/patterns/ticket", tags=["operate"], summary="Open a Jira or Linear ticket for a pattern")
    def learn_ticket(request: Request, source: str, key: str, days: float = 7, p: Principal = Depends(require("manage"))):
        pats = learn.patterns(runner.CachedSource(resolve(p, source)), runner.window_for_days(days), engine,
                              update_log=False)
        pattern = next((x for x in pats["patterns"] if x["key"] == key), None)
        if pattern is None:
            raise HTTPException(404, "No such pattern in this window.")
        try:
            ticket = integrations.create_ticket(engine, source, pattern, base_url(request))
        except ValueError as e:
            raise HTTPException(422, str(e))
        except RuntimeError as e:
            raise HTTPException(502, str(e))
        integrations.record_ticket(engine, source, key, ticket)
        return ticket

    @app.get("/v1/learn/suites/{name}/export", tags=["results"],
             summary="A suite as a file your test tool can run: json, jsonl or csv")
    def learn_export(name: str, source: str, format: str = "json", p: Principal = Depends(require("read"))):
        check_source(p, source)
        if format not in ("json", "jsonl", "csv"):
            raise HTTPException(422, "format is json, jsonl or csv.")
        cases = learn.suite(engine, source, name)
        for c in cases:  # added before cases kept their trace: take it from the trace, if it's still there
            if not c.get("trajectory") and c.get("origin_trace"):
                c["trajectory"] = learn.snapshot(engine, source, c["origin_trace"])
        body, media = learn.export(cases, format)
        return Response(body, media_type=media,
                        headers={"Content-Disposition": f'attachment; filename="{name}.{format}"'})

    # ---------- reading production conversations (assay/review.py) ----------

    def reviewer():
        from assay import review
        try:
            return review.reader_for(settings)
        except ImportError:
            raise HTTPException(501, "Reading conversations needs a model: pip install anthropic, or set "
                                     "ASSAY_JUDGE_PROVIDER.")

    @app.post("/v1/review/run", tags=["operate"],
              summary="Read a sample of production conversations, note what went wrong, and group it (model calls)")
    def review_run(source: str, sample: Optional[int] = None, days: float = 1.0,
                   p: Principal = Depends(require("manage"))):
        check_source(p, source)
        from assay import review
        rt = review.runtime_for(settings)
        return review.run(engine, runner.CachedSource(resolve(p, source)), _tenant(source), reviewer(),
                          n=sample if sample is not None else settings.review_sample, days=days, rt=rt,
                          redact=settings.judge_redact)

    @app.get("/v1/review/queue", tags=["results"],
             summary="Conversations for a person to read, the likeliest wrong first, with the model's note as a suggestion")
    def review_queue(source: str, days: float = 7, limit: int = 20, p: Principal = Depends(require("read"))):
        check_source(p, source)
        from assay import review
        return review.queue(engine, runner.CachedSource(resolve(p, source)), _tenant(source), days, min(limit, 100),
                            person_first=settings.review_person_first)

    @app.post("/v1/review/search", tags=["operate"],
              summary="Read conversations nobody has for likely instances of the failures people described (model calls)")
    def review_search(source: str, sample: Optional[int] = None, days: float = 7,
                      p: Principal = Depends(require("manage"))):
        check_source(p, source)
        from assay import review
        try:
            return review.search(engine, runner.CachedSource(resolve(p, source)), _tenant(source), reviewer(),
                                 n=sample if sample is not None else settings.review_sample, days=days,
                                 rt=review.runtime_for(settings), redact=settings.judge_redact,
                                 person_first=settings.review_person_first)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    class DimIn(BaseModel):
        name: str = Field(..., max_length=128)
        values: List[str] = Field(..., min_length=2, max_length=50)

    class CompareIn(BaseModel):
        dimensions: List[DimIn] = Field(..., min_length=1, max_length=20)

    @app.post("/v1/synthetic/compare", tags=["results"],
              summary="Synthetic runs against production on the same dimensions (a model call per new conversation)")
    def synthetic_compare(source: str, body: CompareIn, days: float = 30, sample: int = 100,
                          p: Principal = Depends(require("manage"))):
        check_source(p, source)
        from assay import review, synth
        dims = [synth.Dimension(d.name, d.values) for d in body.dimensions]
        return synth.compare(engine, _tenant(source), reviewer(), dims, days, max(0, min(sample, 1000)),
                             rt=review.runtime_for(settings), redact=settings.judge_redact)

    @app.get("/v1/review/saturation", tags=["results"],
             summary="Whether new reviews still find new failure modes")
    def review_saturation(source: str, window: int = 20, p: Principal = Depends(require("read"))):
        check_source(p, source)
        from assay import review
        return review.saturation(engine, _tenant(source), max(1, min(window, 500)))

    class NoteIn2(BaseModel):
        conversation: str = Field(..., max_length=128)
        went_wrong: bool
        note: Optional[str] = Field(None, max_length=500)
        hint: Optional[str] = Field(None, max_length=80)
        first_step: Optional[Dict[str, Any]] = Field(None, description='the first upstream failure: {"trace_id", "seq"}')
        accept: Optional[int] = Field(None, description="the model's suggestion to accept, by its id")
        trace_ids: Optional[List[str]] = None
        also: Optional[List[Dict[str, Any]]] = Field(None, max_length=10,
                                                     description='other independent failures: [{"note", "hint", "first_step"}]')

    @app.post("/v1/review/notes", tags=["operate"], summary="A person's note on a conversation (open coding)")
    def review_note(source: str, body: NoteIn2, p: Principal = Depends(require("manage"))):
        check_source(p, source)
        from assay import review, sso
        try:
            return review.add_note(engine, _tenant(source), body.conversation, sso.actor_of(p), body.went_wrong,
                                   body.note, body.first_step, body.hint, body.accept, body.trace_ids,
                                   body.also)
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.get("/v1/review/categories", tags=["results"],
             summary="Failure categories found by reading conversations: share now and the week before, examples")
    def review_categories(source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        from assay import review
        return review.categories(engine, _tenant(source))

    class CategoryIn(BaseModel):
        status: Optional[str] = Field(None, description="open | confirmed | dismissed")
        name: Optional[str] = Field(None, max_length=128)
        merge_into: Optional[int] = None

    @app.put("/v1/review/categories/{category_id}", tags=["operate"],
             summary="Confirm, dismiss, rename or merge a failure category")
    def review_update(category_id: int, source: str, body: CategoryIn, p: Principal = Depends(require("manage"))):
        check_source(p, source)
        from assay import review
        try:
            out = review.update(engine, _tenant(source), category_id, body.status, body.name, body.merge_into)
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        if out is None:
            raise HTTPException(404, "No such category.")
        return out

    @app.post("/v1/review/categories/{category_id}/candidates", tags=["operate"],
              summary="Draft test cases from a category's conversations")
    def review_candidates(category_id: int, source: str, days: int = 30, p: Principal = Depends(require("manage"))):
        check_source(p, source)
        from assay import review
        return review.propose(engine, runner.CachedSource(resolve(p, source)), category_id,
                              runner.window_for_days(days))

    @app.get("/v1/review/categories/{category_id}/personas", tags=["results"],
             summary="Simulated-user personas from a category's conversations (assay_sdk.Persona)")
    def review_personas(category_id: int, source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        from assay import review
        return review.personas(engine, _tenant(source), category_id)

    # ---------- the report (assay/report.py) ----------

    @app.get("/v1/report", tags=["results"],
             summary="What the evaluation found: issues caught before users, failure modes, fixes, the log")
    def evaluation_report(source: str, days: float = 7, format: str = "markdown", p: Principal = Depends(require("read"))):
        check_source(p, source)
        from assay import report
        r = report.build(engine, _tenant(source), days, source=runner.CachedSource(resolve(p, source)))
        if format == "json":
            return report.as_json(r)
        return Response(report.markdown(r), media_type="text/markdown")

    class NoteIn(BaseModel):
        text: str = Field(..., max_length=2000)
        by: Optional[str] = Field(None, max_length=256)

    @app.post("/v1/report/log", tags=["operate"], summary="Add to the running log: what you found, learned, fixed")
    def report_note(source: str, body: NoteIn, p: Principal = Depends(require("manage"))):
        check_source(p, source)
        from assay import report, sso
        report.note(engine, _tenant(source), body.text, by=body.by or sso.actor_of(p))
        return {"logged": True}

    @app.post("/v1/report/send", tags=["operate"], summary="Post the report to the alert webhook (Slack)")
    def report_send(source: str, days: float = 7, p: Principal = Depends(require("manage"))):
        check_source(p, source)
        if not settings.webhook_url:
            raise HTTPException(400, "Set ASSAY_WEBHOOK_URL to post the report.")
        from assay import integrations, report
        md = report.markdown(report.build(engine, _tenant(source), days, source=runner.CachedSource(resolve(p, source))))
        integrations._post(settings.webhook_url, {"text": md})
        return {"sent": True}

    @app.get("/v1/learn/candidates", tags=["results"], summary="Drafted test cases: proposed, approved, rejected")
    def learn_candidates(source: str, status: Optional[str] = None, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return learn.candidates(engine, source, status)

    @app.post("/v1/learn/candidates/{candidate_id}/approve", tags=["operate"],
              summary="Add a drafted case to a suite, with edits; for agents its reference is stored for evaluation")
    def learn_approve(candidate_id: int, body: ApproveIn, p: Principal = Depends(require("manage"))):
        check_source(p, body.source)
        out = learn.approve(engine, body.source, candidate_id, body.suite, body.case, body.redact_pii, p.name)
        if out is None:
            raise HTTPException(404, f"No candidate {candidate_id} in {body.source}.")
        return out

    @app.post("/v1/learn/candidates/{candidate_id}/reject", tags=["operate"])
    def learn_reject(candidate_id: int, body: RejectIn, p: Principal = Depends(require("manage"))):
        check_source(p, body.source)
        if not learn.reject(engine, body.source, candidate_id, body.note, p.name):
            raise HTTPException(404, f"No candidate {candidate_id} in {body.source}.")
        return {"rejected": candidate_id}

    @app.get("/v1/learn/suites", tags=["results"], summary="Regression suites built from production failures")
    def learn_suites(source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return {"suites": learn.suites(engine, source), "loop": learn.loop_metrics(engine, source)}

    @app.get("/v1/learn/suites/{name}", tags=["results"],
             summary="A suite's cases: input, expectations, the production failure each guards, latest result")
    def learn_suite(name: str, source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return learn.suite(engine, source, name)

    # ---------- failure causes ----------

    def _public(analysis):
        if analysis is None:
            return None
        return analysis | {"groups": [{k: v for k, v in g.items() if k != "member_ids"} for g in analysis["groups"]]}

    def _tenant(source: str) -> str:
        return prompts.registry_tenant(source)

    @app.get("/v1/failures", tags=["results"],
             summary="Reported errors grouped into causes, each with its kind (AI, infrastructure, evaluator, "
                     "intended change) and the evidence")
    def failure_causes(source: str, days: float = 30, p: Principal = Depends(require("read"))):
        out = failures.production(runner.CachedSource(resolve(p, source)), runner.window_for_days(days), engine)
        if out is None:
            raise HTTPException(404, f"No errors have been reported for {source}. POST /v1/errors, or send "
                                     "evaluation results to POST /v1/events/eval-results.")
        return _public(out)

    @app.get("/v1/evals/runs", tags=["results"], summary="Evaluation runs, newest first, with pass/fail counts")
    def list_eval_runs(source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        return failures.eval_runs(engine, _tenant(source))

    @app.get("/v1/evals/runs/{run_id}/failures", tags=["results"],
             summary="One evaluation run's failures grouped into causes, compared with the run before it")
    def eval_failures(run_id: str, source: str, baseline: Optional[str] = None, tolerance: float = 0.01,
                      p: Principal = Depends(require("read"))):
        out = failures.evaluation(engine, resolve(p, source), _tenant(source), run_id, baseline, tolerance)
        if out is None:
            raise HTTPException(404, f"No results for evaluation run '{run_id}' in {source}.")
        return _public(out)

    @app.get("/v1/evals/runs/{run_id}/stability", tags=["results"],
             summary="Pass rate per check from its attempts: got worse, flaky, needs reruns; and the run's call")
    def eval_stability(run_id: str, source: str, baseline: Optional[str] = None, tolerance: float = 0.01,
                       p: Principal = Depends(require("read"))):
        out = failures.evaluation(engine, resolve(p, source), _tenant(source), run_id, baseline, tolerance)
        if out is None:
            raise HTTPException(404, f"No results for evaluation run '{run_id}' in {source}.")
        return out["stability"]

    @app.get("/v1/evals/runs/{run_id}/diff", tags=["results"],
             summary="What behavior changed since the baseline: regressions with the flow before and after, "
                     "severity, what changed but still passes, flaky and improved cases")
    def eval_diff(run_id: str, source: str, baseline: Optional[str] = None, tolerance: float = 0.01,
                  format: str = "json", p: Principal = Depends(require("read"))):
        from assay import diff
        if format not in ("json", "markdown", "text"):
            raise HTTPException(422, "format is json, markdown or text.")
        src, tenant = resolve(p, source), _tenant(source)
        runs = failures.eval_runs(engine, tenant)
        current = diff.resolve(runs, run_id)
        if current is None:
            raise HTTPException(404, f"No evaluation run or version '{run_id}' in {source}.")
        base = diff.resolve(runs, baseline) if baseline else failures._baseline(runs, current, None)
        if base is None:
            raise HTTPException(404, f"No run or version '{baseline}' in {source}." if baseline else
                                     f"Nothing to compare '{run_id}' with: it's the first run in {source}.")
        d = diff.compute(engine, tenant, current, base, {"tolerance": tolerance,
                                                          "behavior": {"fail": True, "ratios": {}}}, src)
        if "error" in d:
            raise HTTPException(404, d["error"])
        if format == "json":
            return d
        return Response({"markdown": diff.markdown, "text": diff.text}[format](d),
                        media_type="text/markdown" if format == "markdown" else "text/plain")

    @app.get("/v1/evals/runs/{run_id}/verdicts", tags=["results"],
             summary="Every check's verdict: PASS, FAIL, FLAKY, INCONCLUSIVE, INVALID, TIMEOUT, RATE_LIMITED, "
                     "EVALUATOR_ERROR, INFRA_ERROR, MISSING")
    def eval_verdicts(run_id: str, source: str, verdict: Optional[str] = None, baseline: Optional[str] = None,
                      p: Principal = Depends(require("read"))):
        out = failures.evaluation(engine, resolve(p, source), _tenant(source), run_id, baseline)
        if out is None:
            raise HTTPException(404, f"No results for evaluation run '{run_id}' in {source}.")
        v = out["verdicts"]
        return {"run_id": run_id, "counts": v["counts"],
                "checks": [c for c in v["checks"] if verdict is None or c["verdict"] == verdict.upper()]}

    @app.get("/v1/evals/runs/{run_id}/audit", tags=["results"],
             summary="Were the evaluators given the right data? Their recorded inputs, checked against the trace")
    def eval_audit(run_id: str, source: str, p: Principal = Depends(require("read"))):
        check_source(p, source)
        out = audit.evaluation_run(engine, _tenant(source), run_id)
        if out is None:
            raise HTTPException(404, f"No results for evaluation run '{run_id}' in {source}.")
        return out

    @app.post("/v1/evals/runs/{run_id}/gate", tags=["operate"],
              summary="Advance, rerun, hold or roll back on an evaluation run, allowing for flakiness; recorded")
    def eval_gate(run_id: str, body: EvalGateIn, p: Principal = Depends(require("manage"))):
        a = failures.evaluation(engine, resolve(p, body.source), _tenant(body.source), run_id, body.baseline,
                                body.tolerance)
        if a is None:
            raise HTTPException(404, f"No results for evaluation run '{run_id}' in {body.source}.")
        out = a["stability"]
        lineage = {**out["lineage"], "eval_run": run_id, **({"baseline_run": out["baseline"]} if out["baseline"] else {})}
        detail = {"kind": "eval_run", "slices": [], "reasons": out["reasons"], "states": out["states"],
                  "roles": out["roles"],
                  "change": out["change"], "noise": out["noise"], "reruns": out["reruns"][:200],
                  "got_worse": out["got_worse"][:200]}
        with engine.begin() as conn:
            gid = conn.execute(store.gate_decisions.insert().values(
                created_at=datetime.utcnow(), outcome=out["outcome"], tenant=_tenant(body.source),
                lineage=lineage, detail=detail)).inserted_primary_key[0]
        return {"id": gid, "outcome": out["outcome"], "lineage": lineage, **detail}

    @app.get("/v1/evals/runs/{run_id}/expectations", tags=["results"],
             summary="For a group accepted as an intended change: the new expected values, to update the test set")
    def eval_expectations(run_id: str, source: str, key: str, p: Principal = Depends(require("read"))):
        out = failures.evaluation(engine, resolve(p, source), _tenant(source), run_id)
        if out is None:
            raise HTTPException(404, f"No results for evaluation run '{run_id}' in {source}.")
        return failures.expectations(out, key, engine, _tenant(source))

    @app.put("/v1/failures/decisions", tags=["operate"],
             summary="Record a call on a group of failures: accepted as intended, not a problem, or confirmed")
    def put_decision(body: DecisionIn, p: Principal = Depends(require("manage"))):
        check_source(p, body.source)
        if body.decision is not None and body.decision not in failures.DECISIONS:
            raise HTTPException(422, f"decision is one of: {', '.join(failures.DECISIONS)}, or null to clear it.")
        failures.save_decision(engine, body.source, body.key, body.decision, body.note, p.name)
        return {"source": body.source, "key": body.key, "decision": body.decision}

    # ---------- path contracts ----------

    @app.get("/v1/contracts", tags=["results"], summary="Path contracts for a source, and how each is holding up")
    def list_contracts(source: str, days: float = 7, p: Principal = Depends(require("read"))):
        src = runner.CachedSource(resolve(p, source))
        return contracts.check(src, runner.window_for_days(days), contracts.load(engine, source))

    @app.get("/v1/contracts/suggestions", tags=["results"],
             summary="Contracts the paths seen already keep, to confirm rather than write from scratch")
    def suggest_contracts(source: str, days: float = 30, p: Principal = Depends(require("read"))):
        src = runner.CachedSource(resolve(p, source))
        return contracts.suggest(src, runner.window_for_days(days), contracts.load(engine, source))

    @app.get("/v1/contracts/shifts", tags=["results"],
             summary="Path changes that break no contract: new steps, new moves, shares that moved beyond noise")
    def path_shifts(source: str, days: float = 7, p: Principal = Depends(require("read"))):
        return contracts.shifts(runner.CachedSource(resolve(p, source)), runner.window_for_days(days))

    @app.get("/v1/contracts/documents/{document_id:path}", tags=["results"],
             summary="The contracts one document breaks, and at which step")
    def document_contracts(document_id: str, source: str, p: Principal = Depends(require("read"))):
        out = contracts.check_document(resolve(p, source), document_id, contracts.load(engine, source))
        if out is None:
            raise HTTPException(404, f"No document '{document_id}' in {source}.")
        return out

    def contract_body(body: ContractIn, p: Principal) -> tuple:
        source = own_source(p, body.source)
        data = body.model_dump()
        err = contracts.validate(data)
        if err:
            raise HTTPException(422, err)
        return source, data

    def own_contract(p: Principal, contract_id: int):
        t = store.path_contracts
        with engine.connect() as conn:
            row = conn.execute(select(t).where(t.c.id == contract_id)).first()
        if row is None or not (p.platform or row.source == f"events:{p.tenant}"):
            raise HTTPException(404, f"No contract with id {contract_id} that this key can change.")
        return row

    @app.post("/v1/contracts", tags=["operate"], status_code=201, summary="Add a path contract")
    def create_contract(body: ContractIn, p: Principal = Depends(require("manage"))):
        source, data = contract_body(body, p)
        return contracts.save(engine, source, data)

    @app.put("/v1/contracts/{contract_id}", tags=["operate"], summary="Change a path contract")
    def update_contract(contract_id: int, body: ContractIn, p: Principal = Depends(require("manage"))):
        own_contract(p, contract_id)
        source, data = contract_body(body, p)
        return contracts.save(engine, source, data, contract_id)

    @app.delete("/v1/contracts/{contract_id}", tags=["operate"],
                summary="Remove a path contract and resolve its open alert")
    def delete_contract(contract_id: int, p: Principal = Depends(require("manage"))):
        own_contract(p, contract_id)
        with engine.begin() as conn:
            conn.execute(delete(store.path_contracts).where(store.path_contracts.c.id == contract_id))
            contracts.close_alerts(conn, contract_id)
        return {"deleted": contract_id}

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
                                     notify=integrations.notifier(engine, source.name, settings.public_url,
                                                                  settings.notifier()),
                                     alert_min_n=settings.alert_min_n,
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
        scheduler.sweep()  # agent runs that went quiet, and any run not yet evaluated
        if settings.schedule_sources:
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
                              ("eval-results", "eval_results", EvalResultEvent),
                              ("inputs", "inputs", InputEvent), ("feedback", "feedback", FeedbackEvent),
                              ("indexed", "extractions", ExtractionEvent)]:
        app.add_api_route(f"/v1/events/{path}", one_kind(kind, model), methods=["POST"], tags=["ingest"],
                          summary=f"Send {path.replace('-', ' ')}", include_in_schema=path != "indexed")

    @app.post("/v1/otlp/v1/traces", tags=["ingest"],
              summary="OpenTelemetry traces (OTLP/HTTP, JSON). Point a collector's otlphttp exporter at /v1/otlp",
              description=ingest.OTEL_MAPPING + "\n" + ingest.OPENINFERENCE)
    async def otlp_traces(request: Request, x_tenant: Optional[str] = Header(None),
                          p: Principal = Depends(require("ingest"))):
        if "json" not in (request.headers.get("content-type") or ""):
            return JSONResponse({"detail": "Send OTLP/HTTP with JSON encoding (collector: encoding: json)."},
                                status_code=415)
        try:
            payload = await request.json()
            batch, ends = ingest.from_otlp(payload), ingest.otlp_root_ends(payload)
        except Exception as exc:
            raise HTTPException(400, f"Couldn't read the OTLP payload: {exc}")
        too_big(sum(len(getattr(batch, k)) for k in ingest.TABLES))
        tenant = tenant_for(p, x_tenant)
        # A run's spans come over several batches: add to it, and end it when its root span arrives.
        counts = ingest.write_batch(engine, batch, tenant, merge_trajectories=True)
        ended = ingest.end_trajectories(engine, tenant, ends)
        lifecycle.after_ingest(engine, tenant, {t.trajectory_id for t in batch.trajectories} | set(ended),
                               settings.abandon_minutes)
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
