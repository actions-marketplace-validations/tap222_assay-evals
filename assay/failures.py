"""Finding patterns across failures: many failures, a few causes, and what kind each is.

5,000 failed checks are rarely 5,000 problems. This groups them into causes
and says which of four kinds each cause is, with the evidence:

- ai               an AI (or code) step got the value wrong. Flagged as a
                   regression when it's newly failing, or worse than the
                   version before, or started at a point in time.
- infrastructure   the step failed or timed out, a fallback answered, the
                   value was lost after the pipeline, or the test harness
                   couldn't reach something.
- evaluator        the check is wrong, not the output: the values differ
                   only in format, the output is in the document and the
                   expected value isn't, or another judgement of the same
                   output passed.
- intended_change  the output changed on purpose: newly failing with a
                   release, consistently, and still correct once formatting
                   is ignored. A person confirms it and updates expectations.
- unsure           the evidence doesn't point anywhere. Said plainly rather
                   than forced into a kind.

How it works. Each failure is traced through the document's steps
(assay/rootcause.py) and classified on its own evidence. Failures are then
grouped by (kind, mechanism, step). Each group is described by what sets its
failures apart from the passes, not by what they merely share: a feature is
listed only if it is much more common among the group's failures than among
passing cases (lift, with a significance test), so "they're all invoices"
doesn't make the list when most cases are invoices. Group-level evidence then
settles the kind: whether the failures are new since the previous run, when
they started, whether they came in a burst, and which release they started with.

Nothing here asks an LLM. Every kind comes from rules whose evidence is shown.
"""
from __future__ import annotations

import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import flaky, store
from assay.cost import is_fallback
from assay.models import FAILED_STATUSES, ErrorReport, Window
from assay.rootcause import (VERDICTS, _text_forms, _texts, analyze_document, localize, normalize, order_steps,
                             transcribed)

KINDS = {
    "ai": "AI / pipeline error",
    "infrastructure": "Infrastructure",
    "evaluator": "Evaluator",
    "intended_change": "Intended change",
    "unsure": "Unsure",
}
DECISIONS = ("accepted_change", "not_a_problem", "confirmed")
INFRA_REASON = re.compile(r"time ?out|timed out|connection|refused|unavailable|unreachable|\b5\d\d\b|rate.?limit|"
                          r"quota|out of memory|\boom\b|dns|reset by peer|throttl", re.I)
MAX_POPULATION = 6000  # documents or results compared against
EXAMPLES = 8
MIN_GROUP = 3  # smaller groups are folded into "other"


# ---------- one failure ----------

def diff_shape(expected, actual) -> str:
    """How the actual value differs from the expected one."""
    e = None if expected in (None, "") else str(expected)
    a = None if actual in (None, "") else str(actual)
    if e is None and a is None:
        return "no_values"
    if a is None:
        return "missing"
    if e is None:
        return "unexpected"
    if e == a:
        return "identical"
    ne, na = normalize(e), normalize(a)
    if ne == na:
        return "format_only"
    iso = re.compile(r"(\d{4})-(\d{2})-(\d{2})$")
    me, ma = iso.match(ne or ""), iso.match(na or "")
    if me and ma:
        if me[1] == ma[1] and me[2] == ma[3] and me[3] == ma[2]:
            return "date_swap"
        return "date_other"
    num = re.compile(r"-?\d+(\.\d+)?$")
    if num.match(ne or "") and num.match(na or ""):
        x, y = float(ne), float(na)
        if x and y and abs(math.log10(abs(y / x)) - round(math.log10(abs(y / x)))) < 1e-9 and y != x:
            return "scale"
        return "number_off"
    if ne and na and (ne.startswith(na) or na.startswith(ne)):
        return "truncated"
    if not transcribed(e) and not transcribed(a):
        return "label_mismatch"
    return "different_text"


SHAPES = {"missing": "missing", "unexpected": "shouldn't be there", "identical": "identical, yet failed",
          "format_only": "same value, different format", "date_swap": "day and month swapped",
          "date_other": "wrong date", "scale": "off by a power of ten", "number_off": "wrong number",
          "truncated": "cut short", "label_mismatch": "wrong label", "different_text": "different value",
          "no_values": "no values recorded"}


def reason_pattern(reason: Optional[str]) -> Optional[str]:
    """An evaluator's reason with the specifics masked, so the same complaint groups together."""
    if not reason:
        return None
    s = reason.strip().lower()
    s = re.sub(r"(['\"`]).*?\1", "'…'", s)
    s = re.sub(r"\d+(\.\d+)?", "#", s)
    return re.sub(r"\s+", " ", s)[:90]


def _pages(n: Optional[int]) -> Optional[str]:
    return None if n is None else "1-2" if n <= 2 else "3-9" if n <= 9 else "10+"


def doc_features(detail) -> set:
    """What a document's run looked like, as features to compare failures with passes."""
    doc, runs, calls = detail
    f = set()
    for k in ("document_type", "segment", "processing_mode"):
        v = getattr(doc, k, None)
        if v:
            f.add(f"{k}={v}")
    if getattr(doc, "page_count", None) is not None:
        f.add(f"pages={_pages(doc.page_count)}")
    for r in runs:
        if r.prompt:
            f.add(f"prompt:{r.stage}={r.prompt}")
        if r.status in FAILED_STATUSES:
            f.add(f"failed:{r.stage}")
    for c in calls:
        if c.prompt:
            f.add(f"prompt:{c.stage}={c.prompt}")
        if c.model_served:
            f.add(f"model:{c.stage}={c.model_served}")
        if is_fallback(c):
            f.add(f"fallback:{c.stage}")
        if c.status in FAILED_STATUSES:
            f.add(f"call_failed:{c.stage}")
        if c.code_revision:
            f.add(f"build={c.code_revision}")
    return f


