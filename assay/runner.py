"""Compute measures against a source, persist the results, and raise alerts."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Callable, Iterable, List, Optional

from sqlalchemy import and_, desc, select
from sqlalchemy.engine import Engine

from assay import alerts, store
from assay.config import Settings
from assay.measures import REGISTRY
from assay.measures.base import MeasureOutput, unmeasured
from assay.models import Window
from assay.sources.sql import SQLSource
from assay.sources.events import EventsSource

log = logging.getLogger(__name__)


def resolve_source(name: str, engine: Engine, settings: Settings):
    if name == "sql":
        if not settings.source_url:
            raise ValueError("ASSAY_SOURCE_URL is not set, so the sql source is unavailable.")
        return SQLSource(settings.source_url, settings.downstream_url, settings.downstream_hash_sql)
    if name.startswith("events:"):
        return EventsSource(engine, name.split(":", 1)[1])
    raise ValueError(f"Unknown source '{name}'. Use 'sql' or 'events:<tenant>'.")


class CachedSource:
    """Fetch each (method, window) once per run, however many measures ask for it."""

    def __init__(self, source):
        self._source, self._cache = source, {}
        self.name = source.name
        self.memo = {}  # for derived results several measures share (e.g. the error summary)

    def _get(self, method: str, *args):
        key = (method,) + args
        if key not in self._cache:
            out = getattr(self._source, method)(*args)
            self._cache[key] = None if out is None else list(out) if not isinstance(out, set) else out
        return self._cache[key]

    def calls(self, w): return self._get("calls", w)
    def documents(self, w): return self._get("documents", w)
    def stage_runs(self, w): return self._get("stage_runs", w)
    def indexed(self, w): return self._get("indexed", w)
    def downstream_hashes(self): return self._get("downstream_hashes")

    def reviews(self, w):
        return self._get("reviews", w) if hasattr(self._source, "reviews") else None

    def document_detail(self, document_id):
        return self._source.document_detail(document_id)

    def __getattr__(self, name):
        # Anything else the source offers (e.g. an events source's agent trajectories), uncached.
        if name.startswith("_"):
            raise AttributeError(name)
        return getattr(self._source, name)

    def document_details(self, document_ids):
        if hasattr(self._source, "document_details"):
            return self._source.document_details(document_ids)
        return {i: d for i in document_ids if (d := self._source.document_detail(i))}

    def errors(self, w, document_id=None):
        if not hasattr(self._source, "errors"):
            return None
        return self._get("errors", w) if document_id is None else self._source.errors(w, document_id)


def load_rates(engine: Engine, source_name: str) -> dict:
    """The rate card for a source: its own rates over the "*" defaults."""
    t = store.cost_rates
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(t.c.source.in_([source_name, "*"]))).all()
    rates = {r.key: r.value for r in rows if r.source == "*"}
    rates.update({r.key: r.value for r in rows if r.source != "*"})
    return rates


def window_for_days(days: float, now: Optional[datetime] = None) -> Window:
    end = now or datetime.utcnow()
    return Window(end - timedelta(days=days), end)


def run_measures(engine: Engine, source, window: Window,
                 measure_ids: Optional[Iterable[str]] = None,
                 as_of: Optional[datetime] = None,
                 notify: Optional[Callable[[str, dict], None]] = None,
                 alert_min_n: int = 30, alert_after_runs: int = 2,
                 evaluate_alerts: bool = True, prompt_regressions: bool = True) -> int:
    """Compute and store measures, then evaluate alerts. `as_of` backdates the run (for backfills)."""
    ids = list(measure_ids or REGISTRY)
    cached = CachedSource(source)
    cached.cost_rates = load_rates(engine, source.name)
    outputs: List[MeasureOutput] = []
    for mid in ids:
        try:
            outputs.append(REGISTRY[mid].compute(cached, window))
        except Exception as exc:  # one broken measure must not hide the others
            log.exception("Measure %s failed", mid)
            outputs.append(unmeasured(mid, f"Measure failed: {type(exc).__name__}: {exc}"))

    with engine.begin() as conn:
        run_id = conn.execute(store.measure_runs.insert().values(
            started_at=as_of or datetime.utcnow(), source=source.name,
            window_start=window.start, window_end=window.end)).inserted_primary_key[0]
        rows = []
        for out in outputs:
            if out.status == "unmeasured":
                rows.append(dict(run_id=run_id, measure_id=out.measure_id, status=out.status,
                                 reason=out.reason, dimension=None, slice_value=None, value=None,
                                 numerator=None, denominator=None, n=0, note=None, stderr=None))
            for r in out.results:
                rows.append(dict(run_id=run_id, measure_id=out.measure_id, status=out.status,
                                 reason=out.reason, dimension=r.dimension, slice_value=r.slice_value,
                                 value=r.value, numerator=r.numerator, denominator=r.denominator,
                                 n=r.n, note=r.note, stderr=r.stderr))
        if rows:
            conn.execute(store.measure_results.insert(), rows)

    _discover_prompts(engine, cached, window)
    if evaluate_alerts:
        alerts.evaluate_run(engine, run_id, min_n=alert_min_n, notify=notify, after_runs=alert_after_runs)
        _contract_alerts(engine, run_id, cached, window, notify)
        if prompt_regressions:  # compares 30 days, so backfills only do it for recent days
            _prompt_regressions(engine, run_id, cached, window, notify)
    return run_id


PROMPT_LOOKBACK_DAYS = 30  # compare versions over this long, whatever the run's window


def _prompt_regressions(engine: Engine, run_id: int, source, window: Window, notify) -> None:
    from assay import prompts, rootcause
    try:
        # A run's window can be a day; a release needs both versions in view to compare.
        lookback = Window(window.end - timedelta(days=PROMPT_LOOKBACK_DAYS), window.end)
        summary = rootcause.summarize(source, lookback, include_all=True) if hasattr(source, "errors") else None
        if summary is None:
            return
        analysis = prompts.analyze(source, lookback, engine, summary)
        alerts.evaluate_prompt_regressions(engine, run_id, analysis, notify, live_since=window.start)
    except Exception:
        log.exception("Prompt regression check failed for %s", source.name)


def _contract_alerts(engine: Engine, run_id: int, source, window: Window, notify) -> None:
    from assay import contracts
    try:
        rules = contracts.load(engine, source.name)
        with engine.connect() as conn:  # an open contract alert must still resolve once its contract is gone
            open_ = conn.execute(select(store.alerts.c.id).where(and_(
                store.alerts.c.source == source.name, store.alerts.c.kind == "contract",
                store.alerts.c.state == "open"))).first()
        if rules or open_:
            alerts.evaluate_contracts(engine, run_id, contracts.check(source, window, rules), notify)
    except Exception:
        log.exception("Contract check failed for %s", source.name)


def _discover_prompts(engine: Engine, source, window: Window) -> None:
    """Record prompt versions seen in this window, for sources that aren't ingested (SQL)."""
    from assay import ingest, prompts
    tenant = prompts.registry_tenant(source.name)
    try:
        calls = [{"prompt_id": c.prompt_id, "prompt_version": c.prompt_version, "ts": c.ts}
                 for c in source.calls(window) or []]
        runs = [{"prompt_id": r.prompt_id, "prompt_version": r.prompt_version, "started_at": r.started_at}
                for r in source.stage_runs(window) or []]
    except Exception:
        log.exception("Couldn't read prompt versions from %s", source.name)
        return
    ingest.discover_prompts(engine, calls, tenant, "ts")
    ingest.discover_prompts(engine, runs, tenant, "started_at")


