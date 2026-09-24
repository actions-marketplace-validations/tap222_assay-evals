"""Alerting: learned baselines, SLO targets, and an open → resolved lifecycle.

Two independent conditions are checked for every measured slice after a run:

- anomaly: the value leaves the band learned from the previous runs of the same
  slice (median ± 3 robust deviations, with a minimum width). The minimum width
  matters: a zero-width band would make every ordinary week look like a
  regression.
- slo: the value is on the wrong side of a target someone set.

A condition seen once makes the alert *pending*; it only *opens* (and
notifies) when it holds on `after_runs` consecutive runs, so a one-run blip
never pages anyone. It resolves the same way: only once the slice is measured
and clear on `after_runs` consecutive runs, so one good run doesn't flap it.
While open, an anomaly is judged against the range it opened with, so a
sustained shift can't quietly become the new normal. The band's width comes
from the slice's normal noise in history, not the current run's, because an
incident often raises the spread as well. A slice that stops being measured
does not resolve its alert.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Callable, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, or_, select
from sqlalchemy.engine import Engine

from assay import store
from assay.measures import REGISTRY
from assay.units import fmt

log = logging.getLogger(__name__)

MIN_POINTS = 4  # runs of history needed before a band is trusted
LOOKBACK = 8
K = 3.0  # band half-width in robust standard deviations / standard errors
MIN_WIDTH = {"ratio": 0.005, "psi": 0.05, "count": 1.0, "ms": 1.0, "seconds": 1.0, "usd": 0.01}
# Quantiles of long-tailed timings are noisy in ways a standard error for a
# mean doesn't capture, so they get a wider relative floor.
REL_FLOOR = {"ratio": 0.1, "count": 0.1, "ms": 0.25, "seconds": 0.25, "usd": 0.1, "psi": 0.0}

Key = Tuple[str, Optional[str], Optional[str]]  # measure_id, dimension, slice_value


@dataclass
class Band:
    low: float
    high: float
    center: float
    points: int


def sampling_error(center: float, n: Optional[int], unit: str) -> float:
    """Standard error expected from sample size alone, where there is a formula for it."""
    if not n:
        return 0.0
    if unit == "ratio":
        p = min(max(center, 1 / n), 1 - 1 / n)  # a 0% baseline still has uncertainty
        return math.sqrt(p * (1 - p) / n)
    if unit == "count":
        return math.sqrt(max(center, 1.0))  # Poisson
    return 0.0


def learn_band(history: List[Optional[float]], unit: str, n: Optional[int] = None,
               stderr: Optional[float] = None) -> Optional[Band]:
    """Expected range for the next value: the widest of run-to-run spread,
    sampling error at this sample size, and a minimum width."""
    vals = [v for v in history if v is not None][-LOOKBACK:]
    if len(vals) < MIN_POINTS:
        return None
    med = median(vals)
    mad = median(abs(v - med) for v in vals)
    half = max(K * 1.4826 * mad, K * sampling_error(med, n, unit), K * (stderr or 0.0),
               REL_FLOOR.get(unit, 0.1) * abs(med), MIN_WIDTH.get(unit, 0.0))
    return Band(med - half, med + half, med, len(vals))


def outside(value: float, band: Band, higher_is_better: Optional[bool]) -> bool:
    if higher_is_better is True:
        return value < band.low
    if higher_is_better is False:
        return value > band.high
    return value < band.low or value > band.high


def breaches(value: float, target: float, higher_is_better: Optional[bool]) -> bool:
    return value < target if higher_is_better else value > target


def slice_label(dimension: Optional[str], slice_value: Optional[str]) -> str:
    return "overall" if dimension is None else f"{dimension}={slice_value}"


# ---------- SLOs ----------

def slo_for(slos: List[dict], measure_id: str, dimension: Optional[str],
            slice_value: Optional[str]) -> Optional[dict]:
    """Most specific SLO that applies: exact slice, then whole dimension, then overall."""
    best, best_rank = None, -1
    for s in slos:
        if s["measure_id"] != measure_id:
            continue
        if s["dimension"] is None and dimension is None:
            rank = 1
        elif s["dimension"] == dimension and s["slice_value"] is None and dimension is not None:
            rank = 2
        elif s["dimension"] == dimension and s["slice_value"] == slice_value and dimension is not None:
            rank = 3
        else:
            continue
        rank += 10 if s["source"] != "*" else 0  # source-specific beats global
        if rank > best_rank:
            best, best_rank = s, rank
    return best


def load_slos(conn, source: str) -> List[dict]:
    t = store.slos
    return [dict(r._mapping) for r in conn.execute(select(t).where(or_(t.c.source == source, t.c.source == "*")))]


# ---------- history ----------

def history_by_key(conn, source: str, before, limit_runs: int = LOOKBACK
                   ) -> Dict[Key, List[Tuple[Optional[float], Optional[float]]]]:
    """(value, stderr) per slice over the runs before time `before`, oldest first.

    Ordered by when each run's window ended, not by insertion order, so a
    backfill of older days slots into history where it belongs.
    """
    runs, res = store.measure_runs, store.measure_results
    rows_ = conn.execute(
        select(runs.c.id).where(and_(runs.c.source == source, runs.c.started_at < before))
        .order_by(desc(runs.c.started_at)).limit(limit_runs)).all()
    run_ids = [r[0] for r in rows_]
    order = {rid: i for i, rid in enumerate(reversed(run_ids))}
    out: Dict[Key, List[Tuple[Optional[float], Optional[float]]]] = defaultdict(list)
    if not run_ids:
        return out
    rows = conn.execute(select(res.c.run_id, res.c.measure_id, res.c.dimension, res.c.slice_value, res.c.value,
                               res.c.stderr)
                        .where(and_(res.c.run_id.in_(run_ids), res.c.status == "measured"))).all()
    for r in sorted(rows, key=lambda r: order[r.run_id]):
        out[(r.measure_id, r.dimension, r.slice_value)].append((r.value, r.stderr))
    return out


# ---------- evaluation ----------

def evaluate_run(engine: Engine, run_id: int, min_n: int = 30,
                 notify: Optional[Callable[[str, dict], None]] = None,
                 after_runs: int = 2) -> Dict[str, List[dict]]:
    runs, res, alerts = store.measure_runs, store.measure_results, store.alerts
    opened, resolved = [], []
    with engine.begin() as conn:
        run = conn.execute(select(runs).where(runs.c.id == run_id)).first()
        now = run.started_at
        current = conn.execute(select(res).where(and_(res.c.run_id == run_id, res.c.status == "measured",
                                                      res.c.value.is_not(None)))).all()
        hist = history_by_key(conn, run.source, run.started_at)
        slos = load_slos(conn, run.source)
        open_rows = {(a.measure_id, a.dimension, a.slice_value, a.kind): a for a in conn.execute(
            select(alerts).where(and_(alerts.c.source == run.source, alerts.c.state.in_(("open", "pending")))))}

        evaluated = set()
        for r in current:
            m = REGISTRY.get(r.measure_id)
            if m is None:
                continue
            if m.unit != "count" and (r.n or 0) < min_n:
                continue  # too small to judge; a count of zero is still judged
            key = (r.measure_id, r.dimension, r.slice_value)
            where = slice_label(r.dimension, r.slice_value)
            checks = {}
            past = hist.get(key, [])
            # Normal noise comes from history, not this run: an incident often raises the
            # spread too, and must not widen the band it's judged against.
            past_se = [se for _, se in past if se is not None]
            band = learn_band([v for v, _ in past], m.unit, r.n,
                              median(past_se) if past_se else None) if m.anomaly_alerts else None
            held = open_rows.get(key + ("anomaly",))
            if band and held is not None and held.expected_low is not None and held.expected_high is not None:
                # Judge an open anomaly against the range it opened with. Re-learning would let a
                # sustained shift become the new normal and resolve an incident that's still going on.
                band = Band(held.expected_low, held.expected_high, (held.expected_low + held.expected_high) / 2,
                            band.points)
            if band and outside(r.value, band, m.higher_is_better):
                checks["anomaly"] = dict(
                    expected_low=band.low, expected_high=band.high, target=None,
                    message=(f"{m.name} [{where}] is {fmt(r.value, m.unit)}; the last {band.points} runs "
                             f"put it between {fmt(max(band.low, 0), m.unit)} and {fmt(band.high, m.unit)}."))
            slo = slo_for(slos, r.measure_id, r.dimension, r.slice_value) if m.higher_is_better is not None else None
            if slo and breaches(r.value, slo["target"], m.higher_is_better):
                op = "≥" if m.higher_is_better else "≤"
                checks["slo"] = dict(
                    expected_low=None, expected_high=None, target=slo["target"],
                    message=f"{m.name} [{where}] is {fmt(r.value, m.unit)}; SLO is {op} {fmt(slo['target'], m.unit)}.")
            for kind in ("anomaly", "slo"):
                evaluated.add(key + (kind,))
                existing = open_rows.get(key + (kind,))
                if kind in checks:
                    fields = dict(last_seen_at=now, run_id=run_id, value=r.value, n=r.n, clear_streak=0,
                                  **checks[kind])
                    if existing is None:
                        streak = 1
                        row = dict(source=run.source, measure_id=r.measure_id, dimension=r.dimension,
                                   slice_value=r.slice_value, kind=kind, opened_at=now, streak=streak,
                                   state="open" if streak >= after_runs else "pending", **fields)
                        row["id"] = conn.execute(alerts.insert().values(**row)).inserted_primary_key[0]
                        if row["state"] == "open":
                            opened.append(row)
                    else:
                        streak = (existing.streak or 1) + 1
                        promote = existing.state == "pending" and streak >= after_runs
                        conn.execute(alerts.update().where(alerts.c.id == existing.id).values(
                            streak=streak, **fields, **({"state": "open"} if promote else {})))
                        if promote:
                            opened.append(dict(existing._mapping) | fields | {"state": "open", "streak": streak})
                elif existing is not None and existing.state == "pending":
                    conn.execute(alerts.delete().where(alerts.c.id == existing.id))  # blip, never fired
                elif existing is not None:
                    clear = (existing.clear_streak or 0) + 1
                    if clear >= after_runs:  # recovered on enough runs in a row: not a one-run dip
                        conn.execute(alerts.update().where(alerts.c.id == existing.id)
                                     .values(state="resolved", resolved_at=now, value=r.value, run_id=run_id,
                                             clear_streak=clear))
                        resolved.append(dict(existing._mapping) | {"value": r.value, "resolved_at": now})
                    else:
                        conn.execute(alerts.update().where(alerts.c.id == existing.id)
                                     .values(clear_streak=clear, value=r.value, run_id=run_id))

    if notify:
        for a in opened:
            notify("opened", a)
        for a in resolved:
            notify("resolved", a)
    return {"opened": opened, "resolved": resolved}


# ---------- prompt regressions ----------

def evaluate_prompt_regressions(engine: Engine, run_id: int, analysis: dict,
                                notify: Optional[Callable[[str, dict], None]] = None,
                                live_since: Optional[datetime] = None) -> Dict[str, List[dict]]:
    """Open an alert when a prompt version's error rate is worse than the version
    before it, beyond noise and adjusted for document mix; resolve it when that's
    no longer true. A new version has no history of its own, so this comparison,
    not an anomaly band, is what catches a bad release."""
    runs, alerts_t = store.measure_runs, store.alerts
    opened, resolved = [], []
    with engine.begin() as conn:
        run = conn.execute(select(runs).where(runs.c.id == run_id)).first()
        now = run.started_at
        current = {a.slice_value: a for a in conn.execute(select(alerts_t).where(and_(
            alerts_t.c.source == run.source, alerts_t.c.kind == "regression", alerts_t.c.state == "open")))}
        worse, cleared = {}, set()
        for p in analysis.get("prompts", []):
            for v in p["versions"]:
                cmp_ = (v.get("vs_previous") or {}).get("error_rate") or {}
                retired = live_since is not None and v["last_seen"] and v["last_seen"] < live_since.isoformat()
                if cmp_.get("verdict") == "worse" and not retired:
                    worse[v["prompt"]] = (v, cmp_)
                elif retired or cmp_.get("verdict") in ("better", "no clear difference"):
                    cleared.add(v["prompt"])  # shown no worse, or no longer serving: resolve
        for label, (v, c) in worse.items():
            msg = (f"Prompt {label} has a higher error rate than {v['prompt_id']}@{v['vs_previous']['previous']}: "
                   f"{v['error_rate']:.2%}, +{c['diff'] * 100:.2f} points (95% interval +{c['low'] * 100:.2f} to "
                   f"+{c['high'] * 100:.2f}), adjusted for document mix, over {v['documents']:,} documents.")
            fields = dict(last_seen_at=now, run_id=run_id, value=v["error_rate"], n=v["documents"], message=msg,
                          expected_low=None, expected_high=None, target=None)
            if label in current:
                conn.execute(alerts_t.update().where(alerts_t.c.id == current[label].id).values(**fields))
            else:
                row = dict(source=run.source, measure_id="prompt_error_rate", dimension="prompt", slice_value=label,
                           kind="regression", state="open", opened_at=now, streak=1, clear_streak=0, **fields)
                row["id"] = conn.execute(alerts_t.insert().values(**row)).inserted_primary_key[0]
                opened.append(row)
        seen_now = {v["prompt"] for p in analysis.get("prompts", []) for v in p["versions"]}
        for label, a in current.items():
            if label in cleared or label not in seen_now:  # "too few" keeps an open alert open
                conn.execute(alerts_t.update().where(alerts_t.c.id == a.id)
                             .values(state="resolved", resolved_at=now, run_id=run_id))
                resolved.append(dict(a._mapping) | {"resolved_at": now})
    if notify:
        for a in opened:
            notify("opened", a)
        for a in resolved:
            notify("resolved", a)
    return {"opened": opened, "resolved": resolved}


# ---------- path contracts ----------

def evaluate_contracts(engine: Engine, run_id: int, report: dict,
                       notify: Optional[Callable[[str, dict], None]] = None) -> Dict[str, List[dict]]:
    """Open an alert the first run a contract is broken, with no waiting: a
    contract is a rule, not a baseline, so one document that ran a forbidden
    step is already the news. Resolve it on the first run where documents were
    judged and none broke it, or when the contract is deleted."""
    runs, alerts_t = store.measure_runs, store.alerts
    opened, resolved = [], []
    with engine.begin() as conn:
        run = conn.execute(select(runs).where(runs.c.id == run_id)).first()
        now = run.started_at
        current = {a.slice_value: a for a in conn.execute(select(alerts_t).where(and_(
            alerts_t.c.source == run.source, alerts_t.c.kind == "contract", alerts_t.c.state == "open")))}
        live = set()
        for c in report.get("contracts", []):
            key = str(c["id"])
            live.add(key)
            if c["violations"]:
                ex = ", ".join(e["document_id"] for e in c["examples"][:3])
                msg = (f"{'Contract' if c.get('severity') != 'warning' else 'Warning contract'} broken: "
                       f"{c['label']}. {c['violations']:,} of {c['judged']:,} documents ({ex}"
                       f"{', …' if c['violations'] > 3 else ''}): {c['examples'][0]['detail']}.")[:512]
                fields = dict(last_seen_at=now, run_id=run_id, value=float(c["violations"]), n=c["judged"],
                              message=msg, expected_low=None, expected_high=None, target=0.0, clear_streak=0)
                if key in current:
                    conn.execute(alerts_t.update().where(alerts_t.c.id == current[key].id)
                                 .values(streak=(current[key].streak or 1) + 1, **fields))
                else:
                    row = dict(source=run.source, measure_id="path_contract", dimension="contract", slice_value=key,
                               kind="contract", state="open", opened_at=now, streak=1, **fields)
                    row["id"] = conn.execute(alerts_t.insert().values(**row)).inserted_primary_key[0]
                    opened.append(row)
            elif c["judged"] and key in current:
                conn.execute(alerts_t.update().where(alerts_t.c.id == current[key].id)
                             .values(state="resolved", resolved_at=now, run_id=run_id, value=0.0, n=c["judged"]))
                resolved.append(dict(current[key]._mapping) | {"value": 0.0, "resolved_at": now})
        for key, a in current.items():
            if key not in live:  # contract deleted
                conn.execute(alerts_t.update().where(alerts_t.c.id == a.id)
                             .values(state="resolved", resolved_at=now, run_id=run_id))
                resolved.append(dict(a._mapping) | {"resolved_at": now})
    if notify:
        for a in opened:
            notify("opened", a)
        for a in resolved:
            notify("resolved", a)
    return {"opened": opened, "resolved": resolved}


# ---------- notification ----------

def webhook_notifier(url: str, fmt_kind: str = "slack", public_url: Optional[str] = None,
                     secret: Optional[str] = None) -> Callable[[str, dict], None]:
    """Build a notifier that POSTs each alert change. Failures are logged, never raised.

    With a secret, each request carries X-Assay-Timestamp and
    X-Assay-Signature: sha256=HMAC(secret, "<timestamp>.<body>"), so the
    receiver can check it came from Assay and isn't a replay.
    """

    def send(event: str, alert: dict) -> None:
        icon = ":red_circle:" if event == "opened" else ":white_check_mark:"
        text = f"{icon} Assay alert {event} ({alert['kind']}, {alert['source']}): {alert['message']}"
        m = REGISTRY.get(alert["measure_id"])
        if event == "resolved" and alert["kind"] == "contract":
            text = f"{icon} Resolved (contract, {alert['source']}): no documents break it any more."
        elif event == "resolved" and m:
            text = (f"{icon} Resolved ({alert['kind']}, {alert['source']}): "
                    f"{m.name} [{slice_label(alert['dimension'], alert['slice_value'])}] "
                    f"is back to {fmt(alert['value'], m.unit)}.")
        elif event == "resolved":
            text = f"{icon} Resolved ({alert['kind']}, {alert['source']}): {alert['message']}"
        if public_url:
            page = "workflow" if alert["kind"] == "contract" else f"measures/{alert['measure_id']}"
            text += f" {public_url.rstrip('/')}/#{page}"
        if fmt_kind == "slack":
            body = {"text": text}
        else:
            body = {"event": event, "text": text,
                    "alert": {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in alert.items()}}
        data = json.dumps(body).encode()
        headers = {"Content-Type": "application/json"}
        if secret:
            stamp = str(int(datetime.utcnow().timestamp()))
            sig = hmac.new(secret.encode(), stamp.encode() + b"." + data, hashlib.sha256).hexdigest()
            headers |= {"X-Assay-Timestamp": stamp, "X-Assay-Signature": f"sha256={sig}"}
        req = urllib.request.Request(url, data=data, method="POST", headers=headers)
        try:
            urllib.request.urlopen(req, timeout=5).close()
        except Exception:
            log.exception("Alert webhook failed for alert %s", alert.get("id"))

    return send
