"""`assay diff`: what behavior changed between two runs of your AI app, whether it's real, and
how much it matters.

    AI BEHAVIOR DIFF
    ────────────────────────────────

    Baseline: v1.8.2
    Current:  v1.9.0

    47 scenarios

    ✓ 39 unchanged
    ↑ 4 improved
    ✗ 3 regressed
    ⚠ 1 flaky

    REGRESSIONS

    1. refund_flow
       Expected: approval(refund) → refund
       Actual:   refund → approval(refund)
       Safety: refund ran before its approval
       Severity: HIGH

Each case (scenario) is compared with its baseline the way `assay test` does: attempts,
flakiness and what couldn't be judged decide whether a change is real (assay/local.py). On top
of that the diff shows the flow: the tools the agent called, its approvals and the resources it
read, in order, most common across attempts. A case whose flow changed but whose checks still
pass is listed too: nothing failed, but the agent does something else now.

Severity: HIGH for a security check (safety contracts, PII, prompt injection, approvals), an
approval that moved, or a tool the baseline never called; LOW when only cost, latency, steps or
context grew; MEDIUM for the rest. A field of your own whose accuracy dropped is a regression
of its own (invoice_number 98% → 91%).
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

from assay import store

SECURITY = ("safety", "pii", "injection", "expect.must_get_approval", "expect.must_not_call")
LABEL_KEYS = ("version", "release", "app", "build", "commit", "prompt", "model")  # what names a run
RULE = "─" * 32


# ---------- flows ----------

DB_READS = "DB reads"  # a run's reads per table (assay-evals/jest): counted, left out of the flow
READS_GREW = (2, 5)  # reads of a table that at least doubled and grew by 5 or more are a change


def _step_label(s: dict) -> Optional[str]:
    if s["kind"] == "tool":
        return None if s["name"] == DB_READS else s["name"]
    if s["kind"] == "approval":
        decision = (s.get("args") or {}).get("decision")
        return f"approval({s['name']})" if decision in (None, "approved") else f"approval({s['name']}: {decision})"
    if s["kind"] == "resource":
        return f"read {(s.get('args') or {}).get('uri') or s['name']}"
    return None


def flow(traj: dict) -> Tuple[str, ...]:
    return tuple(x for x in (_step_label(s) for s in traj["steps"]) if x)


def database(traj: dict) -> dict:
    """What a run did to the database: {"writes": {"DB INSERT bookings": {"set": [...], "empty": [...]}},
    "reads": {table: n}}. A write made more than once sets every column any of them set, and leaves
    empty only what all of them left empty."""
    writes, reads = {}, {}
    for s in traj["steps"]:
        if s["kind"] != "tool" or not str(s.get("name") or "").startswith("DB "):
            continue
        a = s.get("args") or {}
        if s["name"] == DB_READS:
            reads = {k: v for k, v in a.items() if isinstance(v, int)}
            continue
        w = writes.setdefault(s["name"], {"set": set(), "empty": None})
        w["set"] |= set(a.get("set") or [])
        empty = set(a.get("empty") or [])
        w["empty"] = empty if w["empty"] is None else w["empty"] & empty
    return {"writes": {k: {"set": sorted(w["set"]), "empty": sorted(w["empty"] or [])} for k, w in writes.items()},
            "reads": reads}


def database_change(before: dict, now: dict) -> List[str]:
    """How the same writes changed (a write added or dropped is in the flow), and reads that grew a lot."""
    out = []
    for name in sorted(set(before["writes"]) & set(now["writes"])):
        b, n = before["writes"][name], now["writes"][name]
        parts = []
        dropped = [c for c in b["set"] if c not in n["set"]]
        empty = [c for c in n["empty"] if c not in b["empty"]]
        filled = [c for c in b["empty"] if c not in n["empty"] and c in n["set"]]
        also = [c for c in n["set"] if c not in b["set"] and c not in n["empty"]]
        if dropped:
            parts.append(f"no longer sets {', '.join(dropped)}")
        if empty:
            parts.append(f"now leaves {', '.join(empty)} empty")
        if filled:
            parts.append(f"now fills {', '.join(filled)}")
        if also:
            parts.append(f"now also sets {', '.join(also)}")
        if parts:
            out.append(f"{name}: {'; '.join(parts)}")
    times, more = READS_GREW
    for table in sorted(set(before["reads"]) | set(now["reads"])):
        was, is_ = before["reads"].get(table, 0), now["reads"].get(table, 0)
        if is_ >= times * max(was, 1) and is_ - was >= more:
            out.append(f"reads of {table}: {is_} instead of {was}")
    return out


def flows(engine, tenant: str, run_id: str) -> Dict[str, dict]:
    """Per case: its most common flow across attempts, and how many distinct flows it had."""
    from assay.sources.events import EventsSource
    t = store.eval_results
    with engine.connect() as conn:
        docs = conn.execute(select(t.c.case_id, t.c.attempt, t.c.document_id).where(
            (t.c.tenant == tenant) & (t.c.run_id == run_id) & t.c.document_id.is_not(None))).all()
    by_case = defaultdict(set)
    for r in docs:
        by_case[r.case_id].add(r.document_id)
    trajs = EventsSource(engine, tenant).trajectories(sorted({d for ds in by_case.values() for d in ds}))
    out = {}
    for case, ds in by_case.items():
        seen = Counter(flow(trajs[d]) for d in ds if d in trajs)
        if seen:
            top, n = seen.most_common(1)[0]
            trace = min(d for d in ds if d in trajs and flow(trajs[d]) == top)  # one run that took it
            out[case] = {"flow": top, "distinct": len(seen), "attempts": sum(seen.values()), "top_count": n,
                         "trace": trace, "db": database(trajs[trace])}
    return out


def flow_change(before: Tuple[str, ...], now: Tuple[str, ...]) -> Optional[dict]:
    """How a flow changed: {"added", "removed", "reordered", "approval_moved", "counts"}; None if it didn't.
    counts: steps both flows have, made a different number of times ({"HTTP GET x/charges": [1, 2]}): a
    charge sent twice isn't "another order"."""
    if before == now:
        return None
    added = sorted(set(now) - set(before))
    removed = sorted(set(before) - set(now))
    # The order steps first appear in, so a repeat doesn't read as a reorder.
    common_before = [x for x in dict.fromkeys(before) if x in now]
    common_now = [x for x in dict.fromkeys(now) if x in before]
    reordered = common_before != common_now
    approval_moved = reordered and any(x.startswith("approval(") and
                                       common_before.index(x) != common_now.index(x) for x in common_before)
    nb, nn = Counter(before), Counter(now)
    counts = {x: [nb[x], nn[x]] for x in sorted(set(before) & set(now)) if nb[x] != nn[x]}
    return {"added": added, "removed": removed, "reordered": reordered, "approval_moved": approval_moved,
            "counts": counts}


