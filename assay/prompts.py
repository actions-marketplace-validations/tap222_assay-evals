"""Prompt versions: what each version did in production, and whether a new one is better.

A version is identified by (prompt_id, prompt_version) on the calls and steps
that ran it. The registry (store.prompt_versions) holds every version seen in
traffic or registered from CI, with its template and a note on what changed.

For each version this computes calls, documents, the error rate (documents
with a reported error that started at a step running this version), cost per
call, p95 latency, fallback and model-mismatch rates. Each version is then
compared with the one before it. The error-rate comparison is standardized to
the new version's document-type mix, so a version that happened to get more
hard documents isn't blamed for them.
"""
from __future__ import annotations

import difflib
import math
from collections import defaultdict
from typing import Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store
from assay.cost import is_fallback
from assay.measures.base import quantile
from assay.models import FAILED_STATUSES, UNRECORDED, Window, prompt_label

MIN_DOCS = 30  # per version, before a comparison says anything


def registry_tenant(source_name: str) -> str:
    return source_name.split(":", 1)[1] if source_name.startswith("events:") else source_name


def registry(engine: Engine, tenant: str) -> Dict[tuple, dict]:
    t = store.prompt_versions
    with engine.connect() as conn:
        return {(r.prompt_id, r.version): dict(r._mapping)
                for r in conn.execute(select(t).where(t.c.tenant == tenant))}


def _prop_test(x1: int, n1: int, x2: int, n2: int) -> Optional[dict]:
    """Difference of two proportions (second minus first) with a 95% interval."""
    if not n1 or not n2:
        return None
    p1, p2 = x1 / n1, x2 / n2
    se = math.sqrt(max(p1 * (1 - p1), 0.25 / n1) / n1 + max(p2 * (1 - p2), 0.25 / n2) / n2)
    d = p2 - p1
    return {"diff": d, "low": d - 1.96 * se, "high": d + 1.96 * se}


def _standardized(a: Dict[str, List[int]], b: Dict[str, List[int]]) -> Optional[dict]:
    """Error-rate difference (b - a), with a reweighted to b's document-type mix.

    a, b: document_type -> [documents with an error, documents]. Types seen by
    only one version are left out (and reported)."""
    common = [t for t in b if t in a and a[t][1] and b[t][1]]
    if not common:
        return None
    nb = sum(b[t][1] for t in common)
    diff = var = 0.0
    for t in common:
        w = b[t][1] / nb
        pa, pb = a[t][0] / a[t][1], b[t][0] / b[t][1]
        diff += w * (pb - pa)
        var += w * w * (max(pa * (1 - pa), 0.25 / a[t][1]) / a[t][1] + max(pb * (1 - pb), 0.25 / b[t][1]) / b[t][1])
    se = math.sqrt(var)
    return {"diff": diff, "low": diff - 1.96 * se, "high": diff + 1.96 * se,
            "types_compared": len(common), "types_left_out": sorted(set(a) ^ set(b))}


def _verdict(test: Optional[dict], n_prev: int, n_cur: int, higher_is_worse: bool = True) -> str:
    if test is None or min(n_prev, n_cur) < MIN_DOCS:
        return "too few"
    if test["low"] > 0:
        return "worse" if higher_is_worse else "better"
    if test["high"] < 0:
        return "better" if higher_is_worse else "worse"
    return "no clear difference"


