"""Monitoring production: a sampled judge on live traffic, and quality with confidence intervals.

CI protects against known regressions, on a small curated suite of deterministic checks. Production
has no reference answers, so it relies more on reference-free evaluators, run on a sample, after
the fact, and on how often a failure happens rather than whether it happened once.

  sampled judge  ended production runs (not test cases, not synthetic ones, consented only when
                 ASSAY_REVIEW_CONSENTED_ONLY) are judged in the background by the built-in judge
                 (plan quality, consistency, context retention, faithfulness): a share of them
                 (ASSAY_PRODUCTION_JUDGE_SAMPLE), at least one per task so rare tasks are seen, within
                 a daily budget (ASSAY_PRODUCTION_JUDGE_BUDGET_USD). Redacted first. Kept apart from
                 test results (production_results), never a baseline.
  quality        per production check (the reference-free ones every ended run gets, the sampled
                 judge's, and each failure category's share of what was read), per day and over the
                 window, with a 95% interval. A target per metric: when the interval's lower bound is
                 under it (over it, for a failure share), investigate; when the whole interval is,
                 it's a breach. Both are alerts (kind quality), resolved when the interval clears.
  the loop       a sampled judge's FAIL is a failure signal for assay learn, so it groups into
                 patterns and draft test cases: what monitoring finds becomes a regression test.
"""
from __future__ import annotations

import hashlib
import math
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import ingest, store

Z = 1.96