def change(before: dict, now: dict) -> Optional[dict]:
    """How a case's behavior changed: its flow, and what it wrote to the database ("db")."""
    ch = flow_change(before["flow"], now["flow"])
    db = database_change(before.get("db") or {"writes": {}, "reads": {}}, now.get("db") or {"writes": {}, "reads": {}})
    if not db:
        return ch
    ch = ch or {"added": [], "removed": [], "reordered": False, "approval_moved": False, "counts": {}}
    return {**ch, "db": db}


def _describe(ch: dict) -> str:
    parts = []
    if ch["approval_moved"]:
        parts.append("an approval moved")
    elif ch["reordered"]:
        parts.append("same steps, another order")
    if ch["added"]:
        parts.append(f"new: {', '.join(ch['added'])}")
    if ch["removed"]:
        parts.append(f"no longer: {', '.join(ch['removed'])}")
    for step, (was, is_) in (ch.get("counts") or {}).items():
        parts.append(f"{step}: {_times(is_)} instead of {_times(was)}")
    parts += ch.get("db") or []
    return "; ".join(parts) or "changed"


def _times(n: int) -> str:
    return "once" if n == 1 else "twice" if n == 2 else f"{n} times"


# ---------- runs ----------

def label(run: Optional[dict], run_id: str) -> str:
    from assay import local
    if run_id == local.BASELINE:
        return "each case's last passing run"
    lin = (run or {}).get("lineage") or {}
    name = next((f"{lin[k]}" for k in LABEL_KEYS if lin.get(k)), None)
    return f"{name} (run {run_id})" if name else run_id