def _in_text(value, steps) -> bool:
    if value in (None, "") or not transcribed(value):
        return False
    forms = _text_forms(str(value))
    return any(fm in t for s in steps for t in _texts(s.outputs) for fm in forms)


def _ai_stage(stage: Optional[str], runs, calls) -> bool:
    return bool(stage) and (any(c.stage == stage for c in calls) or any(r.stage == stage and r.prompt for r in runs))


def trace_failure(f: dict, detail, other_errors: Optional[List[ErrorReport]] = None) -> dict:
    """Add where the failure started (verdict, origin step, signals) from the document's trace."""
    if detail is None:
        return f | {"verdict": "unlocalized", "origin_stage": None, "signals": [], "origin_ai": False,
                    "explanation": "No trace of this case was sent, so it can't be followed step by step.",
                    "text_actual": False, "text_expected": False, "doc_failed": []}
    doc, runs, calls = detail
    steps = order_steps(runs)
    doc_failed = sorted({r.stage for r in runs if r.status in FAILED_STATUSES})
    base = {"text_actual": _in_text(f["actual"], steps), "text_expected": _in_text(f["expected"], steps),
            "doc_failed": doc_failed}
    if not f.get("field") or (f["expected"] in (None, "") and f["actual"] in (None, "")):
        return f | base | {"verdict": "unlocalized", "origin_stage": None, "signals": [], "origin_ai": False,
                           "explanation": "A whole-case check, or no values to compare, so there's no step to trace."}
    err = ErrorReport(error_id=f["id"], document_id=f["document_id"], field=f["field"], reported_at=f["ts"],
                      expected=f["expected"], observed=f["actual"],
                      kind="missing" if f["actual"] in (None, "") else "extra" if f["expected"] in (None, "")
                      else "wrong")
    if other_errors:
        a = analyze_document(None, f["document_id"], other_errors, detail=detail)
        r = next((x for x in a["errors"] if x["error_id"] == f["id"]), None) or localize(err, runs, calls)
    else:
        r = localize(err, runs, calls)
    # The first step whose value is exactly what the check saw: where a formatting change came from.
    made = next((t for t in r["timeline"] if f["actual"] not in (None, "") and t["value"] is not None
                 and str(t["value"]) == str(f["actual"])), None)
    return f | base | {"verdict": r["verdict"], "origin_stage": r["origin_stage"], "signals": r["signals"],
                       "origin_prompt": r["origin_prompt"], "explanation": r["explanation"],
                       "origin_ai": _ai_stage(r["origin_stage"], runs, calls),
                       "value_origin": made["stage"] if made else None, "value_origin_prompt": made["prompt"] if made
                       else None, "value_origin_ai": _ai_stage(made["stage"], runs, calls) if made else None}


def trace_agent(f: dict, traj: dict, ref: Optional[dict], rules: List[dict]) -> dict:
    """For an agent trajectory: the first bad step, by credit assignment (assay/agents.py)."""
    from assay import agents
    why = agents.credit(traj, ref, rules, f["field"] if f.get("field") in agents.CHECKS else None)
    return f | {"agent": True, "verdict": why["mechanism"], "origin_stage": why["stage"], "signals": [],
                "explanation": why["detail"], "origin_ai": True, "expected_tool": why.get("expected_tool"),
                "tool_error": why.get("error"), "origin_prompt": (traj.get("lineage") or {}).get("prompt"),
                "text_actual": False, "text_expected": False, "doc_failed": [], "value_origin": None,
                "value_origin_prompt": None, "step": why.get("seq")}


def classify(f: dict) -> Tuple[str, str, List[str]]:
    """(kind, mechanism, evidence) for one failure, from its own evidence only."""
    reason, stage = f.get("reason") or "", f.get("origin_stage")
    shape, verdict = f["shape"], f.get("verdict")
    if f["status"] == "error":
        if INFRA_REASON.search(reason):
            return "infrastructure", "harness", [f"The check couldn't run: {reason[:160]}"]
        return "evaluator", "evaluator_error", [f"The evaluator itself failed: {reason[:160] or 'no reason given'}"]
    if f.get("audit"):  # assay/audit.py: what it graded isn't what the run did
        return "evaluator", "wrong_inputs", [f"{f.get('evaluator') or 'The evaluator'} was given data that doesn't "
                                             f"match the trace: {f['audit'][0]}."]
    if f.get("agent") and not f.get("disagreement"):
        if verdict == "tool_error" and INFRA_REASON.search(f.get("tool_error") or ""):
            return "infrastructure", "tool_unavailable", [f.get("explanation", "")]
        return "ai", verdict, [f.get("explanation", "")]
    if f.get("disagreement"):
        return "evaluator", "disagreement", [f"{f.get('evaluator') or 'The evaluator'} passed this same output on "
                                             "another attempt."]
    if shape == "identical":
        return "evaluator", "identical", ["Expected and actual are identical, yet the check failed."]
    if shape == "format_only":
        return "evaluator", "format_only", [
            f"Expected {f['expected']!r} and got {f['actual']!r}: the same value written differently."]
    if shape == "missing" and f.get("doc_failed"):
        return "infrastructure", "step_failed", [f"{', '.join(f['doc_failed'])} failed on this document, and the "
                                                 "value is missing."]
    failed_here = [s for s in f.get("signals", []) if s.startswith("the step itself failed")
                   or s.startswith("a model call returned")]
    if failed_here:
        return "infrastructure", "step_failed", [f"At {stage}: {failed_here[0]}."]
    # A fallback is infrastructure when the primary was unavailable; when it answered
    # because the primary wasn't confident, its wrong answer is still the AI's.
    fallback = [s for s in f.get("signals", []) if "fallback" in s or "served by" in s]
    if fallback and INFRA_REASON.search(" ".join(fallback)):
        return "infrastructure", "fallback", [f"At {stage}: {fallback[0]}."]
    if f.get("flake") == "output":
        c = f["check"]
        return "ai", "nondeterministic", [
            f"Passed {c['passed']} of {c['attempts']} attempts ({c['outcomes']}); the output changes between "
            f"attempts: {', '.join(repr(o) for o in f.get('outputs', []))}."]
    if f.get("flake") == "infrastructure":
        c = f["check"]
        return "infrastructure", "attempt_errors", [
            f"Attempts errored ({c['outcomes']}), so the pass rate is {c['passed']} of {c['attempts']} judged."]
    if verdict == "upstream":
        if f.get("text_actual") and not f.get("text_expected"):
            return "evaluator", "expected_not_in_source", [
                f"The output {f['actual']!r} is in the document's text and the expected {f['expected']!r} isn't: "
                "the expected value is probably wrong."]
        if f.get("origin_ai"):
            return "ai", "lost_in_text", [f.get("explanation", "")]
        return "unsure", "input", ["The value isn't in the text step's output: the source document is unreadable, "
                                   "or the text step lost it."]
    if verdict == "after":
        if f["origin"] == "eval":
            return "evaluator", "reads_other_value", [
                "Every step produced the expected value, but the check saw a different one: it's reading the "
                "wrong output or field."]
        return "infrastructure", "delivery", [f.get("explanation", "")]
    if verdict in ("introduced", "corrupted", "dropped", "caused_by"):
        where = "an AI step" if f.get("origin_ai") else "a code step"
        also = f" (also: {'; '.join(fallback)})" if fallback else ""
        return "ai", verdict, [f.get("explanation", ""), f"{stage} is {where}; no infrastructure problem there{also}."]
    if f["status"] == "fail" and not f.get("field"):
        return "unsure", "check_failed", [f"{f.get('evaluator') or 'The check'} failed the case: "
                                          f"{reason[:160] or 'no reason given'}"]
    return "unsure", "unlocalized", [f.get("explanation") or "Nothing to trace this failure with."]