def analyze(source, window: Window, engine: Optional[Engine] = None, error_summary: Optional[dict] = None) -> dict:
    """Every prompt version active in the window, with metrics and comparisons."""
    calls = list(source.calls(window) or [])
    runs = list(source.stage_runs(window) or [])
    docs = {d.document_id: d for d in (source.documents(window) or [])}
    reg = registry(engine, registry_tenant(source.name)) if engine is not None else {}

    v: Dict[str, dict] = {}

    def entry(pid, ver):
        label = prompt_label(pid, ver)
        return v.setdefault(label, {"prompt": label, "prompt_id": pid or "(unnamed)", "version": ver or "(unversioned)",
                                    "calls": 0, "failed_calls": 0, "fallback": 0, "mismatch": 0, "latencies": [],
                                    "costs": [], "docs": set(), "stages": set(), "first": None, "last": None})

    def seen(e, ts):
        if ts is not None:
            e["first"] = ts if e["first"] is None else min(e["first"], ts)
            e["last"] = ts if e["last"] is None else max(e["last"], ts)

    for c in calls:
        if not c.prompt:
            continue
        e = entry(c.prompt_id, c.prompt_version)
        e["calls"] += 1
        e["failed_calls"] += c.status in FAILED_STATUSES
        e["fallback"] += is_fallback(c)
        e["mismatch"] += bool(c.model_declared and c.model_served and c.model_declared != c.model_served)
        if c.latency_ms is not None:
            e["latencies"].append(c.latency_ms)
        if c.cost_usd is not None:
            e["costs"].append(c.cost_usd)
        if c.document_id:
            e["docs"].add(c.document_id)
        e["stages"].add(c.stage)
        seen(e, c.ts)
    for r in runs:
        if r.prompt:
            e = entry(r.prompt_id, r.prompt_version)
            e["docs"].add(r.document_id)
            e["stages"].add(r.stage)
            seen(e, r.started_at)

    # Documents with an error that started at a step running each version.
    err_docs: Dict[str, set] = defaultdict(set)
    for r in (error_summary or {}).get("recent_all", []):
        if r.get("origin_prompt"):
            err_docs[r["origin_prompt"]].add(r["document_id"])

    by_prompt: Dict[str, List[dict]] = defaultdict(list)
    for e in v.values():
        meta = reg.get((e["prompt_id"], e["version"]), {})
        n_docs = len(e["docs"])
        bad = err_docs.get(e["prompt"], set()) & e["docs"]
        by_type: Dict[str, List[int]] = defaultdict(lambda: [0, 0])
        for d in e["docs"]:
            t = (docs[d].document_type if d in docs else None) or UNRECORDED
            by_type[t][1] += 1
            by_type[t][0] += d in bad
        known = [x for x in (e["first"], meta.get("first_seen")) if x]
        first = min(known) if known else None
        row = {"prompt": e["prompt"], "prompt_id": e["prompt_id"], "version": e["version"],
               "stages": sorted(e["stages"]), "calls": e["calls"], "documents": n_docs,
               "first_seen": first.isoformat() if first else None,
               "last_seen": e["last"].isoformat() if e["last"] else None,
               "error_documents": len(bad), "error_rate": len(bad) / n_docs if n_docs else None,
               "call_error_rate": e["failed_calls"] / e["calls"] if e["calls"] else None,
               "fallback_rate": e["fallback"] / e["calls"] if e["calls"] else None,
               "mismatch_rate": e["mismatch"] / e["calls"] if e["calls"] else None,
               "latency_p95_ms": quantile(e["latencies"], 0.95) if e["latencies"] else None,
               "cost_per_call": sum(e["costs"]) / len(e["costs"]) if e["costs"] else None,
               "note": meta.get("note"), "author": meta.get("author"),
               "registered": bool(meta.get("registered_at")), "has_template": bool(meta.get("template")),
               "_by_type": by_type, "_counts": (e["failed_calls"], e["fallback"], e["mismatch"])}
        by_prompt[e["prompt_id"]].append(row)

    out = []
    for pid, rows in by_prompt.items():
        rows.sort(key=lambda r: r["first_seen"] or "")
        for prev, cur in zip([None] + rows[:-1], rows):
            if prev is None:
                cur["vs_previous"] = None
                continue
            err = _standardized(prev["_by_type"], cur["_by_type"])
            fb = _prop_test(prev["_counts"][1], prev["calls"], cur["_counts"][1], cur["calls"])
            ce = _prop_test(prev["_counts"][0], prev["calls"], cur["_counts"][0], cur["calls"])
            cur["vs_previous"] = {
                "previous": prev["version"],
                "error_rate": err | {"verdict": _verdict(err, prev["documents"], cur["documents"])} if err else
                {"verdict": "too few"},
                "fallback_rate": (fb or {}) | {"verdict": _verdict(fb, prev["calls"], cur["calls"])},
                "call_error_rate": (ce or {}) | {"verdict": _verdict(ce, prev["calls"], cur["calls"])},
                "latency_p95_ms": _ratio(prev["latency_p95_ms"], cur["latency_p95_ms"]),
                "cost_per_call": _ratio(prev["cost_per_call"], cur["cost_per_call"]),
            }
        for r in rows:
            r.pop("_by_type"), r.pop("_counts")
        live = max(rows, key=lambda r: r["last_seen"] or "")
        out.append({"prompt_id": pid, "versions": rows, "live_version": live["version"],
                    "stages": sorted({s for r in rows for s in r["stages"]})})
    out.sort(key=lambda p: p["prompt_id"])
    return {"prompts": out, "window": [window.start.isoformat(), window.end.isoformat()]}


def _ratio(a, b):
    if a is None or b is None or not a:
        return None
    return {"previous": a, "current": b, "change": b / a - 1}


def diff(engine: Engine, tenant: str, prompt_id: str, a: str, b: str) -> Optional[dict]:
    reg = registry(engine, tenant)
    ra, rb = reg.get((prompt_id, a)), reg.get((prompt_id, b))
    if not ra or not rb:
        return None
    if not (ra.get("template") and rb.get("template")):
        return {"available": False, "reason": "Register both versions with their template to see a diff."}
    lines = list(difflib.unified_diff(ra["template"].splitlines(), rb["template"].splitlines(),
                                      fromfile=f"{prompt_id}@{a}", tofile=f"{prompt_id}@{b}", lineterm=""))
    return {"available": True, "diff": lines, "note": rb.get("note")}


def changes_between(engine: Engine, tenant: str, start, end) -> List[dict]:
    """Prompt versions that first went live inside [start, end]: chart markers."""
    t = store.prompt_versions
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.first_seen >= start,
                                                 t.c.first_seen <= end)).order_by(t.c.first_seen)).all()
    return [{"at": r.first_seen.isoformat(), "prompt_id": r.prompt_id, "version": r.version, "note": r.note}
            for r in rows]