def backfill(engine: Engine, source, days: int = 30, window_days: float = 1.0,
             alert_min_n: int = 30, alert_after_runs: int = 2) -> dict:
    """Replay the last `days` as if Assay had been running all along.

    One run per day, oldest first, each over a `window_days` window, so a newly
    connected pipeline has baselines (and open alerts for anything already
    wrong) on day one instead of after a week. Days that already have a run for
    the same window are skipped, so it's safe to repeat. On a fresh source,
    alerts are evaluated in order (without notifying anyone about history); on
    a source that already has runs they aren't, so replaying old days can't
    reopen or resolve today's alerts.
    """
    runs = store.measure_runs
    now = datetime.utcnow().replace(microsecond=0)
    with engine.connect() as conn:
        existing = [r.window_end for r in conn.execute(select(runs.c.window_end).where(runs.c.source == source.name))]
    fresh = not existing
    made, skipped = [], 0
    for d in range(days - 1, -1, -1):
        end = now - timedelta(days=d)
        if any(abs((e - end).total_seconds()) < 3600 for e in existing):
            skipped += 1
            continue
        made.append(run_measures(engine, source, Window(end - timedelta(days=window_days), end), as_of=end,
                                 notify=None, alert_min_n=alert_min_n, alert_after_runs=alert_after_runs,
                                 evaluate_alerts=fresh, prompt_regressions=d < 7))
    return {"runs_created": len(made), "skipped": skipped, "run_ids": made}


def _last_runs(conn, source_name: str, k: int):
    runs = store.measure_runs
    return conn.execute(select(runs).where(runs.c.source == source_name)
                        .order_by(desc(runs.c.started_at), desc(runs.c.id)).limit(k)).all()


def latest_run(engine: Engine, source_name: str) -> Optional[dict]:
    res = store.measure_results
    with engine.connect() as conn:
        last = _last_runs(conn, source_name, 1)
        if not last:
            return None
        run = last[0]
        rows = conn.execute(select(res).where(res.c.run_id == run.id)).all()
    measures = {}
    for r in rows:
        m = measures.setdefault(r.measure_id, {"status": r.status, "reason": r.reason,
                                               "overall": None, "slices": {}})
        if r.dimension is None and r.status == "measured":
            m["overall"] = _row(r)
        elif r.dimension:
            m["slices"].setdefault(r.dimension, []).append(_row(r))
    return {"run_id": run.id, "source": run.source, "started_at": run.started_at.isoformat(),
            "window": [run.window_start.isoformat(), run.window_end.isoformat()], "measures": measures}