def settle(f: dict) -> dict:
    f["kind"], f["mechanism"], f["evidence"] = classify(f)
    if f["mechanism"] == "step_failed" and f.get("doc_failed"):
        f["origin_stage"] = f["doc_failed"][0]  # the step that failed, not where tracing ran out
    return f


# ---------- statistics ----------

def _z(x1: int, n1: int, x2: int, n2: int) -> float:
    """Two-proportion z: positive when the second rate is higher."""
    if not n1 or not n2:
        return 0.0
    p = (x1 + x2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2)) if 0 < p < 1 else 0.0
    return (x2 / n2 - x1 / n1) / se if se else 0.0


def _p(p: float) -> str:
    return "p < 0.001" if p < 0.001 else f"p = {p:.3f}"


def distinguishing(group_feats: List[set], pass_feats: List[set], limit: int = 4) -> List[dict]:
    """Features much more common in the group's failures than in passing cases."""
    n_g, n_p = len(group_feats), len(pass_feats)
    if n_g < MIN_GROUP or n_p < 20:
        return []
    cg, cp = Counter(), Counter()
    for s in group_feats:
        cg.update(s)
    for s in pass_feats:
        cp.update(s)
    out = []
    for feat, k in cg.items():
        support, base = k / n_g, cp.get(feat, 0) / n_p
        lift = support / base if base else float("inf")
        if support >= 0.5 and lift >= 1.5 and _z(cp.get(feat, 0), n_p, k, n_g) >= 3:
            out.append({"feature": feat, "share": support, "base": base, "lift": None if base == 0 else round(lift, 1)})
    # Drop a feature implied by a stronger one on the same key (e.g. two prompt versions of one step).
    out.sort(key=lambda x: (-(x["lift"] or 99) * x["share"]))
    seen, keep = set(), []
    for x in out:
        key = x["feature"].split("=")[0]
        if key not in seen:
            keep.append(x)
            seen.add(key)
    return keep[:limit]


def burst(times: List[datetime], pop_times: List[datetime], width: timedelta) -> Optional[dict]:
    """Most of the group inside one short stretch that holds little of the population."""
    if len(times) < 5 or not pop_times:
        return None
    ts = sorted(times)
    best, j = (0, None), 0
    for i in range(len(ts)):
        while ts[i] - ts[j] > width:
            j += 1
        if i - j + 1 > best[0]:
            best = (i - j + 1, ts[j])
    count, start = best
    share = count / len(ts)
    pop_share = sum(1 for t in pop_times if start <= t <= start + width) / len(pop_times)
    if share >= 0.6 and pop_share <= 0.25:
        return {"start": start.isoformat(), "end": (start + width).isoformat(), "share": share,
                "population_share": pop_share}
    return None


def onset(times: List[datetime], pop_times: List[datetime], bins: int = 15) -> Optional[dict]:
    """The point after which the group's failure rate jumped, if there is one."""
    if len(times) < 5 or len(pop_times) < 50:
        return None
    lo, hi = min(pop_times), max(pop_times)
    if hi <= lo:
        return None
    step = (hi - lo) / bins
    fail = Counter(min(int((t - lo) / step), bins - 1) for t in times if lo <= t <= hi)
    pop = Counter(min(int((t - lo) / step), bins - 1) for t in pop_times)
    best = None
    for cut in range(2, bins - 1):
        fb, pb = sum(fail[b] for b in range(cut)), sum(pop[b] for b in range(cut))
        fa, pa = sum(fail[b] for b in range(cut, bins)), sum(pop[b] for b in range(cut, bins))
        if not pb or not pa:
            continue
        z = _z(fb, pb, fa, pa)
        ratio = (fa / pa) / (fb / pb) if fb else float("inf")
        if z >= 3 and ratio >= 3 and fa >= 5 and (best is None or z > best["z"]):
            best = {"at": (lo + step * cut).isoformat(), "z": round(z, 1), "rate_before": fb / pb,
                    "rate_after": fa / pa}
    return best


