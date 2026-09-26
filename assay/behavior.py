"""How an agent run behaved, beyond whether its answer was right, compared with its baseline.

A run can pass every check and still get worse: cost twice as much, take three times as long,
drag a context that keeps growing, offer the model forty tools instead of eight, stop resolving
what it was asked, or decide differently about something that needs approval. Per test case:

  cost_usd        what the run's model and tool calls cost
  seconds         how long the run took, start to end
  steps           model calls, tool calls, state changes, approvals and the answer
  context_tokens  the largest input any model call got: how big the context grew
  input_tokens    all the input the run's model calls got: what the bill counts
  fragments       the most retrieved fragments one query put into the prompt (run.retrieve())
  retrieved_tokens  the most tokens of fragments one query put into the prompt, with their share
                  of the prompt that took them
  tools_exposed   the most tools any model call was offered
  outcome         resolved, unresolved or escalated (run.outcome())
  approvals       the last decision per action (run.approval())

A number counts as worse when it grew by RATIO and by at least MIN (so 2 cents → 3 cents isn't
news, and neither is 40 ms → 80 ms); an outcome that was resolved and isn't; an approval that
changed its decision. With several attempts, a case's number is their median.

Each case can stay under its ratio while the whole run triples: a few hundred tokens more on
every query. suite_totals() compares the sums over the cases both runs have (input tokens, cost,
retrieved tokens) at their own ratio. limits() are absolute: no query over max_fragments,
max_retrieved_tokens or max_context_tokens, whatever the baseline was.
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
    "input_tokens": (1.5, 500, lambda v: f"{v:,.0f} tokens"),
    "fragments": (1.5, 2, lambda v: f"{v:g} fragments"),
    "retrieved_tokens": (1.5, 200, lambda v: f"{v:,.0f} tokens"),
    "tools_exposed": (1.5, 2, lambda v: f"{v:g} tools"),
}
LABELS = {"cost_usd": "Cost", "seconds": "Latency", "steps": "Steps", "context_tokens": "Context",
          "input_tokens": "Input tokens", "fragments": "Retrieved fragments", "retrieved_tokens": "Retrieved tokens",
          "tools_exposed": "Tools exposed", "outcome": "Outcome", "approvals": "Approval"}
SUITE = {  # whole-run totals: (minimum increase, how to show)
    "input_tokens": (1000, lambda v: f"{v:,.0f}"),
    "cost_usd": (0.01, lambda v: f"${v:,.2f}"),
    "retrieved_tokens": (500, lambda v: f"{v:,.0f}"),
}
SUITE_LABELS = {"input_tokens": "Input tokens", "cost_usd": "Cost", "retrieved_tokens": "Retrieved tokens"}
SUITE_RATIO = 1.25
LIMITS = ("max_fragments", "max_retrieved_tokens", "max_context_tokens")
LIMIT_EVALUATOR = "assay.limits@1"
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
    rets = [s for s in steps if s["kind"] == "retrieval"]
    biggest = max(rets, key=lambda s: ((s.get("args") or {}).get("tokens_used") or 0), default=None)
    return {
        "cost_usd": round(sum(s.get("cost_usd") or 0 for s in steps), 6),
        "seconds": round((finished - started).total_seconds(), 3) if started and finished else None,
        "steps": len(steps),
        "context_tokens": max((s.get("tokens_in") or 0 for s in calls), default=0) or None,
        "tools_exposed": max((len(s.get("tools") or []) for s in calls), default=0) or None,
        "input_tokens": sum(s.get("tokens_in") or 0 for s in calls) or None,
        "fragments": max(((s.get("args") or {}).get("used") or 0 for s in rets), default=None) if rets else None,
        "retrieved_tokens": ((biggest.get("args") or {}).get("tokens_used") or 0) if biggest else None,
        "context_share": _share(steps, biggest),
        "retriever": biggest.get("name") if biggest else None,
        "outcome": traj.get("outcome"),
        "approvals": approvals or None,
    }


def _share(steps: List[dict], ret: Optional[dict]) -> Optional[float]:
    """The retrieved tokens' share of the prompt of the next model call that got them."""
    if not ret:
        return None
    used = (ret.get("args") or {}).get("tokens_used") or 0
    nxt = next((s for s in steps if s["kind"] == "reason" and s["seq"] > ret["seq"] and s.get("tokens_in")), None)
    return round(min(1.0, used / nxt["tokens_in"]), 3) if nxt and used else None