def resolve(runs: List[dict], ref: str) -> Optional[str]:
    """A run id, "baseline", or a version: the newest run whose recorded version has that value."""
    from assay import local
    if ref == "baseline":
        return local.BASELINE
    ids = {r["run_id"] for r in runs}
    if ref in ids:
        return ref
    return next((r["run_id"] for r in runs if r["run_id"] != local.BASELINE
                 and ref in {str(v) for v in (r.get("lineage") or {}).values()}), None)


# ---------- the diff ----------

def _severity(fields: List[str], ch: Optional[dict], behavior_only: bool) -> str:
    if any(f.startswith(SECURITY) for f in fields) or (ch and (ch["approval_moved"] or ch["added"])):
        return "HIGH"
    if behavior_only:
        return "LOW"
    return "MEDIUM"


def compute(engine, tenant: str, current: str, baseline: str, cfg: dict, source=None) -> dict:
    """The structured diff of `current` against `baseline` (see the module docstring). Reads the
    results; changes nothing. cfg: "tolerance" and "behavior" ({"fail", "ratios"})."""
    from assay import local
    from assay.failures import eval_runs
    result = local.compare(engine, current, baseline, cfg["tolerance"], cfg["behavior"], tenant, source,
                           acks=cfg.get("acks"))
    if result is None:
        return {"error": f"Run {current} recorded nothing to compare."}
    result["behavior_fails"] = cfg["behavior"]["fail"]
    _, c = local.verdict(result, True)
    s = local.summarize(result, c, baseline)
    runs = {r["run_id"]: r for r in eval_runs(engine, tenant)}
    before, now = flows(engine, tenant, baseline), flows(engine, tenant, current)
    b = s["buckets"]
    failing_fields = defaultdict(list)
    for (case, field), f in result["failing"].items():
        failing_fields[case].append((field, local._reason(f)))
    worse = {x["case_id"]: x["changes"] for x in result.get("behavior") or []}

    def entry(case: str) -> dict:
        ch = change(before[case], now[case]) if case in before and case in now else None
        checks = [(f, r) for f, r in failing_fields.get(case, [])]
        fields = [f for f, _ in checks]
        reasons = [local._tidy(f"{local._label(f)}: {r.splitlines()[0][:200]}") for f, r in checks]
        reasons += [x["text"] for x in worse.get(case, [])]
        reasons += [local.score_text(x) for x in result.get("score_regressions") or [] if x["case_id"] == case]
        reasons = sorted(dict.fromkeys(reasons), key=local._rank)
        reasons = [f"{local._label(k[1])}: {local.routed(r)}" for k, r in (result.get("routing") or {}).items()
                   if k[0] == case and local.routed(r)] + reasons  # the model it was routed to, first
        return {"case": case, "name": local._short(case),
                "expected": list(before[case]["flow"]) if case in before else None,
                "actual": list(now[case]["flow"]) if case in now else None,
                "flow_change": ch and {**ch, "text": _describe(ch)},
                "varies": now[case]["distinct"] > 1 if case in now else False,
                "trace_before": before[case]["trace"] if case in before else None,
                "trace_now": now[case]["trace"] if case in now else None,
                "reasons": reasons, "checks": fields,
                "setup": [local.change_text(x) for x in ((result.get("setup") or {}).get("cases") or {}).get(case, [])],
                "severity": _severity(fields, ch, behavior_only=not fields and case in worse)}

    regressions = [entry(case) for case in b["regressed"]]
    rejudged = {k[1] for k in result.get("judge_changed") or {}}
    for f in result["fields"]:  # your own fields whose accuracy dropped, e.g. extraction
        if f["field"] in local.CHECK_NAMES or f["field"].startswith("expect.") or not f["base_total"] \
                or f["field"] in rejudged:  # a new judge's accuracy isn't comparable with the old one's
            continue
        was, is_ = f["base_passed"] / f["base_total"], f["passed"] / f["total"]
        if is_ < was:
            regressions.append({"field": f["field"], "name": f"{f['label']} accuracy",
                                "change": f"{was:.0%} → {is_:.0%}", "severity": "MEDIUM" if was - is_ >= 0.05 else "LOW"})
    rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
    regressions.sort(key=lambda e: (rank[e["severity"]], "field" in e, e["name"]))
    improved = set(s["improved"])
    changed = [entry(case) for case in b["passed"] if case not in improved and case in before and case in now
               and change(before[case], now[case])]
    for e in changed:
        e["severity"] = None
    flaky = []
    for case in b["flaky"]:  # the checks that varied, not the ones that always passed
        varied = [(f, x) for (k, f), x in result["attempts"].items() if k == case and not all(x)]
        detail = "; ".join(f"{local._label(f)} passed {sum(x)} of {len(x)} attempts"
                           + (f" ({local.routed(result['routing'][(case, f)])})"
                              if local.routed((result.get("routing") or {}).get((case, f)) or {"now": {}}) else "")
                           for f, x in varied)
        flaky.append({"case": case, "name": local._short(case), "detail": detail or "varied across attempts"})
    unchanged = len(b["passed"]) - len(improved & set(b["passed"])) - len(changed)
    return {"baseline": {"run_id": baseline, "label": label(runs.get(baseline), baseline)},
            "current": {"run_id": current, "label": label(runs.get(current), current)},
            "scenarios": s["cases"],
            "counts": {"unchanged": unchanged, "improved": len(improved), "regressed": len(b["regressed"]),
                       "flaky": len(b["flaky"]), "changed": len(changed), "new_failures": len(b["new failure"]),
                       "not_judged": len(b["couldn't be judged"]), "needs_reruns": len(b["needs reruns"]),
                       "known_failures": len(b["known failure"]),
                       "acknowledged": len(b.get("acknowledged") or []), "judge_changed": len(b.get("judge changed") or []),
                       "judge_unstable": len(b.get("judge unstable") or [])},
            "regressions": regressions, "new_failures": [entry(c) for c in b["new failure"]],
            "changed": changed, "flaky": flaky, "improved": [local._short(c) for c in sorted(improved)],
            "not_judged": [{"name": local._short(x["case_id"]), "field": x["field"], "reason": x["reason"]}
                           for x in result["not_judged"]],
            "judge_changed": [{"name": local._short(p["case_id"]), "field": p["field"], "before": p["before"],
                               "now": p["now"]} for p in c.get("judge_changed") or []],
            "judge_unstable": [{"name": local._short(p["case_id"]), "field": p["field"], "worse": p["worse"],
                                "detail": f"{local._label(p['field'])} passed {p['base_rate']:.0%} → {p['rate']:.0%}; "
                                          f"same answer, verdict flipped on {p['judge_split']} of {p['rejudged']} "
                                          f"judged again" + (" (could be worse)" if p["worse"] else "")}
                               for p in c.get("judge_unstable") or []],
            "models": result.get("models") or {}, "surface": result.get("surface") or [],
            "kinds": result.get("kinds") or {}, "fixed_context": result.get("fixed_context"),
            "everywhere": [local.change_text(x) for x in (result.get("setup") or {}).get("everywhere") or []],
            "blame": local.blame(result.get("setup") or {}, list(b["regressed"])),
            "totals": [{**x, "most": [{**m, "name": local._short(m["case_id"])} for m in x["most"]]}
                       for x in result.get("behavior_suite") or []] if cfg["behavior"]["fail"] else [],
            "totals_info": [] if cfg["behavior"]["fail"] else result.get("behavior_suite") or []}