# ---------- grouping ----------

GROUP_NAMES = {
    ("ai", "introduced"): "{stage} gets {fields} wrong",
    ("ai", "corrupted"): "{stage} changes correct {fields} to wrong ones",
    ("ai", "dropped"): "{stage} drops {fields}",
    ("ai", "caused_by"): "{fields} wrong because an earlier decision was wrong",
    ("ai", "lost_in_text"): "{fields} lost before extraction (not in {stage}'s text)",
    ("infrastructure", "step_failed"): "{stage} failed, so {fields} are missing or wrong",
    ("infrastructure", "fallback"): "a fallback model answered at {stage}",
    ("infrastructure", "delivery"): "right in every step, wrong after the pipeline",
    ("infrastructure", "harness"): "the test harness couldn't run the check",
    ("infrastructure", "attempt_errors"): "some attempts errored on {fields}",
    ("ai", "nondeterministic"): "{stage} gives different {fields} from attempt to attempt",
    ("infrastructure", "tool_unavailable"): "{stage} was unavailable, and the agent went on without it",
    ("ai", "tool_error"): "{stage} errored and the agent didn't recover",
    ("ai", "unsafe_action"): "unsafe action: {stage}",
    ("ai", "looped"): "the agent loops on {stage}",
    ("ai", "wrong_tool"): "wrong tool: {stage} where {expected} was expected",
    ("ai", "bad_arguments"): "{stage} called with the wrong arguments",
    ("ai", "gave_up"): "the agent stops before calling {stage}",
    ("ai", "ignored_result"): "{stage} returned the answer, and the agent didn't use it",
    ("ai", "wrong_state"): "the calls look right, but {stage} left the world wrong",
    ("ai", "wrong_answer"): "the tool calls were right, the final answer wasn't",
    ("evaluator", "format_only"): "{evaluator} fails {fields} values that differ only in format",
    ("evaluator", "identical"): "{evaluator} fails identical values",
    ("evaluator", "disagreement"): "{evaluator} contradicts another judgement of the same output",
    ("evaluator", "expected_not_in_source"): "the expected {fields} isn't in the document, but the output is",
    ("evaluator", "reads_other_value"): "the check sees a different {fields} from the one the pipeline produced",
    ("evaluator", "evaluator_error"): "the evaluator errored",
    ("evaluator", "wrong_inputs"): "{evaluator} was given the wrong data",
    ("intended_change", "format_only"): "{fields} changed format with {change}",
    ("unsure", "input"): "{fields} not in the text: source quality or OCR",
    ("unsure", "check_failed"): "{evaluator}: {pattern}",
    ("unsure", "unlocalized"): "{fields} wrong, with nothing to trace them by",
}


def _top(values, k=3) -> str:
    c = Counter(v for v in values if v)
    if not c:
        return "values"
    names = [v for v, _ in c.most_common(k)]
    return ", ".join(names) + (" and others" if len(c) > k else "")


def group_failures(failures: List[dict], passes: List[set], pop_times: List[datetime],
                   changes: Optional[Dict[str, Tuple[str, str]]] = None, release_notes: Optional[dict] = None,
                   burst_width: timedelta = timedelta(days=1), is_eval: bool = False,
                   decisions: Optional[Dict[str, dict]] = None) -> dict:
    """Group classified failures into causes and settle each cause's kind."""
    changes, release_notes, decisions = changes or {}, release_notes or {}, decisions or {}
    buckets = defaultdict(list)
    for f in failures:
        pattern = reason_pattern(f.get("reason")) if f["mechanism"] in ("check_failed", "harness",
                                                                          "evaluator_error") else None
        scope = f.get("evaluator") if f["kind"] == "evaluator" else \
            (f.get("doc_failed") or [None])[0] if f["mechanism"] == "step_failed" and f.get("doc_failed") else \
            f.get("origin_stage")
        # In an eval run, newly failing and already failing are different causes even when they look alike.
        since = f.get("since") if is_eval and f.get("since") in ("new", "persisting", "flaky") and \
            f["kind"] in ("ai", "evaluator", "unsure") else ""
        # How the value is wrong separates causes that share a step (a swapped date vs a wrong vendor).
        shape = f["shape"] if f["kind"] in ("ai", "unsure") and f["mechanism"] != "check_failed" \
            and not f.get("agent") else ""
        buckets[(f["kind"], f["mechanism"], scope or "", pattern or "", since + shape)].append(f)

    groups, other = [], []
    total = len(failures) or 1
    for (kind, mech, scope, pattern, since), members in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        if len(members) < MIN_GROUP and len(buckets) > 1:
            other.extend(members)
            continue
        g = _describe(kind, mech, scope, pattern, members, passes, pop_times, changes, release_notes,
                      burst_width, is_eval, total)
        g["key"] += f"|{since}" if since else ""
        groups.append(g)
    if other:
        groups.append(_describe("unsure", "other", "", "", other, passes, pop_times, changes, release_notes,
                                burst_width, is_eval, total, small=True))
    for g in groups:
        g["decision"] = decisions.get(g["key"])
    groups.sort(key=lambda g: (g["decision"] is not None and g["decision"]["decision"] != "confirmed",
                               g["kind"] == "unsure", -g["failures"]))
    by_kind = Counter()
    for g in groups:
        by_kind[g["kind"]] += g["failures"]
    open_groups = [g for g in groups if not g["decision"] or g["decision"]["decision"] == "confirmed"]
    explained = sum(g["failures"] for g in groups if g["kind"] != "unsure" and g["confidence"] != "low")
    return {"failures": len(failures), "causes": len([g for g in groups if not g.get("small")]),
            "by_kind": [{"kind": k, "label": KINDS[k], "failures": by_kind.get(k, 0)} for k in KINDS],
            "explained": explained / total if failures else None,
            "needs_action": sum(g["failures"] for g in open_groups if g["kind"] in ("ai", "infrastructure")),
            "groups": groups}


