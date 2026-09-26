"""Transition failure matrices: where in a workflow runs fail, and what came right before.

Rows are the last state that went right, columns the first that failed. A cell counts the runs that
failed there, with how they failed. "generate_sql → execute_sql: 12" says where to look first.

The states are the goal checkpoints when a case's reference has them (the last one met, then the
first one missed), else the steps: the last tool call that worked before the first bad step (credit
assignment, assay/agents.py), and that step (a tool, or the answer). A failure before any tool
worked comes from "start".

From an evaluation run (optionally against a baseline, to see which transition a change made worse),
or from the first failures people marked in the Review tab.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import agents, store
from assay import contracts as contracts_mod

START, ANSWER = "start", "answer"


def _last_ok_before(traj: dict, seq: Optional[int]) -> str:
    ok = [s for s in traj["steps"] if s["kind"] == "tool" and not s.get("error") and (seq is None or s["seq"] < seq)]
    return ok[-1]["name"] if ok else START


def failure_of(traj: dict, ref: Optional[dict], rules: List[dict]) -> Optional[Tuple[str, str, str]]:
    """(last state that went right, first that failed, how) for a failing trajectory; None if it passed."""
    cps = agents.checkpoint_results(traj, ref)
    if cps:
        missed = next((i for i, c in enumerate(cps) if not c["passed"]), None)
        if missed is None:
            return None
        return (cps[missed - 1]["name"] if missed else START, cps[missed]["name"], "missed checkpoint")
    checks = agents.checks_for(traj, ref, rules)
    if not any(c["status"] == "fail" for c in checks):
        return None
    why = agents.credit(traj, ref, rules)
    seq = why.get("seq")
    at = why.get("expected_tool") if why["mechanism"] == "gave_up" else why.get("stage")
    step = next((s for s in traj["steps"] if s["seq"] == seq), None)
    if step is not None and step["kind"] == "answer":
        at = ANSWER
    return _last_ok_before(traj, seq), at or ANSWER, agents.MECHANISMS.get(why["mechanism"], why["mechanism"])


def _order(pairs: List[Tuple[str, str]], positions: Dict[str, List[int]]) -> List[str]:
    names = {x for p in pairs for x in p}
    mid = {n: median(positions[n]) if positions.get(n) else 0 for n in names}
    rank = lambda n: (0, 0) if n == START else (2, 0) if n == ANSWER else (1, mid[n])
    return sorted(names, key=lambda n: (rank(n), n))


def build(failures: List[dict], runs: int, positions: Dict[str, List[int]]) -> dict:
    """failures: [{"from", "to", "how", "case"}] -> the matrix."""
    cells: Dict[Tuple[str, str], dict] = {}
    for f in failures:
        c = cells.setdefault((f["from"], f["to"]), {"from": f["from"], "to": f["to"], "count": 0, "how": Counter(),
                                                    "cases": []})
        c["count"] += 1
        c["how"][f["how"]] += 1
        if len(c["cases"]) < 5:
            c["cases"].append(f["case"])
    states = _order(list(cells), positions)
    out = [{**c, "how": dict(c["how"].most_common())} for c in sorted(cells.values(), key=lambda c: -c["count"])]
    return {"runs": runs, "failed": len(failures), "states": states, "cells": out, "hotspots": out[:3]}


def of_run(engine: Engine, source, tenant: str, run_id: str, task: Optional[str] = None) -> dict:
    heads = [h for h in agents.run_trajectories(engine, tenant, run_id) if h["status"] != "running"
             and (task is None or h["task"] == task)]
    trajs = source.trajectories([h["trajectory_id"] for h in heads]) if hasattr(source, "trajectories") else {}
    refs = agents.references(engine, tenant, {h["case_id"] for h in heads if h["case_id"]})
    rules = contracts_mod.load(engine, source.name)
    failures, positions = [], defaultdict(list)
    for h in heads:
        traj = trajs.get(h["trajectory_id"])
        if traj is None:
            continue
        for i, c in enumerate(agents.tool_calls(traj)):
            positions[c["name"]].append(i)
        ref = refs.get(h["case_id"])
        for i, cp in enumerate((ref or {}).get("checkpoints") or []):
            positions[str(cp.get("name") or cp.get("tool"))[:80]].append(i)
        f = failure_of(traj, ref, rules)
        if f:
            failures.append({"from": f[0], "to": f[1], "how": f[2], "case": h["case_id"] or h["trajectory_id"]})
    return {"run_id": run_id, "task": task, **build(failures, len(heads), positions)}


def compare(now: dict, before: dict) -> dict:
    """The candidate's matrix with each cell's change from the baseline's: which transition got worse."""
    was = {(c["from"], c["to"]): c["count"] for c in before["cells"]}
    cells = [{**c, "before": was.get((c["from"], c["to"]), 0), "change": c["count"] - was.get((c["from"], c["to"]), 0)}
             for c in now["cells"]]
    gone = [{"from": f, "to": t, "count": 0, "how": {}, "cases": [], "before": n, "change": -n}
            for (f, t), n in was.items() if not any(c["from"] == f and c["to"] == t for c in now["cells"])]
    cells += gone
    states = list(dict.fromkeys(now["states"] + [s for s in before["states"] if s not in now["states"]]))
    worse = sorted([c for c in cells if c["change"] > 0], key=lambda c: -c["change"])
    return {**now, "baseline": before["run_id"], "baseline_failed": before["failed"], "cells": cells,
            "states": states, "worse": worse[:3], "better": sorted([c for c in cells if c["change"] < 0],
                                                                   key=lambda c: c["change"])[:3]}