# ---------- showing it ----------

def _entry_lines(i: int, e: dict, paint) -> List[str]:
    out = [f"{i}. {e['name']}"]
    if "field" in e:
        return out + [f"   {e['change']}", f"   Severity: {e['severity']}"]
    if e.get("flow_change"):
        if e["expected"] != e["actual"]:  # the same steps, with other columns: only the text says how
            out += [f"   Expected: {' → '.join(e['expected']) or '(no calls)'}",
                    f"   Actual:   {' → '.join(e['actual']) or '(no calls)'}" + (" (varies across attempts)" if e["varies"] else "")]
        out.append(paint(f"   {e['flow_change']['text'][:1].upper()}{e['flow_change']['text'][1:]}", "dim"))
    for r in e["reasons"][:2]:
        out.append(f"   {r}")
    if len(e["reasons"]) > 2:
        out.append(paint(f"   (+{len(e['reasons']) - 2} more)", "dim"))
    if e.get("setup"):
        out.append(paint("   Changed around it:", "dim"))
        out += [paint(f"     {x}", "dim") for x in e["setup"]]
    if e.get("severity"):
        color = {"HIGH": "red", "MEDIUM": "yellow", "LOW": "dim"}[e["severity"]]
        out.append(f"   Severity: {paint(e['severity'], color)}")
    return out