def _describe(kind, mech, scope, pattern, members, passes, pop_times, changes, release_notes, burst_width,
              is_eval, total, small=False) -> dict:
    n = len(members)
    evidence, evidence_kinds = [], Counter(m["kind"] for m in members)
    shapes = Counter(m["shape"] for m in members)
    fields = _top([m.get("field") for m in members])
    feats = distinguishing([m["features"] for m in members], passes)
    times = [m["ts"] for m in members if m.get("ts")]
    new = [m for m in members if m.get("since") == "new"]
    known = [m for m in members if m.get("since") in ("new", "persisting")]
    new_share = len(new) / len(known) if known else None
    flaky_n = sum(1 for m in members if m.get("since") == "flaky")
    # Attempts pooled across the group's checks: is the drop beyond chance, whatever each case says alone?
    pool = flaky.pooled([m["check"] for m in members if m.get("check")]) if is_eval else None
    beyond = pool is None or pool["p"] <= 0.01
    pool_text = (f"Pooled over {pool['checks']:,} checks: {pool['base_passed']:,} of {pool['base_attempts']:,} "
                 f"attempts passed before, {pool['passed']:,} of {pool['attempts']:,} now "
                 f"({_p(pool['p'])}).") if pool else None
    b = burst(times, pop_times, burst_width) if not small else None
    o = onset(times, pop_times) if not small and not is_eval else None
    regression, change = None, None

    # Which change between runs these failures line up with: the one to the prompt
    # at their origin step if there is one, otherwise every change (said so).
    if changes:
        prompt = Counter(m.get("origin_prompt") or m.get("value_origin_prompt") for m in members
                         if m.get("origin_prompt") or m.get("value_origin_prompt")).most_common(1)
        match = [k for k, (_, cur) in changes.items() if prompt and cur == prompt[0][0]]
        if not match and sum(1 for m in members if m.get("value_origin") and not m.get("value_origin_ai")) / n >= 0.8:
            # The values came from a code step: a prompt change can't explain them.
            match = [k for k, (_, cur) in changes.items() if "@" not in str(cur)]
            match = match if len(match) == 1 else []
        keys = match[:1] or list(changes)
        change = {"key": ", ".join(keys), "from": "; ".join(str(changes[k][0]) for k in keys),
                  "to": "; ".join(str(changes[k][1]) for k in keys),
                  "text": "; ".join(f"{k} {changes[k][0]} → {changes[k][1]}" for k in keys),
                  "specific": bool(match) or len(changes) == 1,
                  "note": " ".join(release_notes[changes[k][1]] for k in keys if changes[k][1] in release_notes) or None}

    dom_shape, dom_n = shapes.most_common(1)[0]
    original = kind
    consistent = dom_n / n >= 0.8

    if kind == "evaluator" and mech == "format_only" and ((new_share is not None and new_share >= 0.8 and change
                                                            and beyond) or (not is_eval and o)):
        kind = "intended_change"
        evidence.append("Newly failing" + (f" since {change['text']}" if change else
                                           f" since {o['at'][:10]}") + ", consistently, and the new values are "
                        "still correct once formatting is ignored: the output changed on purpose, not by mistake.")
        if change and change.get("note"):
            evidence.append(f"Release note: “{change['note']}”.")
    elif kind == "evaluator" and mech == "format_only":
        evidence.append("Already failing before any change" if new_share is not None and new_share < 0.2 else
                        "No release lines up with these failures" if is_eval else "No change in time lines up")
        evidence[-1] += ": the check is stricter than the output needs."

    if kind == "ai":
        if is_eval and flaky_n / n >= 0.8:
            regression = bool(pool and pool["p"] <= 0.01)
            evidence.append(("Flakier than before. " if regression else
                             "Flaky, and no worse than before: it passes some attempts and fails others. ")
                            + (pool_text or ""))
        elif is_eval and new_share is not None and new_share >= 0.8 and not beyond:
            evidence.append(f"{len(new):,} of {len(known):,} passed before and fail now, but with this few attempts "
                            f"that could be chance. {pool_text} Rerun them before calling it a regression.")
        elif is_eval and new_share is not None:
            if new_share >= 0.8:
                regression = True
                evidence.append(f"{len(new):,} of {len(known):,} passed in the previous run" +
                                (f"; they started failing with {change['text']}." if change and change["specific"]
                                 else f"; between the runs these changed: {change['text']}." if change else "."))
                if change and change.get("note"):
                    evidence.append(f"Release note: “{change['note']}”.")
                if pool_text:
                    evidence.append(pool_text)
            elif new_share <= 0.2:
                regression = False
                evidence.append(f"{len(known) - len(new):,} of {len(known):,} were already failing in the previous "
                                "run: a long-standing weakness, not a regression.")
        elif o:
            regression = True
            evidence.append(f"Started around {o['at'][:10]}: {o['rate_before']:.2%} of documents before, "
                            f"{o['rate_after']:.2%} after.")
        elif not is_eval and len(times) >= 5:
            regression = False
            evidence.append("Steady across the window: no point where it started.")

    if kind == "unsure" and not small and new_share is not None and new_share >= 0.8 and change and beyond:
        kind, regression = "ai", True
        evidence.append(f"Nothing to trace, but {len(new):,} of {len(known):,} passed in the previous run and "
                        f"started failing with {change['text']}.")

    if b:
        span = (lambda a, z: f"{a[:16].replace('T', ' ')} and {z[11:16] if a[:10] == z[:10] else z[:16].replace('T', ' ')}")
        evidence.append(f"{b['share']:.0%} of these failures fall between {span(b['start'], b['end'])}, a stretch "
                        f"with {b['population_share']:.0%} of all cases.")
        if kind in ("ai", "unsure") and any(f["feature"].startswith(("failed:", "fallback:", "call_failed:"))
                                            for f in feats):
            kind = "infrastructure"

    # The per-failure evidence most members share, first.
    first = Counter(e for m in members for e in m["evidence"][:1] if e)
    if n == 1 or len(first) == 1:
        evidence.insert(0, members[0]["evidence"][0])
    elif mech not in ("other",):
        sample = members[0]["evidence"][0]
        evidence.insert(0, f"For example: {sample}")
    if consistent and dom_shape != "no_values" and not any(m.get("agent") for m in members) and \
            mech not in ("harness", "evaluator_error", "disagreement", "check_failed", "other"):
        evidence.append(f"{dom_n:,} of {n:,} are {SHAPES.get(dom_shape, dom_shape)}.")

    agree = evidence_kinds.get(original, 0) / n
    confidence = "low" if small or kind == "unsure" or agree < 0.7 else \
        "high" if agree >= 0.9 and len(evidence) >= 2 else "moderate"

    stage = scope if kind != "evaluator" else _top([m.get("origin_stage") for m in members], 1)
    name = "Other small groups" if small else GROUP_NAMES.get(
        (kind, mech), GROUP_NAMES.get((original, mech), "{fields}: {mechanism}")).format(
        stage=stage or "a step", fields=fields, evaluator=scope or _top([m.get("evaluator") for m in members], 1),
        expected=_top([m.get("expected_tool") for m in members], 1),
        pattern=pattern or "failed", mechanism=mech.replace("_", " "),
        change=change["to"] if change and change["specific"] else "a release")
    key = "|".join([kind if kind != "intended_change" else "evaluator", mech, scope, pattern])
    return {
        "cases": len({m.get("case_id") for m in members}),
        "key": key, "name": name if re.match(r"\S*[_@\d]", name) else name[0].upper() + name[1:], "kind": kind, "kind_label": KINDS[kind], "mechanism": mech,
        "confidence": confidence, "regression": regression, "failures": n, "share": n / total,
        "stage": scope if kind != "evaluator" else None, "evaluator": scope if kind == "evaluator" else None,
        "evidence": [e for e in evidence if e], "distinguishing": feats,
        "fields": dict(Counter(m.get("field") or "(whole case)" for m in members).most_common(6)),
        "shapes": {SHAPES.get(s, s): c for s, c in shapes.most_common(4)},
        "new": len(new) if known else None, "persisting": (len(known) - len(new)) if known else None,
        "flaky_checks": flaky_n, "pooled": pool,
        "change": change, "burst": b, "onset": o, "small": small,
        "examples": [{k: m.get(k) for k in ("id", "case_id", "document_id", "field", "expected", "actual",
                                            "shape", "verdict", "origin_stage", "evaluator", "reason", "since",
                                            "state", "check")}
                     | {"how": SHAPES.get(m["shape"], m["shape"])} for m in members[:EXAMPLES]],
        "member_ids": [m["id"] for m in members],
    }