def of_review(engine: Engine, tenant: str, days: float = 30, now: Optional[datetime] = None) -> dict:
    """From the first failures people marked in the Review tab: the step they pointed at, and the last
    tool that worked before it."""
    from assay import review
    from assay.sources.events import EventsSource
    now = now or datetime.utcnow()
    t = store.review_notes
    with engine.connect() as conn:
        notes = [dict(r._mapping) for r in conn.execute(select(t).where(and_(
            t.c.tenant == tenant, t.c.went_wrong, t.c.created_at >= now - timedelta(days=days))))
            if review._by_person(r) and not r.superseded and r.first_step]
    ids = sorted({n["first_step"].get("trace_id") for n in notes if n["first_step"].get("trace_id")})
    trajs = EventsSource(engine, tenant).trajectories(ids)
    failures, positions = [], defaultdict(list)
    for tr in trajs.values():
        for i, c in enumerate(agents.tool_calls(tr)):
            positions[c["name"]].append(i)
    for n in notes:
        fs = n["first_step"]
        tr = trajs.get(fs.get("trace_id"))
        if tr is None:
            continue
        seq = fs.get("seq")
        step = next((s for s in tr["steps"] if s["seq"] == seq), None)
        to = ANSWER if step is None or step["kind"] == "answer" else step.get("name") or step["kind"]
        failures.append({"from": _last_ok_before(tr, seq), "to": to, "how": n.get("hint") or "marked by a person",
                         "case": n["conversation"]})
    return {"from_review": True, "days": days, **build(failures, len({n["conversation"] for n in notes}), positions)}


def text(m: dict) -> str:
    """The matrix for a terminal: rows the last state that went right, columns the first that failed."""
    if not m["cells"] or not any(c["count"] for c in m["cells"]):
        return f"No failures to place ({m['runs']} runs)." if not m.get("baseline") else \
            f"No failures in {m['run_id']}; {m['baseline_failed']} in {m['baseline']}."
    states = m["states"]
    count = {(c["from"], c["to"]): c for c in m["cells"]}
    frm = [s for s in states if any(k[0] == s for k in count)]
    to = [s for s in states if any(k[1] == s for k in count)]
    w = max(len(s) for s in frm + ["last ok \\ failed"])
    cw = [max(5, len(s)) for s in to]
    lines = [f"{m['failed']} of {m['runs']} runs failed" + (f" (baseline {m['baseline']}: {m['baseline_failed']})"
                                                            if m.get("baseline") else "") + ".", "",
             f"{'last ok / failed':<{w}}  " + "  ".join(f"{s:>{cw[i]}}" for i, s in enumerate(to))]
    for r in frm:
        row = []
        for i, s in enumerate(to):
            c = count.get((r, s))
            v = "." if not c or not c["count"] and "change" not in c else str(c["count"])
            if c and m.get("baseline") and c.get("change"):
                v += f"{c['change']:+d}"
            row.append(f"{v:>{cw[i]}}")
        lines.append(f"{r:<{w}}  " + "  ".join(row))
    lines.append("")
    top = m.get("worse") if m.get("baseline") else m["hotspots"]
    if top:
        lines.append("Got worse:" if m.get("baseline") else "Where most fail:")
        for c in top:
            how = ", ".join(f"{k} {n}" for k, n in list(c["how"].items())[:2])
            lines.append(f"  {c['from']} → {c['to']}: {c['count']}" + (f" ({c['change']:+d})" if m.get("baseline") else "")
                         + (f", {how}" if how else "") + (f"; e.g. {', '.join(c['cases'][:3])}" if c["cases"] else ""))
    return "\n".join(lines)


def cli(root, run: Optional[str], baseline: Optional[str], task: Optional[str], fmt: str) -> int:
    """`assay matrix`: the latest test run's matrix (or --run), against --baseline if given."""
    import json
    import sys
    from assay import local
    from assay.sources.events import EventsSource
    engine, state = local._open(root)
    if engine is None:
        print("No test runs yet: run `assay test` first.", file=sys.stderr)
        return 2
    run = run or (state or {}).get("last")
    if not run:
        print("No test run yet: run `assay test` first.", file=sys.stderr)
        return 2
    src = EventsSource(engine, local.TENANT)
    m = of_run(engine, src, local.TENANT, run, task)
    if baseline:
        m = compare(m, of_run(engine, src, local.TENANT, baseline, task))
    print(json.dumps(m, indent=1, default=str) if fmt == "json" else text(m))
    return 0
