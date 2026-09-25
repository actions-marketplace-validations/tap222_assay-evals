"""How an agent run behaved, beyond whether its answer was right, compared with its baseline.

A run can pass every check and still get worse: cost twice as much, take three times as long,
drag a context that keeps growing, offer the model forty tools instead of eight, stop resolving
what it was asked, or decide differently about something that needs approval. Per test case:

  cost_usd        what the run's model and tool calls cost
  seconds         how long the run took, start to end
  steps           model calls, tool calls, state changes, approvals and the answer
  context_tokens  the largest input any model call got: how big the context grew
  tools_exposed   the most tools any model call was offered
  outcome         resolved, unresolved or escalated (run.outcome())
  approvals       the last decision per action (run.approval())

A number counts as worse when it grew by RATIO and by at least MIN (so 2 cents → 3 cents isn't
news, and neither is 40 ms → 80 ms); an outcome that was resolved and isn't; an approval that
changed its decision. With several attempts, a case's number is their median.
"""
from __future__ import annotations

from collections import Counter
from statistics import median
from typing import Dict, List, Optional

NUMBERS = {  # name: (default ratio, minimum increase, how to show a value)
    "cost_usd": (1.5, 0.001, lambda v: f"${v:.4f}"),
    "seconds": (1.5, 1.0, lambda v: f"{v:.1f}s"),
    "steps": (1.5, 2, lambda v: f"{v:g} steps"),
    "context_tokens": (1.5, 500, lambda v: f"{v:,.0f} tokens"),
    "tools_exposed": (1.5, 2, lambda v: f"{v:g} tools"),
}
LABELS = {"cost_usd": "Cost", "seconds": "Latency", "steps": "Steps", "context_tokens": "Context",
          "tools_exposed": "Tools exposed", "outcome": "Outcome", "approvals": "Approval"}
WORSE_OUTCOME = {"unresolved", "escalated"}


def measure(traj: dict) -> dict:
    """One run's behavior, from its trajectory (assay/sources/events.py)."""
    steps = traj["steps"]
    calls = [s for s in steps if s["kind"] == "reason"]
    started, finished = traj.get("started_at"), traj.get("finished_at")
    approvals = {}
    for s in steps:
        if s["kind"] == "approval":
            approvals[s["name"]] = (s.get("args") or {}).get("decision")
    return {
        "cost_usd": round(sum(s.get("cost_usd") or 0 for s in steps), 6),
        "seconds": round((finished - started).total_seconds(), 3) if started and finished else None,
        "steps": len(steps),
        "context_tokens": max((s.get("tokens_in") or 0 for s in calls), default=0) or None,
        "tools_exposed": max((len(s.get("tools") or []) for s in calls), default=0) or None,
        "outcome": traj.get("outcome"),
        "approvals": approvals or None,
    }


def combine(runs: List[dict]) -> dict:
    """A case's behavior over its attempts: the median number, the most common outcome and decision."""
    out = {}
    for k in NUMBERS:
        vals = [r[k] for r in runs if r.get(k) is not None]
        out[k] = median(vals) if vals else None
    outcomes = [r["outcome"] for r in runs if r.get("outcome")]
    out["outcome"] = Counter(outcomes).most_common(1)[0][0] if outcomes else None
    actions = {a for r in runs for a in (r.get("approvals") or {})}
    out["approvals"] = {a: Counter((r.get("approvals") or {}).get(a) for r in runs
                                   if (r.get("approvals") or {}).get(a)).most_common(1)[0][0]
                        for a in actions} or None
    return out


def compare(now: dict, before: dict, ratios: Optional[Dict[str, float]] = None) -> List[dict]:
    """What got worse since the baseline: [{"metric", "before", "now", "text"}]."""
    ratios = ratios or {}
    out = []
    for k, (ratio, least, show) in NUMBERS.items():
        a, b = before.get(k), now.get(k)
        r = ratios.get(k, ratio)
        if a is None or b is None or not r:
            continue
        if b - a >= least and (a == 0 or b >= a * r):
            times = f" ({b / a:.1f}×)" if a >= least else ""  # a ratio to next to nothing says nothing
            out.append({"metric": k, "before": a, "now": b, "text": f"{LABELS[k]}: {show(a)} → {show(b)}{times}"})
    if before.get("outcome") == "resolved" and now.get("outcome") in WORSE_OUTCOME:
        out.append({"metric": "outcome", "before": "resolved", "now": now["outcome"],
                    "text": f"Outcome: resolved → {now['outcome']}"})
    for action, was in (before.get("approvals") or {}).items():
        is_ = (now.get("approvals") or {}).get(action)
        if is_ and was and is_ != was:
            out.append({"metric": "approvals", "before": was, "now": is_,
                        "text": f"Approval for {action}: {was} → {is_}"})
    return out