# ---------- sources of failures ----------

def load_decisions(engine: Engine, source: str) -> Dict[str, dict]:
    t = store.failure_decisions
    with engine.connect() as conn:
        return {r.key: {"decision": r.decision, "note": r.note, "decided_by": r.decided_by,
                        "decided_at": r.decided_at.isoformat()}
                for r in conn.execute(select(t).where(t.c.source == source))}


def save_decision(engine: Engine, source: str, key: str, decision: Optional[str], note: Optional[str] = None,
                  by: Optional[str] = None) -> None:
    t = store.failure_decisions
    with engine.begin() as conn:
        conn.execute(t.delete().where(and_(t.c.source == source, t.c.key == key)))
        if decision:
            conn.execute(t.insert().values(source=source, key=key, decision=decision, note=note, decided_by=by,
                                           decided_at=datetime.utcnow()))


def _details(source, ids: List[str]) -> dict:
    if not ids:
        return {}
    if hasattr(source, "document_details"):
        return source.document_details(ids)
    return {i: d for i in ids if (d := source.document_detail(i))}


def production(source, window: Window, engine: Optional[Engine] = None) -> Optional[dict]:
    """Group the wrong values people reported in a window into causes."""
    errors = source.errors(window) if hasattr(source, "errors") else None
    if errors is None:
        return None
    docs = list(source.documents(window) or [])[-MAX_POPULATION:]
    by_doc = defaultdict(list)
    for e in errors:
        by_doc[e.document_id].append(e)
    details = _details(source, list({d.document_id for d in docs} | set(by_doc)))
    feats = {d: doc_features(det) for d, det in details.items()}
    failures = []
    for doc_id, errs in by_doc.items():
        det = details.get(doc_id)
        for e in errs:
            f = {"id": e.error_id, "origin": "production", "case_id": doc_id, "document_id": doc_id,
                 "field": e.field, "expected": e.expected, "actual": e.observed, "status": "fail",
                 "evaluator": e.source or e.reporter, "reason": None, "lineage": None, "since": None,
                 "ts": det[0].received_at if det else e.reported_at, "shape": diff_shape(e.expected, e.observed),
                 "features": feats.get(doc_id, set())}
            f = trace_failure(f, det, errs if len(errs) > 1 else None)
            failures.append(settle(f))
    passes = [feats[d.document_id] for d in docs if d.document_id not in by_doc and d.document_id in feats]
    notes = _release_notes(engine, source.name) if engine is not None else {}
    out = group_failures(failures, passes, [d.received_at for d in docs], release_notes=notes,
                         burst_width=timedelta(days=1), is_eval=False,
                         decisions=load_decisions(engine, source.name) if engine is not None else None)
    return out | {"scope": {"kind": "production", "window": [window.start.isoformat(), window.end.isoformat()],
                            "documents": len(docs)}, "population": len(docs)}


