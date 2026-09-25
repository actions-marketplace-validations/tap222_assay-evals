"""The Assay event schema, v1: models, JSON Schema, and how events land in Assay's tables.

See docs/event-schema.md for the design. A run is a stream: `run.start`, then
`step`s as they happen (each keyed by (run_id, seq), so they can arrive out of
order or be resent), then `run.end`. Outcomes (`feedback`, `check`,
`correction`, `expect`) arrive whenever they're known.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator
from sqlalchemy import and_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import Engine

from assay import store

VERSION = 1
MAX_EVENTS = 5000
MAX_JSON = 256 * 1024  # per free-form field: args, result, outputs, value, input


class _E(BaseModel):
    model_config = ConfigDict(extra="forbid")
    v: Literal[1] = Field(..., description="Schema version")
    id: str = Field(..., min_length=1, max_length=128, description="Unique per event; retries reuse it")
    ts: datetime = Field(..., description="When it happened (a step: when it started)")

    @model_validator(mode="after")
    def _checks(self):
        for name in type(self).model_fields:
            v = getattr(self, name)
            if isinstance(v, datetime) and v.tzinfo is not None:
                object.__setattr__(self, name, v.astimezone(timezone.utc).replace(tzinfo=None))
            if name in ("args", "result", "outputs", "value", "input") and v is not None \
                    and len(json.dumps(v, default=str)) > MAX_JSON:
                raise ValueError(f"{name} is larger than {MAX_JSON // 1024} KB; trim it or send a reference")
        return self


class Test(BaseModel):
    model_config = ConfigDict(extra="forbid")
    run: str = Field(..., max_length=128, description="The test run, e.g. nightly-2026-09-24")
    case: str = Field(..., max_length=128, description="The test case; the same across test runs")
    attempt: Optional[int] = Field(None, ge=0)


Scalar = Union[str, int, float, bool, None]


class RunStart(_E):
    type: Literal["run.start"]
    run_id: str = Field(..., max_length=128)
    kind: Literal["agent", "pipeline"] = Field("agent", description="agent: llm/tool/state/answer steps; "
                                                                    "pipeline: stage steps with model calls")
    task: Optional[str] = Field(None, max_length=128)
    segment: Optional[str] = Field(None, max_length=128)
    input: Optional[Any] = None
    input_ref: Optional[str] = Field(None, max_length=1024)
    version: Optional[Dict[str, str]] = None
    test: Optional[Test] = None
    parent_run_id: Optional[str] = Field(None, max_length=128)
    tags: Optional[Dict[str, Scalar]] = None


class Step(_E):
    type: Literal["step"]
    run_id: str = Field(..., max_length=128)
    seq: int = Field(..., ge=0)
    kind: Literal["llm", "tool", "state", "answer", "stage"]
    name: Optional[str] = Field(None, max_length=128)
    parent_seq: Optional[int] = Field(None, ge=0)
    ended_at: Optional[datetime] = None
    status: Literal["ok", "error"] = "ok"
    error: Optional[str] = Field(None, max_length=2048)
    # llm
    model: Optional[str] = Field(None, max_length=128)
    tokens_in: Optional[int] = Field(None, ge=0)
    tokens_out: Optional[int] = Field(None, ge=0)
    cost_usd: Optional[float] = Field(None, ge=0)
    prompt: Optional[str] = Field(None, max_length=192, description="id@version")
    text: Optional[str] = Field(None, max_length=32768)
    # tool
    args: Optional[Dict[str, Any]] = None
    result: Optional[Any] = None
    # state
    op: Optional[Literal["create", "update", "delete"]] = None
    value: Optional[Any] = None
    # stage
    outputs: Optional[Dict[str, Any]] = None
    did_work: Optional[bool] = None

    @model_validator(mode="after")
    def _kind_fields(self):
        allowed = {"llm": {"model", "tokens_in", "tokens_out", "cost_usd", "prompt", "text"},
                   "tool": {"args", "result"}, "state": {"op", "value"}, "answer": {"text"},
                   "stage": {"outputs", "did_work", "prompt"}}[self.kind]
        specific = {"model", "tokens_in", "tokens_out", "cost_usd", "prompt", "text", "args", "result", "op",
                    "value", "outputs", "did_work"}
        wrong = [f for f in specific - allowed if getattr(self, f) is not None]
        if wrong:
            raise ValueError(f"a {self.kind} step doesn't take {', '.join(sorted(wrong))}")
        if self.kind in ("tool", "stage", "state") and not self.name:
            raise ValueError(f"a {self.kind} step needs a name")
        return self


class RunEnd(_E):
    type: Literal["run.end"]
    run_id: str = Field(..., max_length=128)
    status: Literal["completed", "failed", "abandoned"] = "completed"
    answer: Optional[str] = Field(None, max_length=32768)
    error: Optional[str] = Field(None, max_length=2048)


class Feedback(_E):
    type: Literal["feedback"]
    run_id: str = Field(..., max_length=128)
    kind: Literal["thumbs_up", "thumbs_down", "retry", "escalation", "complaint"]
    note: Optional[str] = Field(None, max_length=1024)


class Check(_E):
    type: Literal["check"]
    test: Test
    status: Literal["pass", "fail", "error"]
    run_id: Optional[str] = Field(None, max_length=128, description="The run that produced the output")
    field: Optional[str] = Field(None, max_length=256)
    expected: Optional[str] = Field(None, max_length=4096)
    actual: Optional[str] = Field(None, max_length=4096)
    evaluator: Optional[str] = Field(None, max_length=128)
    score: Optional[float] = None
    reason: Optional[str] = Field(None, max_length=2048)
    version: Optional[Dict[str, str]] = None


class Correction(_E):
    type: Literal["correction"]
    run_id: str = Field(..., max_length=128)
    field: str = Field(..., max_length=256)
    expected: Optional[str] = Field(None, max_length=4096)
    observed: Optional[str] = Field(None, max_length=4096)
    kind: Literal["wrong", "missing", "extra"] = "wrong"
    reporter: Optional[str] = Field(None, max_length=128)


class Expect(_E):
    type: Literal["expect"]
    case: str = Field(..., max_length=128)
    calls: List[Dict[str, Any]] = Field(default_factory=list)
    allow_extra: List[str] = Field(default_factory=list)
    answer: Optional[str] = None
    answer_match: Literal["contains", "equals"] = "contains"
    state: List[Dict[str, Any]] = Field(default_factory=list)
    max_steps: Optional[int] = Field(None, ge=1)


Event = Annotated[Union[RunStart, Step, RunEnd, Feedback, Check, Correction, Expect], Field(discriminator="type")]
EVENTS = TypeAdapter(List[Event])


def json_schema() -> dict:
    s = EVENTS.json_schema()
    s["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    s["$id"] = "https://assay.dev/schema/events-v1.json"
    s["title"] = "Assay events, v1"
    s["description"] = "A list of events. See docs/event-schema.md."
    return s


# ---------- landing events in Assay's tables ----------

def _upsert(conn, engine: Engine, table, rows: List[dict], keys: List[str]) -> None:
    if not rows:
        return
    ins = sqlite_insert if engine.dialect.name == "sqlite" else pg_insert
    by_cols: Dict[tuple, List[dict]] = {}
    for r in rows:
        by_cols.setdefault(tuple(sorted(r)), []).append(r)
    for cols, batch in by_cols.items():
        stmt = ins(table)
        updates = {c: stmt.excluded[c] for c in cols if c not in keys}
        stmt = stmt.on_conflict_do_update(index_elements=keys, set_=updates) if updates \
            else stmt.on_conflict_do_nothing(index_elements=keys)
        conn.execute(stmt, batch)


def _split(prompt: Optional[str]):
    if not prompt:
        return None, None
    pid, _, ver = prompt.partition("@")
    return pid or None, ver or None


ORDER = {"run.start": 0, "step": 1, "run.end": 2}


def ingest(engine: Engine, events: List[BaseModel], tenant: str) -> Dict[str, int]:
    """Write events; returns counts by type. Idempotent: resending the same events changes nothing."""
    from assay.ingest import discover_prompts
    events = sorted(events, key=lambda e: ORDER.get(e.type, 3))
    counts: Dict[str, int] = {}
    runs_t, heads, steps_t = store.runs, store.agent_trajectories, store.agent_steps
    with engine.begin() as conn:
        run_ids = {getattr(e, "run_id", None) for e in events} - {None}
        known = {r.run_id: dict(r._mapping) for r in conn.execute(
            select(runs_t).where(and_(runs_t.c.tenant == tenant, runs_t.c.run_id.in_(run_ids))))} if run_ids else {}
        rows = {k: [] for k in ("runs", "docs", "inputs", "heads", "steps", "stages", "calls", "feedback", "checks",
                                "errors", "refs")}
        for e in events:
            counts[e.type] = counts.get(e.type, 0) + 1
            if isinstance(e, RunStart):
                run = {"tenant": tenant, "run_id": e.run_id, "kind": e.kind, "task": e.task, "segment": e.segment,
                       "started_at": e.ts, "status": "running", "version": e.version,
                       "test_run": e.test.run if e.test else None, "test_case": e.test.case if e.test else None,
                       "attempt": e.test.attempt if e.test else None, "parent_run_id": e.parent_run_id, "tags": e.tags}
                known[e.run_id] = run
                rows["runs"].append(run)
                rows["docs"].append({"tenant": tenant, "document_id": e.run_id, "received_at": e.ts,
                                     "status": "running", "document_type": e.task, "segment": e.segment})
                if e.input is not None or e.input_ref:
                    rows["inputs"].append({"tenant": tenant, "trace_id": e.run_id, "input": e.input,
                                           "input_ref": e.input_ref, "captured_at": e.ts})
                if e.kind == "agent":
                    rows["heads"].append(_head(run))
            elif isinstance(e, Step):
                run = known.get(e.run_id)
                if run is None:  # a step before its run.start: open the run from what we know
                    run = {"tenant": tenant, "run_id": e.run_id, "kind": "pipeline" if e.kind == "stage" else "agent",
                           "started_at": e.ts, "status": "running"}
                    known[e.run_id] = run
                    rows["runs"].append(run)
                    rows["docs"].append({"tenant": tenant, "document_id": e.run_id, "received_at": e.ts,
                                         "status": "running"})
                    if run["kind"] == "agent":
                        rows["heads"].append(_head(run))
                pid, ver = _split(e.prompt)
                status = "success" if e.status == "ok" else "failed"
                if e.kind == "stage":
                    rows["stages"].append({"tenant": tenant, "run_id": f"{e.run_id}#{e.seq}", "document_id": e.run_id,
                                           "stage": e.name, "status": status, "started_at": e.ts,
                                           "finished_at": e.ended_at, "did_work": e.did_work, "outputs": e.outputs,
                                           "sequence": e.seq, "prompt_id": pid, "prompt_version": ver})
                elif e.kind == "llm" and run.get("kind") == "pipeline":
                    rows["calls"].append({"tenant": tenant, "call_id": f"{e.run_id}#{e.seq}", "stage": e.name or "llm",
                                          "ts": e.ts, "document_id": e.run_id, "model_declared": e.model,
                                          "model_served": e.model, "cost_usd": e.cost_usd,
                                          "latency_ms": (e.ended_at - e.ts).total_seconds() * 1000 if e.ended_at else None,
                                          "status": "success" if e.status == "ok" else "error", "prompt_id": pid,
                                          "prompt_version": ver, "code_revision": (run.get("version") or {}).get("build"),
                                          "segment": run.get("segment"), "document_type": run.get("task")})
                else:
                    tokens = (e.tokens_in or 0) + (e.tokens_out or 0)
                    rows["steps"].append({
                        "tenant": tenant, "trajectory_id": e.run_id, "seq": e.seq,
                        "kind": "reason" if e.kind == "llm" else e.kind, "name": e.name,
                        "args": {"op": e.op or "update"} if e.kind == "state" else e.args,
                        "result": e.value if e.kind == "state" else e.result,
                        "error": e.error if e.status == "error" else None, "text": e.text, "model": e.model,
                        "tokens": tokens or None, "cost_usd": e.cost_usd, "started_at": e.ts,
                        "finished_at": e.ended_at, "parent_seq": e.parent_seq})
            elif isinstance(e, RunEnd):
                run = known.setdefault(e.run_id, {"tenant": tenant, "run_id": e.run_id, "kind": "agent",
                                                  "started_at": e.ts})
                run.update(ended_at=e.ts, status=e.status, answer=e.answer, error=e.error)
                rows["runs"].append({k: v for k, v in run.items()})
                rows["docs"].append({"tenant": tenant, "document_id": e.run_id,
                                     "received_at": run.get("started_at") or e.ts,
                                     "completed_at": e.ts if e.status == "completed" else None, "status": e.status})
                if run.get("kind", "agent") == "agent":
                    head = _head(run)
                    head.update(finished_at=e.ts, status=e.status)
                    if e.answer is not None:
                        head["answer"] = e.answer
                    rows["heads"].append(head)
            elif isinstance(e, Feedback):
                rows["feedback"].append({"tenant": tenant, "feedback_id": e.id, "trace_id": e.run_id, "kind": e.kind,
                                         "ts": e.ts, "note": e.note})
            elif isinstance(e, Check):
                rows["checks"].append({"tenant": tenant, "result_id": e.id, "run_id": e.test.run,
                                       "case_id": e.test.case, "attempt": e.test.attempt, "document_id": e.run_id,
                                       "field": e.field, "expected": e.expected, "actual": e.actual,
                                       "status": e.status, "evaluator": e.evaluator, "score": e.score,
                                       "reason": e.reason, "ts": e.ts,
                                       "lineage": e.version or (known.get(e.run_id) or {}).get("version")})
            elif isinstance(e, Correction):
                rows["errors"].append({"tenant": tenant, "error_id": e.id, "document_id": e.run_id, "field": e.field,
                                       "reported_at": e.ts, "expected": e.expected, "observed": e.observed,
                                       "kind": e.kind, "reporter": e.reporter, "source": "correction"})
            elif isinstance(e, Expect):
                rows["refs"].append({"tenant": tenant, "case_id": e.case, "calls": e.calls,
                                     "allow_extra": e.allow_extra, "answer": e.answer, "answer_match": e.answer_match,
                                     "state": e.state or None, "max_steps": e.max_steps, "updated_at": e.ts})
        _upsert(conn, engine, runs_t, _merge(rows["runs"], "run_id"), ["tenant", "run_id"])
        _upsert(conn, engine, store.event_documents, _merge(rows["docs"], "document_id"), ["tenant", "document_id"])
        _upsert(conn, engine, store.trace_inputs, rows["inputs"], ["tenant", "trace_id"])
        _upsert(conn, engine, heads, _merge(rows["heads"], "trajectory_id"), ["tenant", "trajectory_id"])
        _upsert(conn, engine, steps_t, rows["steps"], ["tenant", "trajectory_id", "seq"])
        _upsert(conn, engine, store.event_stage_runs, rows["stages"], ["tenant", "run_id"])
        _upsert(conn, engine, store.event_calls, rows["calls"], ["tenant", "call_id"])
        _upsert(conn, engine, store.trace_feedback, rows["feedback"], ["tenant", "feedback_id"])
        _upsert(conn, engine, store.eval_results, rows["checks"], ["tenant", "result_id"])
        _upsert(conn, engine, store.event_errors, rows["errors"], ["tenant", "error_id"])
        _upsert(conn, engine, store.agent_references, rows["refs"], ["tenant", "case_id"])
        # Server time of the latest event, per trajectory: when to evaluate again (assay/lifecycle.py).
        touched = {r["trajectory_id"] for r in rows["heads"]} | {r["trajectory_id"] for r in rows["steps"]}
        if touched:
            conn.execute(heads.update().where(and_(heads.c.tenant == tenant, heads.c.trajectory_id.in_(list(touched))))
                         .values(updated_at=datetime.utcnow()))
        # An answer step is the run's answer unless run.end said otherwise.
        answered = {r["trajectory_id"]: r["text"] for r in sorted(rows["steps"], key=lambda r: r["seq"])
                    if r["kind"] == "answer"}
        for rid, text in answered.items():
            conn.execute(heads.update().where(and_(heads.c.tenant == tenant, heads.c.trajectory_id == rid,
                                                   heads.c.answer.is_(None))).values(answer=text))
    prompts = [{"prompt_id": s["prompt_id"], "prompt_version": s["prompt_version"], "started_at": s["started_at"]}
               for s in rows["stages"] if s["prompt_id"]]
    if prompts:
        discover_prompts(engine, prompts, tenant, "started_at")
    return counts


def _head(run: dict) -> dict:
    return {"tenant": run["tenant"], "trajectory_id": run["run_id"], "run_id": run.get("test_run"),
            "case_id": run.get("test_case"), "attempt": run.get("attempt"), "task": run.get("task"),
            "started_at": run.get("started_at"), "status": run.get("status") or "running",
            "lineage": run.get("version")}


def _merge(rows: List[dict], key: str) -> List[dict]:
    """Several rows for the same record in one batch (run.start then run.end): later values win,
    missing ones don't erase earlier ones."""
    out: Dict[str, dict] = {}
    for r in rows:
        cur = out.setdefault(r[key], {})
        cur.update({k: v for k, v in r.items() if v is not None or k not in cur})
    return list(out.values())
