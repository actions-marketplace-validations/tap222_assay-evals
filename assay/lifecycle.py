"""The lifecycle of an agent run: evaluate it when it ends, not on a timer.

An agent can take ten seconds or five minutes. Evaluating after a fixed delay
judges the slow ones half-way. So a run moves through states instead:

  running    run.start (or its first step) arrived; run.end hasn't
  ended      run.end said completed or failed, or the run went quiet for
             ABANDON_MINUTES and is marked abandoned (the process died)
  evaluated  checked once it ended, and once its child runs ended too
  again      events arriving after the evaluation (a late step) queue it again

Nothing is queued explicitly: a run needs evaluating when it has ended and has
no evaluation newer than its latest event (agent_trajectories.updated_at, stamped
at ingest). A missed trigger can't lose a run; the sweep finds it.

Every run gets the checks that need no reference: it finished, it kept the
critical path contracts, it didn't loop, and no tool error went unrecovered. A
test-case run also gets its case's checks (assay/agents.py), stored as its test
run's results, so an evaluation run fills in as its cases finish.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Iterable, List, Optional

from sqlalchemy import and_, func, or_, select
from sqlalchemy.engine import Engine

from assay import agents, contracts, ingest, store
from assay.sources.events import EventsSource

log = logging.getLogger(__name__)

ABANDON_MINUTES = 30.0
BATCH = 500  # runs evaluated per pass
CHECKS = ("completed", "safety", "loops", "tool_errors")


def _heads():
    return store.agent_trajectories


def _last_event(t):
    return func.coalesce(t.c.updated_at, t.c.finished_at, t.c.started_at)


def pending(engine: Engine, tenant: Optional[str] = None, ids: Optional[Iterable[str]] = None,
            limit: int = BATCH) -> List[dict]:
    """Ended runs with no evaluation newer than their latest event, and no child still running."""
    t, rc, runs = _heads(), store.run_checks, store.runs
    child = runs.alias("child")
    running_child = select(child.c.run_id).where(and_(child.c.tenant == t.c.tenant,
                                                      child.c.parent_run_id == t.c.trajectory_id,
                                                      child.c.status == "running")).exists()
    q = (select(t.c.tenant, t.c.trajectory_id, t.c.run_id, t.c.case_id, t.c.attempt, t.c.lineage, t.c.started_at,
                t.c.task, t.c.status)
         .select_from(t.outerjoin(rc, and_(rc.c.tenant == t.c.tenant, rc.c.trajectory_id == t.c.trajectory_id)))
         .where(and_(or_(t.c.status.is_(None), t.c.status != "running"),
                     or_(rc.c.evaluated_at.is_(None), rc.c.evaluated_at < _last_event(t)),
                     ~running_child))
         .order_by(_last_event(t)).limit(limit))
    if tenant is not None:
        q = q.where(t.c.tenant == tenant)
    if ids is not None:
        ids = list(ids)
        if not ids:
            return []
        q = q.where(t.c.trajectory_id.in_(ids))
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(q)]


def run_checks(traj: dict, rules: List[dict], abandon_minutes: float = ABANDON_MINUTES,
               abandoned_why: Optional[str] = None) -> List[dict]:
    """The checks every run gets, reference or not. [{"check", "status", "reason"}]"""
    ev = agents.evaluate_one(traj, None, rules)
    out = []

    def add(check, ok, reason=None):
        out.append({"check": check, "status": "pass" if ok else "fail", "reason": None if ok else reason})
    status = traj.get("status")
    last = max((s["seq"] for s in traj["steps"]), default=None)
    where = f" after step {last}" if last is not None else " before any step"
    add("completed", status in (None, "completed"),
        f"Never finished{where}: {abandoned_why or f'no events for {abandon_minutes:g} minutes'}."
        if status == "abandoned" else
        f"Failed{where}: {traj.get('error') or 'no error given'}.")
    if rules:
        crit = [b for b in ev["safety"] if b["severity"] == "critical"]
        add("safety", not crit, "; ".join(f"Broke “{b['label']}”: {b['detail']}" for b in crit))
    e = ev["efficiency"]
    add("loops", e["loop_at"] is None, f"Looped: {e['loop_call']} {e['max_identical']} times, from step {e['loop_at']}.")
    unrecovered = e["tool_errors"] - e["recovered"]
    failed_calls = [s for s in traj["steps"] if s["kind"] == "tool" and s.get("error")]
    add("tool_errors", unrecovered <= 0,
        f"{unrecovered} tool error{'s' * (unrecovered != 1)} nothing recovered: "
        + "; ".join(f"{s['name']} (step {s['seq']}): {s['error']}" for s in failed_calls[:3]))
    return out


def evaluate(engine: Engine, heads: List[dict], abandon_minutes: float = ABANDON_MINUTES,
             abandoned_why: Optional[str] = None) -> int:
    """Evaluate these ended runs and store the results. Returns how many were evaluated."""
    now = datetime.utcnow()
    done = 0
    by_tenant: Dict[str, List[dict]] = defaultdict(list)
    for h in heads:
        by_tenant[h["tenant"]].append(h)
    for tenant, hs in by_tenant.items():
        source = EventsSource(engine, tenant)
        rules = contracts.load(engine, source.name)
        trajs = source.trajectories([h["trajectory_id"] for h in hs])
        with engine.connect() as conn:  # why a failed run failed is on the run
            errors = dict(conn.execute(select(store.runs.c.run_id, store.runs.c.error).where(
                and_(store.runs.c.tenant == tenant, store.runs.c.run_id.in_(list(trajs)),
                     store.runs.c.error.is_not(None)))).all())
        for tid, traj in trajs.items():
            traj["error"] = errors.get(tid)
        refs = agents.references(engine, tenant, {h["case_id"] for h in hs if h["case_id"]})
        checks, results = [], []
        for h in hs:
            traj = trajs.get(h["trajectory_id"])
            if traj is None:
                continue
            found = run_checks(traj, rules, abandon_minutes, abandoned_why)
            if h["run_id"]:  # a test case: its case's checks too, as results of its test run
                done_ = found[0]  # "completed", so a run that never finished shows in the test run
                case = [{"field": "completed", "status": done_["status"], "expected": "the run finishes",
                         "actual": h["status"] or "completed", "reason": done_["reason"]}]
                case += agents.checks_for(traj, refs.get(h["case_id"]), rules)
                results += agents.result_rows(tenant, h["run_id"], h, case)
                found += [{"check": c["field"], "status": c["status"], "reason": c["reason"]} for c in case]
            checks.append({"tenant": tenant, "trajectory_id": h["trajectory_id"], "evaluated_at": now,
                           "status": h["status"], "checks": found,
                           "failed": sum(1 for c in found if c["status"] == "fail")})
        ingest.upsert(engine, store.eval_results, results, "result_id")
        with engine.begin() as conn:
            for row in checks:
                conn.execute(store.run_checks.delete().where(and_(store.run_checks.c.tenant == row["tenant"],
                                                                  store.run_checks.c.trajectory_id ==
                                                                  row["trajectory_id"])))
            if checks:
                conn.execute(store.run_checks.insert(), checks)
        done += len(checks)
    return done


def abandon(engine: Engine, minutes: float = ABANDON_MINUTES, tenant: Optional[str] = None,
            ids: Optional[Iterable[str]] = None, now: Optional[datetime] = None) -> int:
    """Mark running runs abandoned: quiet for `minutes`, or (ids) known to be over, e.g. the
    process that ran them has exited. Returns how many."""
    t = _heads()
    now = now or datetime.utcnow()
    cond = [t.c.status == "running"]
    if ids is not None:
        cond.append(t.c.trajectory_id.in_(list(ids)))
    else:
        cond.append(_last_event(t) < now - timedelta(minutes=minutes))
    if tenant is not None:
        cond.append(t.c.tenant == tenant)
    with engine.begin() as conn:
        rows = conn.execute(select(t.c.tenant, t.c.trajectory_id, _last_event(t).label("last"))
                            .where(and_(*cond))).all()
        for r in rows:
            key = lambda tbl, col: and_(tbl.c.tenant == r.tenant, col == r.trajectory_id)
            # finished_at is the last sign of life; updated_at moves, so it's evaluated after this.
            conn.execute(t.update().where(key(t, t.c.trajectory_id))
                         .values(status="abandoned", finished_at=r.last, updated_at=now))
            conn.execute(store.runs.update().where(key(store.runs, store.runs.c.run_id))
                         .values(status="abandoned", ended_at=r.last))
            conn.execute(store.event_documents.update()
                         .where(key(store.event_documents, store.event_documents.c.document_id))
                         .values(status="abandoned"))
    return len(rows)


def after_ingest(engine: Engine, tenant: str, run_ids: Iterable[str],
                 abandon_minutes: float = ABANDON_MINUTES) -> int:
    """Evaluate the runs a batch just ended (or added late data to), and their parents: a parent
    waits for its children. Never raises: ingest mustn't fail because an evaluation did."""
    try:
        ids = set(run_ids)
        if not ids:
            return 0
        with engine.connect() as conn:
            parents = {r.parent_run_id for r in conn.execute(
                select(store.runs.c.parent_run_id).where(and_(store.runs.c.tenant == tenant,
                                                              store.runs.c.run_id.in_(list(ids)),
                                                              store.runs.c.parent_run_id.is_not(None))))}
        return evaluate(engine, pending(engine, tenant, ids | parents), abandon_minutes)
    except Exception:
        log.exception("Evaluating runs after ingest failed; the sweep will retry them")
        return 0