def wilson(k: int, n: int) -> tuple:
    if not n:
        return (None, None)
    p = k / n
    d = 1 + Z * Z / n
    c = (p + Z * Z / (2 * n)) / d
    h = Z * math.sqrt(p * (1 - p) / n + Z * Z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


# ---------- the sampled judge ----------

def _rank(tenant: str, tid: str) -> float:
    return int(hashlib.sha256(f"{tenant}|{tid}".encode()).hexdigest()[:12], 16) / 16 ** 12


def sample(engine: Engine, tenant: str, since: datetime, until: datetime, rate: float,
           consented_only: bool = False) -> List[dict]:
    """Ended production runs to judge: a share of each task's (at least one), none judged before. The
    same runs every time for the same window: a run's place is a hash of its id."""
    t, pr = store.agent_trajectories, store.production_results
    with engine.connect() as conn:
        heads = [dict(r._mapping) for r in conn.execute(select(t).where(and_(
            t.c.tenant == tenant, t.c.run_id.is_(None), t.c.started_at >= since, t.c.started_at < until,
            t.c.status != "running")))]
        done = {r.trajectory_id for r in conn.execute(select(pr.c.trajectory_id).where(pr.c.tenant == tenant))}
    heads = [h for h in heads if h.get("origin") != "synthetic" and (not consented_only or h.get("consent"))]
    by_task: Dict[str, List[dict]] = defaultdict(list)
    for h in heads:
        by_task[h.get("task") or "(no task)"].append(h)
    out = []
    for task, hs in sorted(by_task.items()):  # the share is of all the window's runs; those judged already drop out
        hs.sort(key=lambda h: _rank(tenant, h["trajectory_id"]))
        out += [h for h in (hs[:max(1, math.ceil(rate * len(hs)))] if rate > 0 else []) if h["trajectory_id"] not in done]
    return out


def spent_today(engine: Engine, tenant: str, now: datetime) -> float:
    pr = store.production_results
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    with engine.connect() as conn:
        return sum(r.cost_usd or 0 for r in conn.execute(select(pr.c.cost_usd).where(and_(
            pr.c.tenant == tenant, pr.c.ts >= day))))


def judge_sample(engine: Engine, tenant: str, heads: List[dict], model: str, provider: str = "anthropic",
                 client=None, redact: bool = True, rt=None, now: Optional[datetime] = None) -> dict:
    """Judge these production runs; store what the judge said. {"judged", "failed", "results", "summary"}."""
    from assay import agents, judge, learn
    from assay.sources.events import EventsSource
    now = now or datetime.utcnow()
    trajs = EventsSource(engine, tenant).trajectories([h["trajectory_id"] for h in heads])
    inputs = learn._inputs(engine, tenant, list(trajs))
    work = []
    for h in heads:
        tr = trajs.get(h["trajectory_id"])
        if tr is None:
            continue
        earlier = None
        if tr.get("conversation_id"):
            earlier = [learn.snapshot(engine, f"events:{tenant}", t["trajectory_id"], redact, False)
                       for t in agents.conversation_turns(engine, tenant, tr["conversation_id"])
                       if t["trajectory_id"] != h["trajectory_id"] and learn._earlier(t, tr)]
        work.append((h, tr, (inputs.get(h["trajectory_id"]) or {}).get("input"), earlier))
    rt = rt or judge.runtime()
    report = rt.map(lambda w: {**judge.judge(w[1], w[2], w[3], model, client, redact, provider),
                               **judge.faithful(w[1], w[2], model, client, redact, provider)}, work)
    rows = []
    for (h, tr, _, _), done in zip(work, report.results):
        if done.status != "DONE":
            continue
        for field, r in done.value.items():
            rows.append({"tenant": tenant, "result_id": ingest._derive("production", h["trajectory_id"], field),
                         "trajectory_id": h["trajectory_id"], "task": h.get("task"), "field": field,
                         "status": r["status"], "category": r.get("category"), "reason": (r.get("reason") or "")[:2000],
                         "judge_model": r.get("judge_model"), "judge_prompt": r.get("judge_prompt"),
                         "cost_usd": r.get("cost_usd"), "duration_ms": r.get("duration_ms"),
                         "started_at": h["started_at"], "ts": now})
    ingest.upsert(engine, store.production_results, rows, "result_id")
    return {"judged": sum(1 for d in report.results if d.status == "DONE"), "results": len(rows),
            "failed": sum(1 for r in rows if r["status"] == "fail"), "summary": report.to_dict()}


def judge_window(engine: Engine, tenant: str, settings, since: datetime, until: datetime, client=None,
                 rt=None) -> dict:
    """The sampled judge over a window, as the settings say, within what's left of today's budget."""
    from assay import judge, review
    rate = settings.production_judge_sample
    if not rate:
        return {"judged": 0, "skipped": "ASSAY_PRODUCTION_JUDGE_SAMPLE is 0"}
    heads = sample(engine, tenant, since, until, rate, settings.review_consented_only)
    budget = settings.production_judge_budget_usd
    left = None if budget is None else budget - spent_today(engine, tenant, until)
    if left is not None and left <= 0:
        return {"judged": 0, "skipped": f"today's budget (${budget:g}) is spent"}
    rt = rt or judge.runtime({"concurrency": settings.judge_concurrency, "rate_limit": settings.judge_rate_limit,
                              **({"budget_usd": left} if left is not None else {})})
    out = judge_sample(engine, tenant, heads, settings.judge_model, settings.judge_provider, client,
                       settings.judge_redact, rt, until)
    return {**out, "sampled": len(heads)}


# ---------- quality, with intervals ----------

def _day(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d")


def metrics(engine: Engine, tenant: str, since: datetime, until: datetime) -> Dict[str, dict]:
    """{metric: {"kind": pass_rate | share, "by_day": {day: [k, n]}, "k", "n"}}: pass counts for checks,
    failure counts for category shares."""
    from assay import review
    t, rc, pr = store.agent_trajectories, store.run_checks, store.production_results
    out: Dict[str, dict] = defaultdict(lambda: {"kind": "pass_rate", "by_day": defaultdict(lambda: [0, 0]), "k": 0, "n": 0})

    def count(metric, day, good, kind="pass_rate"):
        m = out[metric]
        m["kind"] = kind
        m["by_day"][day][0] += int(good)
        m["by_day"][day][1] += 1
        m["k"] += int(good)
        m["n"] += 1
    with engine.connect() as conn:
        heads = {r.trajectory_id: r for r in conn.execute(select(t.c.trajectory_id, t.c.started_at, t.c.origin).where(and_(
            t.c.tenant == tenant, t.c.run_id.is_(None), t.c.started_at >= since, t.c.started_at < until)))
            if r.origin != "synthetic"}
        ids = list(heads)
        for i in range(0, len(ids), 500):
            for r in conn.execute(select(rc).where(and_(rc.c.tenant == tenant, rc.c.trajectory_id.in_(ids[i:i + 500])))):
                for c in r.checks or []:
                    if c.get("status") in ("pass", "fail"):
                        count(f"check.{c['check']}", _day(heads[r.trajectory_id].started_at), c["status"] == "pass")
        for r in conn.execute(select(pr).where(and_(pr.c.tenant == tenant, pr.c.started_at >= since,
                                                   pr.c.started_at < until))):
            if r.status in ("pass", "fail"):
                count(f"judge.{r.field}", _day(r.started_at), r.status == "pass")
        notes = [dict(x._mapping) for x in conn.execute(select(store.review_notes).where(and_(
            store.review_notes.c.tenant == tenant, store.review_notes.c.created_at >= since,
            store.review_notes.c.created_at < until))) if not x.superseded and x.by != review.SEARCHED]
    cats = {c["id"]: c for c in review.categories(engine, tenant)}
    synth = review.synthetic(engine, tenant)
    read: Dict[str, set] = defaultdict(set)
    hit: Dict[tuple, set] = defaultdict(set)
    for n in notes:
        if any(x in synth for x in n["trace_ids"] or []):
            continue
        d = _day(n["created_at"])
        read[d].add(n["conversation"])
        cid = review._resolve({k: v for k, v in cats.items()}, n["category_id"]) if n["went_wrong"] else None
        if cid in cats and cats[cid]["status"] in ("open", "confirmed"):
            hit[(cats[cid]["name"], d)].add(n["conversation"])
    for name in {k[0] for k in hit}:
        m = out[f"category.{name}"]
        m["kind"] = "share"
        for d, convs in read.items():
            k = len(hit.get((name, d), set()))
            m["by_day"][d] = [k, len(convs)]
            m["k"] += k
            m["n"] += len(convs)
    return out


def targets(engine: Engine, tenant: str) -> Dict[str, float]:
    t = store.production_targets
    with engine.connect() as conn:
        return {r.metric: r.target for r in conn.execute(select(t).where(t.c.tenant == tenant))}


def state(kind: str, lo: Optional[float], hi: Optional[float], target: Optional[float]) -> Optional[str]:
    """ok | investigate | breach against a target: for a pass rate, the lower bound under it is worth a look,
    the whole interval under it is a breach; for a failure share, the other way round."""
    if target is None or lo is None:
        return None
    if kind == "pass_rate":
        return "breach" if hi < target else "investigate" if lo < target else "ok"
    return "breach" if lo > target else "investigate" if hi > target else "ok"


def quality(engine: Engine, tenant: str, days: float = 7, now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    since = now - timedelta(days=days)
    got = metrics(engine, tenant, since, now)
    tg = targets(engine, tenant)
    out = []
    for name, m in sorted(got.items()):
        lo, hi = wilson(m["k"], m["n"])
        rate = m["k"] / m["n"] if m["n"] else None
        target = tg.get(name)
        out.append({"metric": name, "kind": m["kind"], "value": rate, "low": lo, "high": hi, "n": m["n"],
                    "target": target, "state": state(m["kind"], lo, hi, target),
                    "by_day": [{"day": d, "value": k / n if n else None, "low": wilson(k, n)[0], "high": wilson(k, n)[1],
                                "n": n} for d, (k, n) in sorted(m["by_day"].items())]})
    order = {"breach": 0, "investigate": 1, "ok": 2, None: 3}
    return {"days": days, "metrics": sorted(out, key=lambda x: (order[x["state"]], x["metric"]))}


def set_target(engine: Engine, tenant: str, metric: str, target: Optional[float]) -> None:
    t = store.production_targets
    with engine.begin() as conn:
        conn.execute(t.delete().where(and_(t.c.tenant == tenant, t.c.metric == metric)))
        if target is not None:
            conn.execute(t.insert().values(tenant=tenant, metric=metric, target=float(target), updated_at=datetime.utcnow()))


def _fmt(kind: str, v: Optional[float]) -> str:
    return "-" if v is None else f"{v:.1%}"


def alert(engine: Engine, tenant: str, q: dict, now: Optional[datetime] = None) -> List[dict]:
    """Open an alert (kind quality) for each metric whose interval says investigate or breach; resolve
    the ones that cleared. Returns what's open."""
    now = now or datetime.utcnow()
    a = store.alerts
    src = f"events:{tenant}"
    opened = []
    with engine.begin() as conn:
        rows = {r.measure_id: dict(r._mapping) for r in conn.execute(select(a).where(and_(
            a.c.source == src, a.c.kind == "quality", a.c.state == "open")))}
        for m in q["metrics"]:
            key = f"quality:{m['metric']}"[:64]
            if m["state"] in ("investigate", "breach"):
                side = "under" if m["kind"] == "pass_rate" else "over"
                msg = (f"{m['metric']} is {_fmt(m['kind'], m['value'])} (95% interval {_fmt(m['kind'], m['low'])} to "
                       f"{_fmt(m['kind'], m['high'])}, n={m['n']}); target {_fmt(m['kind'], m['target'])}. "
                       + ("The whole interval is " + side + " it." if m["state"] == "breach" else
                          f"The interval reaches {side} it: investigate."))[:512]
                vals = dict(last_seen_at=now, value=m["value"], expected_low=m["low"], expected_high=m["high"],
                            target=m["target"], n=m["n"], message=msg)
                if key in rows:
                    conn.execute(a.update().where(a.c.id == rows[key]["id"]).values(streak=rows[key]["streak"] + 1,
                                                                                    clear_streak=0, **vals))
                else:
                    conn.execute(a.insert().values(source=src, measure_id=key, dimension=None, slice_value=m["state"],
                                                   kind="quality", state="open", streak=1, clear_streak=0,
                                                   opened_at=now, **vals))
                opened.append({"metric": m["metric"], "state": m["state"], "message": msg})
            elif key in rows and m["state"] == "ok":
                conn.execute(a.update().where(a.c.id == rows[key]["id"]).values(state="resolved", resolved_at=now,
                                                                                last_seen_at=now))
    return opened