def _release_notes(engine: Engine, source_name: str) -> Dict[str, str]:
    from assay.prompts import registry, registry_tenant
    return {f"{pid}@{ver}": r["note"] for (pid, ver), r in registry(engine, registry_tenant(source_name)).items()
            if r.get("note")}


def eval_runs(engine: Engine, tenant: str) -> List[dict]:
    t = store.eval_results
    with engine.connect() as conn:
        rows = conn.execute(select(t.c.run_id, t.c.status, t.c.ts, t.c.lineage).where(t.c.tenant == tenant)).all()
        r = store.runs
        origins = {}
        for row in conn.execute(select(r.c.test_run, r.c.tags).where(and_(r.c.tenant == tenant,
                                                                         r.c.test_run.is_not(None)))):
            tags = row.tags or {}
            if row.test_run not in origins and (tags.get("repo") or tags.get("folder")):
                origins[row.test_run] = {k: tags[k] for k in ("repo", "folder") if tags.get(k)}
    runs = defaultdict(lambda: {"results": 0, "pass": 0, "fail": 0, "error": 0, "start": None, "end": None,
                                "lineage": Counter()})
    for r in rows:
        x = runs[r.run_id]
        x["results"] += 1
        x[r.status] += 1
        x["start"] = min(x["start"] or r.ts, r.ts)
        x["end"] = max(x["end"] or r.ts, r.ts)
        for k, v in (r.lineage or {}).items():
            x["lineage"][(k, v)] += 1
    out = []
    for run_id, x in runs.items():
        lineage = {}
        for (k, v), c in x["lineage"].most_common():
            lineage.setdefault(k, v)
        out.append({"run_id": run_id, "results": x["results"], "passed": x["pass"], "failed": x["fail"],
                    "errored": x["error"], "start": x["start"].isoformat(), "end": x["end"].isoformat(),
                    "lineage": lineage, "origin": origins.get(run_id)})
    return sorted(out, key=lambda r: r["start"], reverse=True)


def _results(engine: Engine, tenant: str, run_id: str) -> list:
    t = store.eval_results
    with engine.connect() as conn:
        return conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.run_id == run_id))).all()


