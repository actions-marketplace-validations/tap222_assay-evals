"""Where runs broke: in a workflow, the last step that worked and the first that went wrong.

Each pair is one line, in plain words ("generate_sql worked, then execute_sql went wrong"), with how
many runs broke there, how, what went wrong in one of them, and every run, to open. A matrix of the
same pairs (rows the last state that went right, columns the first that failed) is what the
counts are.

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


CHECKPOINT = "missed checkpoint"


def _failure(traj: dict, ref: Optional[dict], rules: List[dict]) -> Optional[dict]:
    """{"from", "to", "how", "detail"} for a failing trajectory; None if it passed."""
    cps = agents.checkpoint_results(traj, ref)
    if cps:
        missed = next((i for i, c in enumerate(cps) if not c["passed"]), None)
        if missed is None:
            return None
        return {"from": cps[missed - 1]["name"] if missed else START, "to": cps[missed]["name"], "how": CHECKPOINT,
                "detail": cps[missed]["detail"]}
    checks = agents.checks_for(traj, ref, rules)
    if not any(c["status"] == "fail" for c in checks):
        return None
    why = agents.credit(traj, ref, rules)
    seq = why.get("seq")
    at = why.get("expected_tool") if why["mechanism"] == "gave_up" else why.get("stage")
    step = next((s for s in traj["steps"] if s["seq"] == seq), None)
    if step is not None and step["kind"] == "answer":
        at = ANSWER
    return {"from": _last_ok_before(traj, seq), "to": at or ANSWER,
            "how": agents.MECHANISMS.get(why["mechanism"], why["mechanism"]), "detail": why.get("detail")}


def failure_of(traj: dict, ref: Optional[dict], rules: List[dict]) -> Optional[Tuple[str, str, str]]:
    """(last state that went right, first that failed, how) for a failing trajectory; None if it passed."""
    f = _failure(traj, ref, rules)
    return (f["from"], f["to"], f["how"]) if f else None


def sentence(frm: str, to: str, checkpoint: bool) -> str:
    """One pair in plain words: what worked last, then what went wrong first."""
    if checkpoint:
        return f"Missed the first checkpoint, “{to}”" if frm == START else f"Reached “{frm}”, then missed “{to}”"
    what = "the answer" if to == ANSWER else to
    return f"Went wrong at {what}, before any tool call worked" if frm == START else f"{frm} worked, then {what} went wrong"


def _order(pairs: List[Tuple[str, str]], positions: Dict[str, List[int]]) -> List[str]:
    names = {x for p in pairs for x in p}
    mid = {n: median(positions[n]) if positions.get(n) else 0 for n in names}
    rank = lambda n: (0, 0) if n == START else (2, 0) if n == ANSWER else (1, mid[n])
    return sorted(names, key=lambda n: (rank(n), n))


def build(failures: List[dict], runs: int, positions: Dict[str, List[int]]) -> dict:
    """failures: [{"from", "to", "how", "case", "trajectory_id"?, "detail"?}] -> the matrix. Each cell
    has its plain-words sentence and every run that broke there (to open), with what went wrong."""
    cells: Dict[Tuple[str, str], dict] = {}
    for f in failures:
        c = cells.setdefault((f["from"], f["to"]), {"from": f["from"], "to": f["to"], "count": 0, "how": Counter(),
                                                    "cases": [], "runs": [], "checkpoint": f["how"] == CHECKPOINT})
        c["count"] += 1
        c["how"][f["how"]] += 1
        if len(c["cases"]) < 5:
            c["cases"].append(f["case"])
        c["runs"].append({"trajectory_id": f.get("trajectory_id"), "case": f["case"], "detail": f.get("detail")})
    for c in cells.values():
        c["sentence"] = sentence(c["from"], c["to"], c["checkpoint"])
    states = _order(list(cells), positions)
    out = [{**c, "how": dict(c["how"].most_common())} for c in sorted(cells.values(), key=lambda c: -c["count"])]
    return {"runs": runs, "failed": len(failures), "states": states, "cells": out, "hotspots": out[:3]}


def of_run(engine: Engine, source, tenant: str, run_id: str, task: Optional[str] = None) -> dict:
    heads = [h for h in agents.run_trajectories(engine, tenant, run_id) if h["status"] != "running"
             and (task is None or h["task"] == task)]
    plain = agents.plain_runs(engine, tenant, [h["trajectory_id"] for h in heads])
    heads = [h for h in heads if h["trajectory_id"] not in plain]  # plain tests (--assay-all) aren't agents
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
        f = _failure(traj, ref, rules)
        if f:
            failures.append({**f, "case": h["case_id"] or h["trajectory_id"], "trajectory_id": h["trajectory_id"]})
    return {"run_id": run_id, "task": task, "plain_left_out": len(plain), **build(failures, len(heads), positions)}


def compare(now: dict, before: dict) -> dict:
    """The candidate's matrix with each cell's change from the baseline's: which transition got worse."""
    was = {(c["from"], c["to"]): c["count"] for c in before["cells"]}
    cells = [{**c, "before": was.get((c["from"], c["to"]), 0), "change": c["count"] - was.get((c["from"], c["to"]), 0)}
             for c in now["cells"]]
    gone = [{**c, "count": 0, "how": {}, "cases": [], "runs": [], "before": c["count"], "change": -c["count"]}
            for c in before["cells"] if not any(x["from"] == c["from"] and x["to"] == c["to"] for x in now["cells"])]
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
                         "case": n["conversation"], "trajectory_id": fs.get("trace_id"), "detail": n.get("note")})
    return {"from_review": True, "days": days, **build(failures, len({n["conversation"] for n in notes}), positions)}


def text(m: dict) -> str:
    """For a terminal: one line per place runs broke, most first (most worse first, against a baseline)."""
    left = f" Agent runs only: {m['plain_left_out']} plain tests left out." if m.get("plain_left_out") else ""
    if not m["cells"] or not any(c["count"] for c in m["cells"]):
        return (f"No failures to place ({m['runs']} runs)." if not m.get("baseline") else
                f"No failures in {m['run_id']}; {m['baseline_failed']} in {m['baseline']}.") + left
    base = m.get("baseline")
    lines = [f"{m['failed']} of {m['runs']} runs failed" + (f" ({base}: {m['baseline_failed']})" if base else "")
             + "." + left, "", "Where runs broke (the last step that worked, then the first that went wrong):"]
    now = [c for c in m["cells"] if c["count"]]
    now.sort(key=lambda c: (-c.get("change", 0), -c["count"]) if base else -c["count"])
    for c in now:
        n = f"{c['count']}" + (f" ({c['change']:+d})" if base and c.get("change") else "")
        how = ", ".join(f"{k} {v}" for k, v in list(c["how"].items())[:2])
        lines.append(f"  {n:>8}  {c['sentence']}: {how}")
        ex = next((r for r in c.get("runs", []) if r.get("detail")), None)
        if ex:
            lines.append(f"            e.g. {ex['case']}: {ex['detail']}")
        elif c["cases"]:
            lines.append(f"            e.g. {', '.join(c['cases'][:3])}")
    gone = [c for c in m["cells"] if not c["count"] and c.get("before")]
    if gone:
        lines += ["", f"No longer breaking since {base}:"] + [f"  {c['sentence']} (was {c['before']})" for c in gone]
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
