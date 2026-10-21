"""Detection of runaway agent runs, caught in the first minute.

Production alerts that watch rates over a daily or hourly window are too slow
when an agent enters an infinite retry loop or runs up an enormous bill overnight.

Immediate runaway detectors:
  1. The same failure again and again: the same tool returning the same error
     (its message with ids and numbers taken out) 5 times in one run, whatever
     the model changes in its arguments.
  2. A run far past its usual size: calls, tokens or cost per run against
     that agent's normal.
  3. Spend faster than normal: spend per hour for a customer or an agent against
     its usual hour, so a loop shows in minutes.
  4. The slow leak: the share of runs that end having done nothing, and the
     cost per run that did something.
"""
from __future__ import annotations

import logging
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store

log = logging.getLogger(__name__)

# Mask variable tokens (IDs, UUIDs, hex, timestamps, IP addresses, digits)
_UUID_HEX_RX = re.compile(r"\b[0-9a-fA-F-]{8,}\b")
_NUM_RX = re.compile(r"\b\d+(?:[a-zA-Z]+)?\b")
_URL_RX = re.compile(r"https?://\S+")


def mask_error(msg: Optional[str]) -> str:
    """Mask specific identifiers, numbers, and URLs from an error message to extract its structural pattern."""
    if not msg:
        return ""
    text = _URL_RX.sub("<url>", str(msg))
    text = _UUID_HEX_RX.sub("<id>", text)
    text = _NUM_RX.sub("<n>", text)
    return re.sub(r"\s+", " ", text).strip()


def check_repeated_failure(steps: List[dict], threshold: int = 5) -> Optional[dict]:
    """Check if the same tool returned the same masked error >= threshold times in this run."""
    fails: List[Tuple[str, str]] = []
    for s in steps:
        err = s.get("error") or (s.get("result") if s.get("kind") == "tool" and s.get("status") == "error" else None)
        if not err and s.get("status") in ("error", "fail"):
            err = s.get("text") or "failed"
        if err:
            tool = s.get("name") or s.get("tool") or s.get("kind") or "tool"
            fails.append((tool, mask_error(str(err))))

    counts = Counter(fails)
    for (tool, pattern), count in counts.items():
        if count >= threshold:
            return {
                "kind": "repeated_failure",
                "tool": tool,
                "error_pattern": pattern,
                "count": count,
                "message": f"Runaway loop: tool '{tool}' returned error '{pattern}' {count} times in one run.",
            }
    return None


def check_runaway_size(steps_count: int, cost: Optional[float] = None, tokens: Optional[int] = None,
                       baseline: Optional[dict] = None) -> Optional[dict]:
    """Check if a run's call count, cost, or tokens exceed normal limits by a wide factor."""
    base_steps = (baseline or {}).get("steps", 20)
    base_cost = (baseline or {}).get("cost", 1.0)

    if steps_count >= max(50, base_steps * 4):
        return {
            "kind": "excessive_steps",
            "steps": steps_count,
            "baseline": base_steps,
            "message": f"Runaway size: run reached {steps_count} steps (normal for agent: ~{base_steps}).",
        }
    if cost is not None and cost >= max(5.0, base_cost * 5):
        return {
            "kind": "excessive_cost",
            "cost": cost,
            "baseline": base_cost,
            "message": f"Runaway cost: run cost ${cost:.2f} (normal for agent: ~${base_cost:.2f}).",
        }
    return None


def check_spend_rate(hourly_spend: float, normal_hourly_spend: float,
                     threshold_factor: float = 3.0) -> Optional[dict]:
    """Detect when spend per hour surges far past typical hourly spend."""
    if normal_hourly_spend > 0 and hourly_spend >= max(5.0, normal_hourly_spend * threshold_factor):
        return {
            "kind": "spend_spike",
            "hourly_spend": hourly_spend,
            "normal": normal_hourly_spend,
            "message": f"Spend spike: ${hourly_spend:.2f}/hour is {hourly_spend / normal_hourly_spend:.1f}x normal (${normal_hourly_spend:.2f}/hour).",
        }
    return None


def check_slow_leak(runs: List[dict]) -> Optional[dict]:
    """Check the share of runs that end 'successfully' while doing no actual work / no tool actions."""
    if not runs or len(runs) < 10:
        return None
    zero_action = sum(1 for r in runs if r.get("status") in ("completed", "pass") and not r.get("tool_actions"))
    ratio = zero_action / len(runs)
    total_cost = sum(r.get("cost") or 0.0 for r in runs)
    if ratio >= 0.5 and total_cost > 1.0:
        return {
            "kind": "slow_leak",
            "ratio": ratio,
            "zero_action_count": zero_action,
            "total_runs": len(runs),
            "cost": total_cost,
            "message": f"Slow leak: {ratio:.0%} of completed runs performed no actions (spent ${total_cost:.2f}).",
        }
    return None


def check_and_alert_runaway(engine: Engine, tenant: str, run_id: str,
                            steps: List[dict], notifier: Optional[Callable[[str, dict], Any]] = None) -> Optional[dict]:
    """Scan a run for immediate runaway behavior and record/dispatch an alert at once."""
    problem = check_repeated_failure(steps) or check_runaway_size(len(steps))
    if not problem:
        return None

    now = datetime.utcnow()
    source = f"events:{tenant}"
    a = store.alerts
    alert_row = dict(
        source=source,
        measure_id="runaway",
        dimension="run_id",
        slice_value=run_id,
        kind="anomaly",
        state="open",
        streak=1,
        clear_streak=0,
        opened_at=now,
        last_seen_at=now,
        value=float(problem.get("count") or problem.get("steps") or 1.0),
        message=problem["message"],
    )

    with engine.begin() as conn:
        existing = conn.execute(select(a.c.id).where(and_(
            a.c.source == source,
            a.c.measure_id == "runaway",
            a.c.slice_value == run_id,
            a.c.state == "open"
        ))).first()

        if existing:
            conn.execute(a.update().where(a.c.id == existing[0]).values(
                last_seen_at=now,
                value=alert_row["value"],
                message=alert_row["message"]
            ))
            alert_id = existing[0]
        else:
            alert_id = conn.execute(a.insert().values(**alert_row)).inserted_primary_key[0]

    full_alert = {"id": alert_id, **alert_row}
    if notifier:
        try:
            notifier("opened", full_alert)
        except Exception:
            log.exception("Notifier failed on runaway alert")
    return full_alert
