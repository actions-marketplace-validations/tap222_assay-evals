"""The pipeline as a graph, inferred from what documents actually did.

Nobody has to draw the pipeline: each document's steps, in order, give its
path; paths across many documents give the steps (nodes), how documents move
between them (edges, with counts, so branches and skipped steps show up), and
each step's position. Every node carries its health: volume, failures, steps
that did nothing, latency, cost, fallback, the prompt it runs, open alerts,
and the reported errors that started there. A start node counts documents
received; an end node counts completed, still open, and errors that happened
after the pipeline.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from statistics import median
from typing import Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store
from assay.cost import is_fallback
from assay.measures.base import quantile
from assay.models import FAILED_STATUSES, Window
from assay.rootcause import order_steps, summarize

START, END = "(received)", "(done)"
MAX_DOCS = 20000  # documents sampled to infer paths


def _health(n: dict) -> str:
    if n["open_alerts"] or n["failure_rate"] > 0.05 or n["regression"]:
        return "bad"
    if n["errors"] or n["noop_rate"] > 0.5 or n["failure_rate"] > 0.01 or (n["fallback_rate"] or 0) > 0.1:
        return "warn"
    return "ok"


def build(source, window: Window, engine: Optional[Engine] = None, source_name: Optional[str] = None) -> dict:
    runs = list(source.stage_runs(window) or [])
    calls = list(source.calls(window) or [])
    docs = {d.document_id: d for d in (source.documents(window) or [])}

    by_doc = defaultdict(list)
    for r in runs:
        by_doc[r.document_id].append(r)
    sampled = list(by_doc)[:MAX_DOCS]

    node = defaultdict(lambda: {"runs": 0, "docs": set(), "failed": 0, "noop": 0, "durations": [], "positions": [],
                                "prompts": Counter(), "failed_docs": []})
    edges = Counter()
    for doc_id in sampled:
        path = order_steps(by_doc[doc_id])
        prev = START
        for i, r in enumerate(path):
            n = node[r.stage]
            n["runs"] += 1
            n["docs"].add(doc_id)
            n["positions"].append(i)
            if r.status in FAILED_STATUSES:
                n["failed"] += 1
                if len(n["failed_docs"]) < 25:
                    n["failed_docs"].append({"document_id": doc_id, "status": r.status,
                                             "at": r.started_at.isoformat() if r.started_at else None})
            n["noop"] += r.did_work is False
            if r.started_at and r.finished_at:
                n["durations"].append((r.finished_at - r.started_at).total_seconds())
            if r.prompt:
                n["prompts"][r.prompt] += 1
            if prev != r.stage:  # a repeated step (retry) isn't a new edge
                edges[(prev, r.stage)] += 1
            prev = r.stage
        edges[(prev, END)] += 1

    call_stats = defaultdict(lambda: {"calls": 0, "cost": 0.0, "priced": 0, "fallback": 0, "models": Counter(),
                                      "prompts": Counter(), "latencies": []})
    for c in calls:
        s = call_stats[c.stage]
        s["calls"] += 1
        if c.cost_usd is not None:
            s["cost"] += c.cost_usd
            s["priced"] += 1
        s["fallback"] += is_fallback(c)
        if c.model_served:
            s["models"][c.model_served] += 1
        if c.prompt:
            s["prompts"][c.prompt] += 1
        if c.latency_ms is not None:
            s["latencies"].append(c.latency_ms)

    errs = summarize(source, window, include_all=True) if hasattr(source, "errors") else None
    err_by_stage = defaultdict(Counter)
    for r in (errs or {}).get("recent_all", []):
        err_by_stage[r["origin_stage"] or END][r["verdict"]] += 1

    open_alerts, regressions = defaultdict(list), set()
    if engine is not None and source_name:
        a = store.alerts
        with engine.connect() as conn:
            for row in conn.execute(select(a).where(and_(a.c.source == source_name, a.c.state == "open"))):
                if row.kind == "regression":
                    regressions.add(row.slice_value)
                if row.dimension in ("stage", "origin_stage") and row.slice_value:
                    open_alerts[row.slice_value].append({"id": row.id, "message": row.message, "kind": row.kind,
                                                         "measure_id": row.measure_id})

    nodes = []
    for stage, n in node.items():
        cs = call_stats.get(stage)
        prompts = (cs["prompts"] if cs and cs["prompts"] else n["prompts"]).most_common()
        live_prompt = prompts[0][0] if prompts else None
        entry = {
            "id": stage, "position": median(n["positions"]), "runs": n["runs"], "documents": len(n["docs"]),
            "failure_rate": n["failed"] / n["runs"], "failed": n["failed"],
            "noop_rate": n["noop"] / n["runs"],
            "duration_p95_s": quantile(n["durations"], 0.95) if n["durations"] else None,
            "calls": cs["calls"] if cs else 0,
            "cost_usd": cs["cost"] if cs else 0.0, "cost_per_document": (cs["cost"] / len(n["docs"])) if cs else 0.0,
            "fallback_rate": (cs["fallback"] / cs["calls"]) if cs and cs["calls"] else None,
            "latency_p95_ms": quantile(cs["latencies"], 0.95) if cs and cs["latencies"] else None,
            "models": [m for m, _ in cs["models"].most_common(3)] if cs else [],
            "prompts": [p for p, _ in prompts[:3]], "prompt": live_prompt,
            "regression": any(p in regressions for p, _ in prompts),
            "errors": sum(err_by_stage[stage].values()), "errors_by_verdict": dict(err_by_stage[stage]),
            "open_alerts": open_alerts.get(stage, []) + [
                {"message": f"Prompt regression: {p}", "kind": "regression", "measure_id": "prompt_error_rate"}
                for p, _ in prompts if p in regressions],
            "failed_documents": n["failed_docs"],
        }
        entry["health"] = _health(entry)
        nodes.append(entry)
    nodes.sort(key=lambda n: (n["position"], n["id"]))

    completed = sum(1 for d in sampled if d in docs and docs[d].completed_at)
    start = {"id": START, "position": -1, "documents": len(sampled), "health": "ok", "kind": "start"}
    end = {"id": END, "position": (nodes[-1]["position"] + 1) if nodes else 0, "documents": completed,
           "open_documents": len(sampled) - completed, "health": "warn" if err_by_stage.get(END) else "ok",
           "kind": "end", "errors": sum(err_by_stage[END].values()), "errors_by_verdict": dict(err_by_stage[END])}
    total = len(sampled) or 1
    edge_list = [{"from": a, "to": b, "documents": n, "share": n / total} for (a, b), n in edges.most_common()]
    return {"window": [window.start.isoformat(), window.end.isoformat()], "documents": len(sampled),
            "nodes": [start] + nodes + [end], "edges": edge_list,
            "errors_total": (errs or {}).get("errors", 0)}


def stage_errors(source, window: Window, stage: str, limit: int = 50) -> List[dict]:
    """Reported errors that started at `stage` (or after the pipeline, for the end node)."""
    errs = summarize(source, window, include_all=True) if hasattr(source, "errors") else None
    want = None if stage == END else stage
    rows = [r for r in (errs or {}).get("recent_all", []) if r["origin_stage"] == want]
    rows.sort(key=lambda r: r["reported_at"] or "", reverse=True)
    return [{k: r[k] for k in ("error_id", "document_id", "field", "expected", "observed", "kind", "verdict",
                               "verdict_label", "explanation", "origin_prompt", "signals", "reported_at", "source")}
            for r in rows[:limit]]


def document_path(source, document_id: str) -> Optional[dict]:
    """One document's path through the workflow, with where each reported error started."""
    from assay.rootcause import analyze_document
    a = analyze_document(source, document_id)
    if a is None:
        return None
    detail = source.document_detail(document_id)
    runs = order_steps(detail[1])
    origin = defaultdict(list)
    for e in a["errors"]:
        origin[e["origin_stage"] or END].append({"field": e["field"], "verdict": e["verdict"]})
    return {"document_id": document_id,
            "steps": [{"stage": r.stage, "status": r.status, "did_work": r.did_work, "prompt": r.prompt,
                       "errors": origin.get(r.stage, [])} for r in runs],
            "after": origin.get(END, [])}