def combine(runs: List[dict]) -> dict:
    """A case's behavior over its attempts: the median number, the most common outcome and decision."""
    out = {}
    for k in NUMBERS:
        vals = [r[k] for r in runs if r.get(k) is not None]
        out[k] = median(vals) if vals else None
    shares = [r["context_share"] for r in runs if r.get("context_share") is not None]
    out["context_share"] = median(shares) if shares else None
    names = [r["retriever"] for r in runs if r.get("retriever")]
    out["retriever"] = Counter(names).most_common(1)[0][0] if names else None
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
    if before.get("input_tokens") == before.get("context_tokens") and now.get("input_tokens") == now.get("context_tokens"):
        out = [c for c in out if c["metric"] != "input_tokens"]  # one model call: the same number twice
    retrieval = [c for c in out if c["metric"] in ("fragments", "retrieved_tokens")]
    if retrieval:  # one line for what retrieval put in the prompt, before and after: first, it's the cause
        out = [c for c in out if c not in retrieval]
        out.insert(0, {"metric": "retrieved_context", "flagged": [c["metric"] for c in retrieval],
                    "before": {k: before.get(k) for k in ("fragments", "retrieved_tokens", "context_share")},
                    "now": {k: now.get(k) for k in ("fragments", "retrieved_tokens", "context_share")},
                    "text": retrieved_text(before, now)})
    if before.get("outcome") == "resolved" and now.get("outcome") in WORSE_OUTCOME:
        out.append({"metric": "outcome", "before": "resolved", "now": now["outcome"],
                    "text": f"Outcome: resolved → {now['outcome']}"})
    for action, was in (before.get("approvals") or {}).items():
        is_ = (now.get("approvals") or {}).get(action)
        if is_ and was and is_ != was:
            out.append({"metric": "approvals", "before": was, "now": is_,
                        "text": f"Approval for {action}: {was} → {is_}"})
    return out


def _context(m: dict) -> str:
    n, t = m.get("fragments"), m.get("retrieved_tokens")
    return "nothing retrieved" if n is None else f"{n:g} fragment{'s' * (n != 1)}, {t or 0:,.0f} tokens"


def retrieved_text(before: dict, now: dict) -> str:
    """Retrieved context (search_docs): 3 fragments, 420 tokens → 12 fragments, 2,900 tokens (68% of the prompt)"""
    where = f" ({now.get('retriever') or before.get('retriever')})" if now.get("retriever") or before.get("retriever") else ""
    share = f" ({now['context_share']:.0%} of the prompt)" if now.get("context_share") else ""
    return f"Retrieved context{where}: {_context(before)} → {_context(now)}{share}"


def suite_totals(now: Dict[str, dict], before: Dict[str, dict], cases: List[str],
                 ratio: float = SUITE_RATIO) -> List[dict]:
    """Totals over the cases both runs have that grew by `ratio` (and SUITE's minimum): the run's
    cost can triple while no case passes its own ratio."""
    if not ratio:
        return []
    out = []
    for k, (least, show) in SUITE.items():
        both = [c for c in cases if now.get(c, {}).get(k) is not None and before.get(c, {}).get(k) is not None]
        if not both:
            continue
        a, b = sum(before[c][k] for c in both), sum(now[c][k] for c in both)
        if b - a >= least and (a == 0 or b >= a * ratio):
            times = f" ({b / a:.1f}×)" if a else ""
            grew = sorted(both, key=lambda c: now[c][k] - before[c][k], reverse=True)
            out.append({"metric": k, "before": a, "now": b, "cases": len(both),
                        "text": f"{SUITE_LABELS[k]} for the whole run: {show(a)} → {show(b)}{times}, over the "
                                f"{len(both):,} case{'s' * (len(both) != 1)} in both runs",
                        "most": [{"case_id": c, "before": before[c][k], "now": now[c][k]} for c in grew[:3]
                                 if now[c][k] > before[c][k]]})
    return out


def limits(traj: dict, cfg: Dict[str, Optional[float]]) -> List[dict]:
    """The [behavior] limits one run is held to: [{"field", "status", "reason", "expected", "actual"}]."""
    out = []
    steps = traj["steps"]
    rets = [s for s in steps if s["kind"] == "retrieval"]
    calls = [s for s in steps if s["kind"] == "reason" and s.get("tokens_in")]
    for key, pool, value, what in (
            ("max_fragments", rets, lambda s: (s.get("args") or {}).get("used") or 0,
             lambda s, v, n: f"{s.get('name') or 'retrieval'} (step {s['seq']}) put {v:,} fragments in the prompt"),
            ("max_retrieved_tokens", rets, lambda s: (s.get("args") or {}).get("tokens_used") or 0,
             lambda s, v, n: f"{s.get('name') or 'retrieval'} (step {s['seq']}) put {v:,} tokens of fragments in "
                             f"the prompt"),
            ("max_context_tokens", calls, lambda s: s.get("tokens_in") or 0,
             lambda s, v, n: f"the model call at step {s['seq']} got {v:,} tokens of input")):
        limit = cfg.get(key)
        if limit is None or not pool:
            continue
        worst = max(pool, key=value)
        v = value(worst)
        over = [s for s in pool if value(s) > limit]
        out.append({"field": key, "status": "fail" if over else "pass", "expected": f"≤ {limit:,.0f}",
                    "actual": f"{v:,}",
                    "reason": f"{what(worst, v, len(over))}, over the limit of {limit:,.0f}"
                              + (f" ({len(over)} times)" if len(over) > 1 else "") if over else None})
    return out
