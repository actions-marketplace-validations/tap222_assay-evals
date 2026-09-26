"""Where Assay's evaluation connects to a pipeline: the other half of the workflow diagram.

Every pipeline is its own: its steps come from what documents did (assay/workflow.py). This says,
for that pipeline, how its data reaches Assay (read from a database, or sent as events), what
Assay evaluates at each step, and the parts of the evaluation that sit off the path:

  steps       per step: which of its outputs are checked (evaluation results on a field the step
              produces), the path contracts that mention it, the measures sliced by it, its prompt
              versions, and the reported errors that started there.
  components  checks on outputs, path contracts, measures, reported errors, prompt versions,
              agent runs and release gates: each with what it covers, how it's doing, and the
              steps it connects to (none when it's about the release as a whole). One that has
              nothing yet says what to send to switch it on.

Assay only reads: none of this changes what the pipeline does.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from typing import Dict, List, Optional

from sqlalchemy import and_, desc, func, select
from sqlalchemy.engine import Engine

from assay import contracts, store
from assay.models import Window

STAGE_DIMENSIONS = ("stage", "origin_stage")


def _tenant(source_name: str) -> Optional[str]:
    return source_name.split(":", 1)[1] if source_name.startswith("events:") else None


def _connection(engine: Engine, source_name: str, graph: dict, settings=None) -> dict:
    tenant = _tenant(source_name)
    if tenant is None:
        dialect = (getattr(settings, "source_url", None) or "").split(":", 1)[0].split("+", 1)[0] or "your database"
        return {"kind": "database", "title": f"Reads {dialect}", "detail": "through a mapping, read-only; never writes back"}
    counts = {}
    with engine.connect() as conn:
        for key, table in (("step runs", store.event_stage_runs), ("model calls", store.event_calls),
                           ("agent runs", store.agent_trajectories), ("evaluation results", store.eval_results)):
            counts[key] = conn.execute(select(func.count()).select_from(table).where(table.c.tenant == tenant)).scalar() or 0
    have = [f"{v:,} {k}" for k, v in counts.items() if v]
    return {"kind": "events", "title": "Sent to Assay",
            "detail": "with the SDK, the API or OpenTelemetry" + (f": {' · '.join(have)}" if have else "")}


def _stage_fields(source, window: Window) -> Dict[str, set]:
    """The fields each step produced (its outputs), from recent step runs."""
    out: Dict[str, set] = defaultdict(set)
    try:
        runs = list(source.stage_runs(window) or [])
    except Exception:  # a source without step outputs
        return out
    for r in runs[-5000:]:
        for k in (getattr(r, "outputs", None) or {}):
            if not str(k).startswith("_"):
                out[r.stage].add(str(k))
    return out


def _checked_fields(engine: Engine, tenant: Optional[str]) -> dict:
    """Fields evaluation results check, with their evaluators and the latest run."""
    if tenant is None:
        return {"fields": {}, "runs": 0, "latest": None, "results": 0}
    t = store.eval_results
    with engine.connect() as conn:
        rows = conn.execute(select(t.c.field, t.c.evaluator, t.c.run_id, t.c.status, t.c.ts)
                            .where(t.c.tenant == tenant)).all()
    fields: Dict[str, set] = defaultdict(set)
    runs, latest = set(), None
    for r in rows:
        if r.field:
            fields[r.field].add(r.evaluator or "unnamed")
        runs.add(r.run_id)
        if latest is None or r.ts > latest[1]:
            latest = (r.run_id, r.ts)
    return {"fields": fields, "runs": len(runs - {"baseline"}), "latest": latest and latest[0], "results": len(rows)}


def _stage_measures(engine: Engine, source_name: str) -> tuple:
    """({stage: measures sliced by it}, measured, total, dimensions) from the latest measure run."""
    mr, res = store.measure_runs, store.measure_results
    with engine.connect() as conn:
        run = conn.execute(select(mr.c.id).where(mr.c.source == source_name).order_by(desc(mr.c.id)).limit(1)).scalar()
        if run is None:
            return {}, 0, 0, []
        rows = conn.execute(select(res.c.measure_id, res.c.status, res.c.dimension, res.c.slice_value)
                            .where(res.c.run_id == run)).all()
    by_stage: Dict[str, set] = defaultdict(set)
    overall = {r.measure_id: r.status for r in rows if r.dimension is None}
    for r in rows:
        if r.dimension in STAGE_DIMENSIONS and r.status == "measured" and r.slice_value:
            by_stage[r.slice_value].add(r.measure_id)
    dims = sorted({r.dimension for r in rows if r.dimension})
    return by_stage, sum(1 for s in overall.values() if s == "measured"), len(overall), dims


def _mentions(c: dict) -> List[str]:
    return [s for s in [c.get("step"), c.get("other"), *(c.get("steps") or [])] if s]


def describe(engine: Engine, source, source_name: str, window: Window, graph: dict, settings=None) -> dict:
    tenant = _tenant(source_name)
    stages = [n for n in graph["nodes"] if n.get("kind") not in ("start", "end")]
    ids = {n["id"] for n in stages}
    produced = _stage_fields(source, window)
    checked = _checked_fields(engine, tenant)
    measures, measured, total, dims = _stage_measures(engine, source_name)
    rules = contracts.load(engine, source_name)
    steps = {}
    for n in stages:
        sid = n["id"]
        fields = sorted(produced.get(sid, set()) & set(checked["fields"]))
        steps[sid] = {"checked_fields": fields, "measures": sorted(measures.get(sid, set())),
                      "contracts": [contracts.describe(c) for c in rules if sid in _mentions(c)],
                      "prompts": n.get("prompts") or [], "errors": n.get("errors") or 0}
    at = lambda pred: [sid for sid, s in steps.items() if pred(s)]
    comps = []

    def comp(kind, title, detail, status, tone, where, hint=None):
        comps.append({"kind": kind, "title": title, "detail": detail, "status": status, "tone": tone,
                      "steps": where, **({"hint": hint} if hint else {})})
    # Checks on what the steps produce.
    n_fields = len(checked["fields"])
    on_steps = at(lambda s: s["checked_fields"])
    if checked["results"]:
        evs = sorted({e for es in checked["fields"].values() for e in es})
        comp("checks", "Checks on outputs", f"{n_fields} field{'s' * (n_fields != 1)} · {len(evs)} evaluator"
             f"{'s' * (len(evs) != 1)} · {checked['runs']} run{'s' * (checked['runs'] != 1)}",
             f"latest {checked['latest']}", "good", on_steps)
    else:
        comp("checks", "Checks on outputs", "results for the fields your steps produce", "none yet", "neutral", [],
             "send them with assay.check() or POST /v1/events/eval-results")
    # Path contracts.
    broken = [n for n in graph["nodes"] if n.get("contract_violations")]
    comp("contracts", "Path contracts", f"{len(rules)} rule{'s' * (len(rules) != 1)} on which steps may run, in what order",
         f"{len(broken)} broken" if broken else ("all hold" if rules else "none yet"),
         "bad" if broken else "good" if rules else "neutral",
         sorted({s for c in rules for s in _mentions(c) if s in ids}),
         None if rules else "write rules, or confirm suggested ones, under Path contracts below")
    # Measures, sliced by step.
    alerting = [n["id"] for n in stages if n.get("open_alerts")]
    comp("measures", "Measures", f"{measured} of {total} measured · sliced by {', '.join(d.replace('_', ' ') for d in dims[:4]) or 'nothing yet'}"
         if total else "failure rates, latency, cost and drift, per step and per slice",
         f"{len(alerting)} step{'s' * (len(alerting) != 1)} alerting" if alerting else ("no alerts" if total else "not run yet"),
         "warn" if alerting else "good" if total else "neutral", at(lambda s: s["measures"]),
         None if total else "Run now, or schedule runs (ASSAY_SCHEDULE_MINUTES)")
    # Reported errors, traced to the step they started at.
    errs = sum(s["errors"] for s in steps.values())
    top = max(steps.items(), key=lambda kv: kv[1]["errors"], default=(None, {"errors": 0}))
    comp("errors", "Reported errors", "wrong values reviewers and customers found, traced to the step they started at",
         f"{errs:,} · most at {top[0].replace('_', ' ')}" if errs else "none reported",
         "warn" if errs else "neutral", at(lambda s: s["errors"]),
         None if errs else "send corrections with assay.correction() or POST /v1/events/errors")
    # Prompt versions.
    regress = [n["id"] for n in stages if n.get("regression")]
    with_prompts = at(lambda s: s["prompts"])
    comp("prompts", "Prompt versions", f"{sum(len(s['prompts']) for s in steps.values())} versions on "
         f"{len(with_prompts)} step{'s' * (len(with_prompts) != 1)}, compared release to release",
         f"{len(regress)} regressed" if regress else ("no regressions" if with_prompts else "none recorded"),
         "bad" if regress else "good" if with_prompts else "neutral", with_prompts,
         None if with_prompts else "record prompt=\"id@version\" on model calls")
    # Agent runs, if the source has any.
    if tenant is not None:
        from assay import lifecycle
        life = lifecycle.overview(engine, tenant, limit=1)
        n_runs = life["running"] + life["awaiting_evaluation"] + life["evaluated"]
        if n_runs:
            comp("agents", "Agent runs", f"{n_runs:,} runs · trajectory, tools, contracts, PII, injection, plan",
                 f"{life['failing']:,} failing" if life["failing"] else "none failing",
                 "bad" if life["failing"] else "good", [])
    # Release gates: about the release, not a step.
    g = store.gate_decisions
    with engine.connect() as conn:
        q = select(g.c.outcome, g.c.created_at).order_by(desc(g.c.id)).limit(1)
        if tenant is not None:
            q = q.where(g.c.tenant.in_([tenant, "*"]))
        last = conn.execute(q).first()
    comp("gates", "Release gates", "advance, hold or roll back on these results, not on infrastructure health",
         f"last: {last.outcome}" if last else "no decisions yet",
         {"advance": "good", "hold": "warn", "rerun": "warn", "rollback": "bad"}.get(last.outcome, "neutral") if last else "neutral",
         [], None if last else "POST /v1/gates/evaluate from your release pipeline")
    return {"connection": _connection(engine, source_name, graph, settings), "steps": steps, "components": comps,
            "ledger": {"measures": total, "dimensions": dims}}