def evaluation(engine: Engine, source, tenant: str, run_id: str, baseline: Optional[str] = None,
               tolerance: float = 0.01) -> Optional[dict]:
    """Group one evaluation run's failing checks into causes, compared with the run before it.

    A check is a case, field and evaluator; its attempts give it a pass rate. A check
    fails if any attempt did. Whether it got worse, is flaky, or needs reruns comes from
    assay/flaky.py, and a cause's regression verdict pools its checks' attempts."""
    rows = _results(engine, tenant, run_id)
    if not rows:
        return None
    from assay import audit
    audited = audit.audit_rows(engine, tenant, rows)  # results whose evaluator was given the wrong data
    runs = eval_runs(engine, tenant)
    this = next(r for r in runs if r["run_id"] == run_id)
    baseline = _baseline(runs, run_id, baseline)
    base_rows = _results(engine, tenant, baseline) if baseline else []
    prev_lineage = next((r["lineage"] for r in runs if r["run_id"] == baseline), {}) if baseline else {}
    changes = {k: (prev_lineage.get(k), v) for k, v in this["lineage"].items()
               if baseline and prev_lineage.get(k) != v}

    cand = flaky.attempts_by_check(rows)
    states = flaky.assess(cand, flaky.attempts_by_check(base_rows))
    details = _details(source, list({r.document_id for r in rows if r.document_id}))
    feats = {d: doc_features(det) for d, det in details.items()}
    # Agent trajectories: traced by their first bad step instead of by value.
    trajs = source.trajectories(list(details)) if hasattr(source, "trajectories") else {}
    agent_refs, rules = {}, []
    if trajs:
        from assay import agents, contracts
        agent_refs = agents.references(engine, tenant, {t["case_id"] for t in trajs.values() if t.get("case_id")})
        rules = contracts.load(engine, source.name)
        for d, t in trajs.items():
            feats[d] = feats.get(d, set()) | agents.features(t)

    def features_of(r):
        return feats.get(r.document_id, set()) | ({f"field={r.field}"} if r.field else set()) | \
            ({f"evaluator={r.evaluator}"} if r.evaluator else set()) | \
            {f"{k}={v}" for k, v in (r.lineage or {}).items()}

    failures, by_doc, passes = [], defaultdict(list), []
    for key, attempts in cand.items():
        x = states[key]
        bad = [r for r in attempts if r.status != "pass"]
        if not bad:
            passes.append(features_of(attempts[0]))
            continue
        r = next((a for a in bad if a.status == "fail"), bad[0])  # the failing attempt to trace
        f = {"id": r.result_id, "origin": "eval", "case_id": r.case_id, "document_id": r.document_id,
             "field": r.field, "expected": r.expected, "actual": r.actual, "status": r.status,
             "evaluator": r.evaluator, "reason": r.reason, "lineage": r.lineage or {}, "ts": r.ts,
             "since": x["since"], "state": x["state"], "flake": x["flake"] if x["state"] == "flaky" else None,
             "check": {k: x[k] for k in ("passed", "attempts", "rate", "low", "high", "base_passed",
                                         "base_attempts", "base_rate", "outcomes")},
             "outputs": sorted({str(a.actual) for a in attempts})[:4],
             "shape": diff_shape(r.expected, r.actual), "features": features_of(r),
             # The same output passed on another attempt: the evaluator is inconsistent.
             # The same output: the same value, or the same run. Two checks with no actual, from two
             # different runs, aren't the same output.
             "disagreement": any(a.status == "pass" and ((r.actual is not None and a.actual == r.actual)
                                                         or (r.document_id and a.document_id == r.document_id))
                                 for a in attempts),
             "audit": audited.get(r.result_id)}
        failures.append(f)
        if r.document_id and r.field and r.status == "fail":
            by_doc[r.document_id].append(f)
    traced = []
    for f in failures:
        det = details.get(f["document_id"]) if f["document_id"] else None
        others = None
        siblings = by_doc.get(f["document_id"], [])
        if det and len(siblings) > 1:
            others = [ErrorReport(error_id=s["id"], document_id=s["document_id"], field=s["field"],
                                  reported_at=s["ts"], expected=s["expected"], observed=s["actual"],
                                  kind="missing" if s["actual"] in (None, "") else "wrong")
                      for s in siblings if s["expected"] not in (None, "")]
        t = trajs.get(f["document_id"]) if f["document_id"] else None
        traced.append(settle(trace_agent(f, t, agent_refs.get(t.get("case_id")), rules) if t is not None
                             else trace_failure(f, det, others)))
    span = (max(r.ts for r in rows) - min(r.ts for r in rows)) or timedelta(minutes=8)
    out = group_failures(traced, passes[:MAX_POPULATION], [r.ts for r in rows], changes=changes,
                         release_notes=_release_notes(engine, source.name), burst_width=span / 8, is_eval=True,
                         decisions=load_decisions(engine, source.name))
    # Cases that came from production failures (assay/learn.py): flag a bug that's back.
    from assay import learn
    guarded = learn.guards(engine, source.name)
    member_of = {f["id"]: f for f in traced}
    back_total = 0
    for g in out["groups"]:
        hits = [member_of[i] for i in g["member_ids"] if member_of[i]["case_id"] in guarded]
        if not hits:
            continue
        back = [h for h in hits if h.get("since") == "new"]
        back_total += len({h["case_id"] for h in back})
        pats = Counter(guarded[h["case_id"]]["name"] for h in hits)
        name, _ = pats.most_common(1)[0]
        first = min(guarded[h["case_id"]]["first_seen"] or "" for h in hits)[:10]
        g["guards"] = {"cases": len({h["case_id"] for h in hits}), "back": len({h["case_id"] for h in back}),
                       "patterns": dict(pats)}
        g["evidence"].insert(0, (f"Production bug back: {len({h['case_id'] for h in back})} regression case(s) that "
                                 f"passed before fail again" if back else
                                 f"{len({h['case_id'] for h in hits})} regression case(s) from production still fail")
                             + f", guarding “{name}” (first seen {first}).")
    # What each failing check's cause means for the release (see flaky.summarize).
    key_of = {f["id"]: flaky.check_key(_Row(f)) for f in traced}
    roles, causes = {}, {}
    for g in out["groups"]:
        causes.update({key_of[i]: g["name"] for i in g["member_ids"]})
        d = (g.get("decision") or {}).get("decision")
        role = "accepted" if d in ("accepted_change", "not_a_problem") else \
            {"intended_change": "intended", "evaluator": "evaluator", "infrastructure": "infrastructure"}.get(g["kind"])
        if role:
            roles.update({key_of[i]: role for i in g["member_ids"]})
    # Judged on data that doesn't match the trace: not evidence about the AI, pass or fail.
    for r in rows:
        if r.result_id in audited:
            roles[flaky.check_key(r)] = "evaluator_input"
    from assay import verdicts
    verdict = verdicts.compute(rows, states, audited, roles, causes, base_rows)
    stability = _with_guards(flaky.summarize(states, tolerance, roles), back_total)
    lost = verdict["counts"]["MISSING"]
    if lost:  # results that never arrived: the run isn't done being judged
        evs = sorted({c["evaluator"] for c in verdict["checks"] if c["verdict"] == "MISSING"})
        stability["reasons"].append(f"{lost:,} results never arrived from {', '.join(evs)}: rerun "
                                    f"{'it' if len(evs) == 1 else 'them'} on the cases they skipped.")
        if stability["outcome"] == "advance":
            stability["outcome"] = "rerun"
    return out | {"audit": audit.summary(rows, audited), "verdicts": verdict} | {"scope": {"kind": "eval", "run_id": run_id, "baseline": baseline, "changes": {
        k: {"from": a, "to": b} for k, (a, b) in changes.items()}, "results": len(rows), "checks": len(cand),
        "passed": len(passes), "attempts_per_check": round(len(rows) / len(cand), 1)},
        "population": len(cand), "stability": stability
        | {"verdicts": verdict["counts"], "run_id": run_id, "baseline": baseline, "lineage": this["lineage"], "production_bugs_back": back_total}}


def _with_guards(st: dict, back: int) -> dict:
    """A production bug that came back holds the release, whatever the pass rate says."""
    if back:
        st["reasons"].insert(0, f"{back} regression case(s) from production failures passed before and fail again: "
                                "a bug that was fixed is back.")
        if st["outcome"] in ("advance", "rerun"):
            st["outcome"] = "hold"
    return st


class _Row:
    """A failure dict seen as a result row, for check_key."""
    def __init__(self, f):
        self.case_id, self.field, self.evaluator = f["case_id"], f.get("field"), f.get("evaluator")


def _baseline(runs: List[dict], run_id: str, baseline: Optional[str]) -> Optional[str]:
    if baseline is not None:
        return baseline
    this = next(r for r in runs if r["run_id"] == run_id)
    earlier = [r for r in runs if r["start"] < this["start"]]
    return earlier[0]["run_id"] if earlier else None


def expectations(analysis: dict, key: str, engine: Engine, tenant: str) -> List[dict]:
    """For an intended change in an eval run: the new expected values, to update the test set with."""
    g = next((g for g in analysis["groups"] if g["key"] == key), None)
    if g is None:
        return []
    ids = set(g["member_ids"])
    t = store.eval_results
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.run_id == analysis["scope"]["run_id"]))
                            ).all()
    return [{"case_id": r.case_id, "field": r.field, "old_expected": r.expected, "new_expected": r.actual}
            for r in rows if r.result_id in ids]