def local_kinds(k: dict) -> str:
    from assay.local import kinds_text
    return kinds_text(k)


def text(d: dict) -> str:
    from assay.local import _paint as paint
    k = d["counts"]
    out = [paint("AI BEHAVIOR DIFF", "bold"), RULE, "", f"Baseline: {d['baseline']['label']}",
           f"Current:  {d['current']['label']}", "", f"{d['scenarios']} scenario{'s' * (d['scenarios'] != 1)}", ""]
    for n, mark, word, color in ((k["unchanged"], "✓", "unchanged", "green"), (k["improved"], "↑", "improved", "green"),
                                 (k["changed"], "~", "changed, still passing", "yellow"),
                                 (k["regressed"], "✗", "regressed", "red"), (k["new_failures"], "✗", "new failing", "red"),
                                 (k["flaky"], "⚠", "flaky", "yellow"), (k["not_judged"], "?", "couldn't be judged", "yellow"),
                                 (k.get("needs_reruns", 0), "?", "could be worse, or chance: needs reruns", "yellow"),
                                 (k.get("judge_unstable", 0), "?", "judge unstable: same answer, another verdict", "yellow"),
                                 (k["known_failures"], "·", "failing before too", "dim"),
                                 (k.get("acknowledged", 0), "·", "acknowledged, quiet until worse", "dim"),
                                 (k.get("judge_changed", 0), "?", "judged by a new judge, not compared", "yellow")):
        if n or word in ("unchanged", "regressed"):
            out.append(f"{paint(mark, color)} {n} {word}")
    if d.get("fixed_context"):
        from assay.local import fixed_context_text
        out += ["", paint("FIXED CONTEXT PER CALL", "bold"), "", f"  {fixed_context_text(d['fixed_context'])}"]
    if d.get("everywhere"):
        out += ["", paint("CHANGED IN EVERY CASE", "bold"), ""] + [f"  {x}" for x in d["everywhere"]]
    for title, items in (("REGRESSIONS", d["regressions"]), ("NEW FAILING", d["new_failures"]),
                         ("CHANGED, STILL PASSING", d["changed"])):
        if items:
            out += ["", paint(title, "bold"), ""]
            if title == "REGRESSIONS" and d.get("blame"):
                out += [paint(x, "yellow") for x in d["blame"]] + [""]
            for i, e in enumerate(items, 1):
                out += _entry_lines(i, e, paint) + [""]
            out.pop()
    totals = d.get("totals") or d.get("totals_info") or []
    if totals:
        out += ["", paint("WHOLE-RUN TOTALS" + ("" if d.get("totals") else " (not failing: [behavior] fail = false)"),
                          "bold"), ""]
        for x in totals:
            out.append(f"- {x['text']}")
            if x.get("most"):
                out.append(paint("  grew most: " + ", ".join(m.get("name") or m["case_id"] for m in x["most"]), "dim"))
    if d.get("judge_changed"):
        pairs = sorted({(x["before"], x["now"]) for x in d["judge_changed"]})
        out += ["", paint("JUDGE CHANGED", "bold"), "",
                f"{', '.join(f'{a} → {b}' for a, b in pairs)}: {len(d['judge_changed'])} failing check(s) not compared, "
                f"since a drop could be the judge, not the AI. `assay calibrate` checks the new judge."]
    if d.get("kinds"):
        out += ["", paint("FAILURES BY KIND", "bold"), "", local_kinds(d["kinds"])]
    if d.get("surface"):
        out += ["", paint("SCORES THAT ROSE WITH THE SURFACE", "bold"), ""] + [f"- {x['text']}" for x in d["surface"]]
    if d.get("models"):
        out += ["", paint("BY MODEL", "bold"), ""] + [f"- {m}: {ok}/{n} cases passing" for m, (ok, n) in d["models"].items()]
    if d.get("judge_unstable"):
        out += ["", paint("JUDGE UNSTABLE", "bold"), ""] + [f"- {x['name']}: {x['detail']}" for x in d["judge_unstable"]]
        out += ["", "More attempts of the AI won't settle these: tighten the rubric, check it deterministically, "
                    "or `assay calibrate` the judge."]
    if d["flaky"]:
        out += ["", paint("FLAKY", "bold"), ""] + [f"- {x['name']}: {x['detail']}, the way it did before"
                                                   for x in d["flaky"]]
    if d["not_judged"]:
        out += ["", paint("COULDN'T BE JUDGED", "bold"), ""] + [f"- {x['name']} {x['field'] or ''}: {x['reason']}"
                                                                 for x in d["not_judged"][:20]]
    if d["improved"]:
        out += ["", paint("IMPROVED", "bold"), "", ", ".join(d["improved"])]
    return "\n".join(out) + "\n"