def sweep(engine: Engine, abandon_minutes: float = ABANDON_MINUTES) -> dict:
    """Mark quiet runs abandoned, then evaluate everything pending."""
    out = {"abandoned": abandon(engine, abandon_minutes), "evaluated": 0}
    while True:
        batch = pending(engine)
        if not batch:
            return out
        n = evaluate(engine, batch, abandon_minutes)
        out["evaluated"] += n
        if n < len(batch) or len(batch) < BATCH:
            return out


def overview(engine: Engine, tenant: str, limit: int = 50) -> dict:
    """Where the runs are in their lifecycle, and the latest that failed a check."""
    t, rc = _heads(), store.run_checks
    joined = t.outerjoin(rc, and_(rc.c.tenant == t.c.tenant, rc.c.trajectory_id == t.c.trajectory_id))
    with engine.connect() as conn:
        rows = conn.execute(select(t.c.status, rc.c.evaluated_at, rc.c.failed, _last_event(t).label("last"))
                            .select_from(joined).where(t.c.tenant == tenant)).all()
        failing = conn.execute(select(t.c.trajectory_id, t.c.task, t.c.status, rc.c.evaluated_at, rc.c.checks)
                               .select_from(joined).where(and_(t.c.tenant == tenant, rc.c.failed > 0))
                               .order_by(rc.c.evaluated_at.desc()).limit(limit)).all()
    counts = {"running": 0, "awaiting_evaluation": 0, "evaluated": 0, "failing": 0, "abandoned": 0}
    for r in rows:
        if r.status == "running":
            counts["running"] += 1
            continue
        counts["abandoned"] += r.status == "abandoned"
        if r.evaluated_at is None or r.evaluated_at < r.last:
            counts["awaiting_evaluation"] += 1
        else:
            counts["evaluated"] += 1
            counts["failing"] += (r.failed or 0) > 0
    return {**counts, "recent_failures": [
        {"trajectory_id": r.trajectory_id, "task": r.task, "status": r.status,
         "evaluated_at": r.evaluated_at.isoformat(),
         "failed": [c for c in r.checks if c["status"] == "fail"]} for r in failing]}


def get(engine: Engine, tenant: str, trajectory_id: str) -> Optional[dict]:
    rc = store.run_checks
    with engine.connect() as conn:
        r = conn.execute(select(rc).where(and_(rc.c.tenant == tenant, rc.c.trajectory_id == trajectory_id))).first()
    if r is None:
        return None
    return {"evaluated_at": r.evaluated_at.isoformat(), "status": r.status, "checks": r.checks, "failed": r.failed}