def history(engine: Engine, source_name: str, measure_id: str,
            dimension: Optional[str] = None, slice_value: Optional[str] = None, limit: int = 90) -> dict:
    """Time series for one slice, plus the band the next run will be judged against and any SLO."""
    runs, res = store.measure_runs, store.measure_results
    cond = [runs.c.source == source_name, res.c.measure_id == measure_id]
    cond.append(res.c.dimension == dimension if dimension else res.c.dimension.is_(None))
    if dimension:
        cond.append(res.c.slice_value == slice_value)
    q = (select(runs.c.started_at, res.c.value, res.c.n, res.c.status, res.c.stderr)
         .join(res, res.c.run_id == runs.c.id).where(and_(*cond))
         .order_by(desc(runs.c.started_at), desc(runs.c.id)).limit(limit))
    with engine.connect() as conn:
        rows = list(reversed(conn.execute(q).all()))
        slo = alerts.slo_for(alerts.load_slos(conn, source_name), measure_id, dimension, slice_value)
    points = [{"at": r.started_at.isoformat(), "value": r.value, "n": r.n, "status": r.status,
               "stderr": r.stderr} for r in rows]
    # Band from the runs before the latest, i.e. the band the latest point was judged against.
    past = points[:-1][-alerts.LOOKBACK:]
    past_se = sorted(p["stderr"] for p in past if p["stderr"] is not None)
    band = alerts.learn_band([p["value"] for p in past], REGISTRY[measure_id].unit,
                             points[-1]["n"] if points else None,
                             past_se[len(past_se) // 2] if past_se else None)
    return {"points": points,
            "band": None if band is None else {"low": band.low, "high": band.high,
                                               "center": band.center, "points": band.points},
            "slo": None if slo is None else {"target": slo["target"], "note": slo["note"]}}


def changes(engine: Engine, source_name: str, min_n: int = 30, limit: int = 12) -> list:
    """Slices whose change between the previous run and the latest is bigger than
    noise, worst first.

    Rates and counts must move by more than 3 standard errors (so a segment
    going from 54 to 41 documents doesn't make the list); timings and other
    units, which have no simple error formula, must move by more than 15% and
    by more than the unit's minimum meaningful step (e.g. 0.05 PSI).
    """
    res = store.measure_results
    with engine.connect() as conn:
        last = _last_runs(conn, source_name, 2)
        if len(last) < 2:
            return []
        cur_id, prev_id = last[0].id, last[1].id
        rows = conn.execute(select(res).where(and_(res.c.run_id.in_([cur_id, prev_id]),
                                                   res.c.status == "measured",
                                                   res.c.value.is_not(None)))).all()
    by_key = {}
    for r in rows:
        by_key.setdefault((r.measure_id, r.dimension, r.slice_value), {})[r.run_id] = r
    out = []
    for (mid, dim, val), pair in by_key.items():
        if cur_id not in pair or prev_id not in pair or mid not in REGISTRY:
            continue
        m, cur, prev = REGISTRY[mid], pair[cur_id], pair[prev_id]
        if m.unit != "count" and min(cur.n or 0, prev.n or 0) < min_n:
            continue
        delta = cur.value - prev.value
        if m.unit in ("ratio", "count") or (cur.stderr is not None and prev.stderr is not None):
            se_of = lambda r: r.stderr if r.stderr is not None else alerts.sampling_error(r.value, r.n, m.unit)
            se = (se_of(prev) ** 2 + se_of(cur) ** 2) ** 0.5
            size = abs(delta) / se if se else 0.0
            if size < 3:
                continue
        else:
            size = abs(delta) / max(abs(prev.value), 1e-9)
            if size < 0.15 or abs(delta) < alerts.MIN_WIDTH.get(m.unit, 0.0):
                continue
        worse = None if m.higher_is_better is None else (delta < 0) == m.higher_is_better
        out.append({"measure_id": mid, "dimension": dim, "slice": val, "previous": prev.value,
                    "current": cur.value, "delta": delta, "worse": worse, "n": cur.n,
                    "significance": size})
    # Worse first, then moves in either direction, then improvements.
    rank = {True: 0, None: 1, False: 2}
    out.sort(key=lambda x: (rank[x["worse"]], -x["significance"]))
    return out[:limit]


def _row(r) -> dict:
    return {"slice": r.slice_value, "value": r.value, "n": r.n, "stderr": r.stderr,
            "numerator": r.numerator, "denominator": r.denominator, "note": r.note}