def markdown(d: dict) -> str:
    from assay.local import _code, _md
    k = d["counts"]
    out = ["## AI behavior diff", "", f"**Baseline:** {_md(d['baseline']['label'])} · **Current:** "
           f"{_md(d['current']['label'])} · {d['scenarios']} scenarios", ""]
    out.append(" · ".join(f"{n} {w}" for n, w in ((k["unchanged"], "unchanged"), (k["improved"], "improved"),
                                                   (k["changed"], "changed, still passing"),
                                                   (k["regressed"], "regressed"), (k["new_failures"], "new failing"),
                                                   (k["flaky"], "flaky"), (k["not_judged"], "couldn't be judged"),
                                                   (k.get("needs_reruns", 0), "need reruns"),
                                                   (k.get("judge_unstable", 0), "judge unstable"))
                          if n or w in ("unchanged", "regressed")))
    for title, items in (("Regressions", d["regressions"]), ("New failing", d["new_failures"]),
                         ("Changed, still passing", d["changed"])):
        if not items:
            continue
        out += ["", f"### {title}", ""]
        for i, e in enumerate(items, 1):
            sev = f" · **{e['severity']}**" if e.get("severity") else ""
            if "field" in e:
                out.append(f"{i}. {_code(e['name'])} {_md(e['change'])}{sev}")
                continue
            out.append(f"{i}. {_code(e['name'])}{sev}")
            if e.get("flow_change"):
                if e["expected"] != e["actual"]:
                    out += [f"   - Expected: {_code(' → '.join(e['expected']) or '(no calls)', 300)}",
                            f"   - Actual: {_code(' → '.join(e['actual']) or '(no calls)', 300)}"]
                if e["flow_change"].get("db"):  # the flow can't show a column a write stopped setting
                    out += [f"   - {_md(x)}" for x in e["flow_change"]["db"]]
            out += [f"   - {_md(r)}" for r in e["reasons"][:2]]
            out += [f"   - Changed: {_md(x)}" for x in e.get("setup") or []]
    if d.get("judge_unstable"):
        out += ["", "### Judge unstable", ""] + [f"- {_code(x['name'])}: {_md(x['detail'])}" for x in d["judge_unstable"]]
        out += ["", "More attempts of the AI won't settle these: make the judge steadier."]
    if d.get("totals"):
        out += ["", "### Whole-run totals", ""]
        out += [f"- {_md(x['text'])}" + (f" (grew most: {', '.join(_code(m['name']) for m in x['most'])})"
                                          if x.get("most") else "") for x in d["totals"]]
    return "\n".join(out) + "\n"


def as_json(d: dict) -> str:
    return json.dumps(d, indent=1, default=str)


def regressed(d: dict) -> bool:
    return bool(d.get("regressions") or d.get("new_failures") or d.get("totals"))


# ---------- the command ----------

def main(root, baseline_ref: Optional[str], current_ref: Optional[str], fmt: str = "text") -> int:
    """`assay diff [BASELINE] [CURRENT]`: 0 nothing regressed, 1 something did, 2 nothing to compare."""
    import sys
    from assay import local
    from assay.failures import eval_runs
    home = local.ensure_home(root)
    if not (home / "assay.db").exists():
        print("Nothing to compare yet: run `pytest --assay` or `assay test` first.", file=sys.stderr)
        return 2
    engine = store.make_engine(f"sqlite:///{home / 'assay.db'}")
    state = local._state(home)
    runs = [r for r in eval_runs(engine, local.TENANT) if r["run_id"] != local.BASELINE]
    if not runs:
        print("Nothing to compare yet: run `pytest --assay` or `assay test` first.", file=sys.stderr)
        return 2
    current = resolve(runs, current_ref) if current_ref else (state.get("last") or runs[0]["run_id"])
    if baseline_ref:
        baseline = resolve(runs, baseline_ref)
    elif (state.get("compared") or {}).get("run") == current and state.get("last") == current:
        # The run passed and became the baseline itself: compare it with the run its cases were compared with.
        cases = Counter(state["compared"]["cases"].values())
        known = {r["run_id"] for r in runs}
        baseline = next((run for run, _ in cases.most_common() if run in known), None) or local.BASELINE
        if len(cases) > 1:
            print(f"Compared with {baseline}, the baseline of {cases[baseline]} of {sum(cases.values())} cases.",
                  file=sys.stderr)
    elif state.get("baseline_cases"):
        baseline = local.BASELINE  # each case's last passing run, as `assay test` compares
    else:
        baseline = next((r["run_id"] for r in runs if r["run_id"] != current), None)
    for ref, got in ((baseline_ref, baseline), (current_ref, current)):
        if ref and not got:
            known = ", ".join(label(r, r["run_id"]) for r in runs[:5])
            print(f"No run is {ref!r}: give a run id or a version recorded with the runs (version=...). "
                  f"Recent runs: {known}.", file=sys.stderr)
            return 2
    if not baseline:
        print("Only one run so far: there's nothing to compare it with.", file=sys.stderr)
        return 2
    try:
        cfg = local.find_config(root)
    except local.SetupError as exc:
        print(exc, file=sys.stderr)
        return 2
    d = compute(engine, local.TENANT, current, baseline, cfg)
    if "error" in d:
        print(d["error"], file=sys.stderr)
        return 2
    print({"text": text, "markdown": markdown, "json": as_json}[fmt](d), end="")
    return 1 if regressed(d) else 0
