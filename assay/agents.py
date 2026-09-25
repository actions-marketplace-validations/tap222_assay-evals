"""Agents: evaluating the trajectory, not just the final answer.

A trajectory is one run of an agent on a task: reasoning, tool calls with
their arguments and results, changes to the world, and a final answer. Each
is checked five ways against what the test case expects (its reference):

  answer      the final answer has the expected value
  end_state   the world afterwards is right: an order exists with qty 2, no
              refund was issued. Folded from the state-change steps.
  tool_calls  the expected tools were called with the right arguments, in
              order where order matters. Harmless extras (read-only lookups
              listed in allow_extra) are fine, and so are retries of a call
              that errored.
  safety      no critical path contract broke (e.g. delete_order without
              confirmed=true). See assay/contracts.py; contracts see tool
              arguments.
  efficiency  within the reference's step budget, and no call repeated three
              times with identical arguments.

Each check is written as an ordinary evaluation result (evaluator
assay.trajectory@1), so failure causes, flakiness across attempts and the
release call work on agents unchanged.

For a failed check, credit assignment finds the first bad step and how it
went wrong:

  unsafe_action           a critical contract broke at this step
  tool_error              a tool errored and nothing later recovered it
  looped                  the same call with the same arguments, over and over
  wrong_tool              a different tool where the reference expected another
  bad_arguments           the expected tool, with the wrong arguments
  gave_up                 stopped before making an expected call
  ignored_result          a tool returned the answer and the final answer doesn't have it
  wrong_state             the calls look right but the world ended up wrong
  wrong_answer            everything before was right; the answer wasn't
"""
from __future__ import annotations

import fnmatch
import json
import re
from collections import Counter, defaultdict
from datetime import datetime
from statistics import mean, median
from typing import Dict, List, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import contracts as contracts_mod
from assay import store
from assay.rootcause import _text_forms, normalize

EVALUATOR = "assay.trajectory@1"
CHECKS = ("answer", "end_state", "tool_calls", "safety", "efficiency")
LOOP = 3  # identical calls that count as a loop
MECHANISMS = {
    "unsafe_action": "Unsafe action", "tool_error": "Tool error, not recovered", "looped": "Looped",
    "wrong_tool": "Wrong tool", "bad_arguments": "Wrong arguments", "gave_up": "Stopped early",
    "ignored_result": "Ignored a tool result", "wrong_state": "Wrong end state", "wrong_answer": "Wrong answer",
}


# ---------- pieces of a trajectory ----------

def tool_calls(traj: dict) -> List[dict]:
    return [s for s in traj["steps"] if s["kind"] == "tool"]


def call_text(name: str, args: Optional[dict]) -> str:
    inner = ", ".join(f"{k}={v!r}" for k, v in (args or {}).items())
    return f"{name}({inner})"


def _same(expected, actual) -> bool:
    if expected == "*":
        return actual is not None
    return normalize(expected) == normalize(actual)


def arg_diffs(expected: Optional[dict], actual: Optional[dict]) -> List[str]:
    """Expected arguments the actual call got wrong (expected args match partially)."""
    actual = actual or {}
    return [f"{k}: expected {v!r}, got {actual.get(k)!r}" for k, v in (expected or {}).items()
            if not _same(v, actual.get(k))]


def compare(traj: dict, ref: Optional[dict]) -> Optional[dict]:
    """Actual tool calls against the reference: what matched, what was extra, where it diverged."""
    if not ref or not ref.get("calls"):
        return None
    expected = [dict(e, index=i, matched=None) for i, e in enumerate(ref["calls"])]
    allow = set(ref.get("allow_extra") or [])
    calls = tool_calls(traj)
    rows, divergence = [], None
    for n, c in enumerate(calls):
        row = {"seq": c["seq"], "tool": c["name"], "args": c["args"], "error": c["error"], "status": None}
        if c["error"]:
            retried = any(later["name"] == c["name"] and not later["error"] for later in calls[n + 1:])
            row["status"] = "error_retried" if retried else "error"
            rows.append(row)
            continue
        open_ = [e for e in expected if e["matched"] is None]
        # The next call the reference expects, in order (optional and any-order ones can come any time).
        nxt = next((e for e in open_ if not e.get("optional") and not e.get("any_order")), None)
        cand = [e for e in open_ if e["tool"] == c["name"] and not arg_diffs(e.get("args"), c["args"])
                and (e is nxt or e.get("optional") or e.get("any_order"))]
        if cand:
            cand[0]["matched"] = c["seq"]
            row["status"], row["expected_index"] = "matched", cand[0]["index"]
        elif c["name"] in allow:
            row["status"] = "allowed_extra"
        else:
            same_tool = next((e for e in open_ if e["tool"] == c["name"]), None)
            if same_tool is not None:
                row["status"], row["expected_index"] = "bad_arguments", same_tool["index"]
                row["diffs"] = arg_diffs(same_tool.get("args"), c["args"])
            elif nxt is not None:
                row["status"], row["expected_tool"] = "wrong_tool", nxt["tool"]
            else:
                row["status"] = "extra"
            if divergence is None:
                divergence = {"seq": c["seq"], "kind": row["status"], "tool": c["name"],
                              "expected_tool": row.get("expected_tool") or (same_tool or {}).get("tool"),
                              "detail": (f"called {call_text(c['name'], c['args'])} where "
                                         f"{call_text(nxt['tool'], nxt.get('args'))} was expected")
                              if row["status"] == "wrong_tool" else
                              (f"called {c['name']} with the wrong arguments: {'; '.join(row['diffs'])}")
                              if row["status"] == "bad_arguments" else
                              f"called {call_text(c['name'], c['args'])}, which the reference doesn't expect"}
        rows.append(row)
    required = [e for e in expected if not e.get("optional")]
    missing = [e for e in required if e["matched"] is None]
    if divergence is None and missing:
        last = traj["steps"][-1]["seq"] if traj["steps"] else 0
        divergence = {"seq": last, "kind": "gave_up", "tool": None, "expected_tool": missing[0]["tool"],
                      "detail": f"finished without calling {call_text(missing[0]['tool'], missing[0].get('args'))}"}
    ordered = [e["matched"] for e in expected if e["matched"] is not None and not e.get("any_order")]
    judged = [r for r in rows if r["status"] in ("matched", "extra", "wrong_tool", "bad_arguments")]
    return {"calls": rows, "expected": [{k: e.get(k) for k in ("index", "tool", "args", "optional", "any_order",
                                                               "matched")} for e in expected],
            "precision": sum(1 for r in judged if r["status"] == "matched") / len(judged) if judged else None,
            "recall": (len(required) - len(missing)) / len(required) if required else 1.0,
            "in_order": ordered == sorted(ordered), "divergence": divergence,
            "passed": not missing and not any(r["status"] in ("extra", "wrong_tool", "bad_arguments") for r in rows)
            and ordered == sorted(ordered)}


def end_state(traj: dict) -> Dict[str, dict]:
    """Every object the trajectory changed, as it was at the end: {name: {"state", "seq", "deleted"}}."""
    out = {}
    for s in traj["steps"]:
        if s["kind"] != "state" or not s["name"]:
            continue
        op = (s["args"] or {}).get("op", "update")
        out[s["name"]] = {"state": None if op == "delete" else s["result"], "seq": s["seq"], "deleted": op == "delete"}
    return out


def check_state(traj: dict, ref: Optional[dict]) -> Optional[List[dict]]:
    """The reference's end-state assertions, each with whether it held and the step that decided it."""
    if not ref or not ref.get("state"):
        return None
    world = end_state(traj)
    out = []
    for a in ref["state"]:
        names = [n for n in world if fnmatch.fnmatch(n, a["object"])]
        live = [n for n in names if not world[n]["deleted"]]
        if "exists" in a:
            ok = bool(live) == bool(a["exists"])
            got = f"{', '.join(live) or 'none'}" + (f" (deleted: {', '.join(n for n in names if n not in live)})"
                                                     if len(names) > len(live) else "")
            text = f"{a['object']} {'exists' if a['exists'] else 'does not exist'}"
            seq = max((world[n]["seq"] for n in names), default=None)
        else:
            vals = [(n, (world[n]["state"] or {}).get(a["field"])) for n in live]
            ok = bool(vals) and all(_same(a["equals"], v) for _, v in vals)
            got = ", ".join(f"{n}.{a['field']}={v!r}" for n, v in vals) or "no such object"
            text = f"{a['object']}.{a['field']} = {a['equals']!r}"
            seq = max((world[n]["seq"] for n in names), default=None)
        out.append({"assertion": text, "passed": ok, "got": got, "seq": seq})
    return out


def check_answer(traj: dict, ref: Optional[dict]) -> Optional[bool]:
    if not ref or ref.get("answer") in (None, ""):
        return None
    got = traj.get("answer") or ""
    if (ref.get("answer_match") or "contains") == "equals":
        return normalize(got) == normalize(ref["answer"])
    low, exp = got.lower(), str(ref["answer"]).strip().lower()
    # Whole-word match for the value as written ("3" in "now has 3 items", not in "13"), or any
    # other way it's usually written (1,240.00 / 1240.00; dates).
    return bool(re.search(rf"(?<![\w.]){re.escape(exp)}(?![\w]|\.\d)", low)) or \
        any(f in low for f in _text_forms(exp))


def efficiency(traj: dict, ref: Optional[dict] = None) -> dict:
    calls = tool_calls(traj)
    seen, repeats, run, worst = Counter(), 0, 1, (1, None)
    prev = None
    for c in calls:
        key = (c["name"], json.dumps(c["args"] or {}, sort_keys=True, default=str))
        repeats += seen[key] > 0
        seen[key] += 1
        run = run + 1 if key == prev else 1
        if seen[key] > worst[0]:
            worst = (seen[key], c)
        prev = key
    errors = [c for c in calls if c["error"]]
    recovered = sum(1 for i, c in enumerate(calls) if c["error"]
                    and any(d["name"] == c["name"] and not d["error"] for d in calls[i + 1:]))
    tokens = sum(s["tokens"] or 0 for s in traj["steps"])
    cost = sum(s["cost_usd"] or 0 for s in traj["steps"])
    dur = (traj["finished_at"] - traj["started_at"]).total_seconds() if traj.get("finished_at") else None
    budget = (ref or {}).get("max_steps")
    loop = worst[0] >= LOOP
    return {"steps": len(traj["steps"]), "tool_calls": len(calls),
            "reasoning_steps": sum(1 for s in traj["steps"] if s["kind"] == "reason"),
            "repeated_calls": repeats, "max_identical": worst[0], "loop_at": worst[1]["seq"] if loop else None,
            "loop_call": call_text(worst[1]["name"], worst[1]["args"]) if loop else None,
            "tool_errors": len(errors), "recovered": recovered, "tokens": tokens, "cost_usd": cost,
            "seconds": dur, "budget": budget,
            "passed": not loop and (budget is None or len(traj["steps"]) <= budget)}


def safety(traj: dict, rules: List[dict]) -> List[dict]:
    """Contracts this trajectory breaks, with the step (seq) where it did."""
    path = [contracts_mod.Step(s["name"] or s["kind"], s["args"] if s["kind"] == "tool" else None)
            for s in traj["steps"] if s["kind"] in ("tool", "answer")]
    seqs = [s["seq"] for s in traj["steps"] if s["kind"] in ("tool", "answer")]
    doc = type("Doc", (), {"document_type": traj.get("task"), "segment": None, "processing_mode": None})()
    out = []
    for c in rules:
        if not contracts_mod.applies(c, doc):
            continue
        b = contracts_mod.breaks(c, path, finished=True)
        if b:
            out.append({"contract_id": c.get("id"), "label": c.get("label") or contracts_mod.describe(c),
                        "severity": c.get("severity", "critical"), "detail": b["detail"], "tool": b["stage"],
                        "seq": seqs[b["at"]] if b["at"] is not None and b["at"] < len(seqs) else None})
    return out


# ---------- the first bad step ----------

def credit(traj: dict, ref: Optional[dict], rules: List[dict], check: Optional[str] = None,
           evaluation: Optional[dict] = None) -> Optional[dict]:
    """Where and how the trajectory first went wrong, for the failed `check` (or overall).
    {"mechanism", "seq", "stage", "detail", "expected_tool"}."""
    ev = evaluation or evaluate_one(traj, ref, rules)
    cmp_, eff, breaks_, state = ev["tool_calls"], ev["efficiency"], ev["safety"], ev["end_state"]
    candidates = []  # (seq, priority, finding)

    critical = [b for b in breaks_ if b["severity"] == "critical" and b["seq"] is not None]
    if critical:
        b = min(critical, key=lambda b: b["seq"])
        candidates.append((b["seq"], 0, {"mechanism": "unsafe_action", "seq": b["seq"], "stage": b["tool"],
                                         "detail": f"Broke “{b['label']}”: {b['detail']}."}))
    calls = tool_calls(traj)
    for i, c in enumerate(calls):
        if c["error"] and not any(d["name"] == c["name"] and not d["error"] for d in calls[i + 1:]):
            candidates.append((c["seq"], 1, {"mechanism": "tool_error", "seq": c["seq"], "stage": c["name"],
                                             "error": c["error"],
                                             "detail": f"{call_text(c['name'], c['args'])} failed: {c['error']}; "
                                                       "the agent carried on without it."}))
            break
    if eff["loop_at"] is not None:
        candidates.append((eff["loop_at"], 2, {"mechanism": "looped", "seq": eff["loop_at"],
                                               "stage": eff["loop_call"].split("(")[0],
                                               "detail": f"Called {eff['loop_call']} {eff['max_identical']} times "
                                                         "with identical arguments."}))
    d = (cmp_ or {}).get("divergence")
    if d and d["kind"] in ("wrong_tool", "bad_arguments", "extra"):
        mech = "wrong_tool" if d["kind"] in ("wrong_tool", "extra") else "bad_arguments"
        candidates.append((d["seq"], 3, {"mechanism": mech, "seq": d["seq"], "stage": d["tool"],
                                         "expected_tool": d.get("expected_tool"),
                                         "detail": d["detail"][0].upper() + d["detail"][1:] + "."}))

    relevant = {"safety": ("unsafe_action",), "efficiency": ("looped",)}.get(check)
    pool = [c for c in candidates if not relevant or c[2]["mechanism"] in relevant]
    if pool:
        return min(pool, key=lambda c: (c[0], c[1]))[2]
    if check == "efficiency":
        return {"mechanism": "looped", "seq": None, "stage": None,
                "detail": f"Took {eff['steps']} steps; the budget is {eff['budget']}."}
    if d and d["kind"] == "gave_up":
        return {"mechanism": "gave_up", "seq": d["seq"], "stage": d["expected_tool"],
                "expected_tool": d["expected_tool"], "detail": d["detail"][0].upper() + d["detail"][1:] + "."}
    if check in (None, "answer") and ev["answer"] is False and ref and ref.get("answer"):
        forms = _text_forms(str(ref["answer"]))
        for c in calls:
            text = json.dumps(c["result"], default=str).lower() if c["result"] is not None else ""
            if any(f in text for f in forms):
                return {"mechanism": "ignored_result", "seq": c["seq"], "stage": c["name"],
                        "detail": f"{call_text(c['name'], c['args'])} returned {ref['answer']!r}, and the final answer "
                                  "doesn't have it."}
    if check in (None, "end_state") and state and not all(a["passed"] for a in state):
        bad = next(a for a in state if not a["passed"])
        tool = next((c["name"] for c in reversed(calls) if bad["seq"] is not None and c["seq"] < bad["seq"]), None)
        return {"mechanism": "wrong_state", "seq": bad["seq"], "stage": tool,
                "detail": f"Expected {bad['assertion']}; got {bad['got']}."}
    answer_seq = next((s["seq"] for s in reversed(traj["steps"]) if s["kind"] == "answer"), None)
    return {"mechanism": "wrong_answer", "seq": answer_seq, "stage": "answer",
            "detail": f"The tool calls were as expected, but the answer {(traj.get('answer') or '')[:120]!r} doesn't "
                      f"have {(ref or {}).get('answer')!r}."}


# ---------- evaluating ----------

def evaluate_one(traj: dict, ref: Optional[dict], rules: List[dict]) -> dict:
    cmp_ = compare(traj, ref)
    state = check_state(traj, ref)
    return {"answer": check_answer(traj, ref), "end_state": state, "tool_calls": cmp_,
            "safety": safety(traj, rules), "efficiency": efficiency(traj, ref)}


def references(engine: Engine, tenant: str, case_ids: Optional[List[str]] = None) -> Dict[str, dict]:
    t = store.agent_references
    cond = [t.c.tenant == tenant]
    if case_ids is not None:
        cond.append(t.c.case_id.in_(list(case_ids)))
    with engine.connect() as conn:
        return {r.case_id: {k: v for k, v in r._mapping.items() if k != "tenant"}
                for r in conn.execute(select(t).where(and_(*cond)))}


def _brief(v, n=300) -> Optional[str]:
    if v is None:
        return None
    s = v if isinstance(v, str) else json.dumps(v, default=str)
    return s[:n]


def checks_for(traj: dict, ref: Optional[dict], rules: List[dict]) -> List[dict]:
    """The five checks as evaluation results (only those the reference and contracts make possible)."""
    ev = evaluate_one(traj, ref, rules)
    out = []

    def add(field, passed, expected, actual):
        why = None if passed else credit(traj, ref, rules, field, ev)
        out.append({"field": field, "status": "pass" if passed else "fail", "expected": _brief(expected),
                    "actual": _brief(actual), "reason": None if passed else f"{MECHANISMS[why['mechanism']]}: "
                                                                            f"{why['detail']}"[:2000]})
    if ev["answer"] is not None:
        add("answer", ev["answer"], ref["answer"], traj.get("answer"))
    if ev["end_state"] is not None:
        bad = [a for a in ev["end_state"] if not a["passed"]]
        add("end_state", not bad, "; ".join(a["assertion"] for a in ev["end_state"]),
            "; ".join(f"{a['assertion']}: got {a['got']}" for a in bad) or "all hold")
    if ev["tool_calls"] is not None:
        add("tool_calls", ev["tool_calls"]["passed"],
            " → ".join(call_text(e["tool"], e["args"]) for e in ev["tool_calls"]["expected"]),
            " → ".join(call_text(c["tool"], c["args"]) for c in ev["tool_calls"]["calls"]))
    if rules:
        crit = [b for b in ev["safety"] if b["severity"] == "critical"]
        add("safety", not crit, "no critical contract broken", "; ".join(b["detail"] for b in crit) or "none broken")
    e = ev["efficiency"]
    add("efficiency", e["passed"], f"≤ {e['budget']} steps" if e["budget"] else f"< {LOOP} identical calls",
        f"{e['steps']} steps, {e['max_identical']} identical calls at most")
    return out


def run_trajectories(engine: Engine, tenant: str, run_id: str) -> List[dict]:
    t = store.agent_trajectories
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(t.c.trajectory_id, t.c.case_id, t.c.attempt,
                                                              t.c.lineage, t.c.started_at, t.c.task, t.c.status)
                                                       .where(and_(t.c.tenant == tenant, t.c.run_id == run_id)))]


def result_rows(tenant: str, run_id: str, head: dict, checks: List[dict], evaluator: str = EVALUATOR) -> List[dict]:
    """A trajectory's checks as evaluation results of its test run (stable ids: re-checking replaces)."""
    from assay import ingest
    case = head["case_id"] or head["trajectory_id"]
    return [{"tenant": tenant, "result_id": ingest._derive(run_id, case, c["field"], evaluator, head["attempt"]),
             "run_id": run_id, "case_id": case, "document_id": head["trajectory_id"], "evaluator": evaluator,
             "attempt": head["attempt"], "ts": head["started_at"], "lineage": head["lineage"], "score": None, **c}
            for c in checks]


def evaluate_run(engine: Engine, source, tenant: str, run_id: str) -> dict:
    """Check every trajectory in an evaluation run against its case's reference and the
    source's contracts, and store the checks as evaluation results (idempotent). A trajectory
    still running is left for later: judged half-way, it would fail for not being done."""
    from assay import ingest
    heads = run_trajectories(engine, tenant, run_id)
    running = sum(1 for h in heads if h["status"] == "running")
    heads = [h for h in heads if h["status"] != "running"]
    trajs = source.trajectories([h["trajectory_id"] for h in heads])
    refs = references(engine, tenant, {h["case_id"] for h in heads if h["case_id"]})
    rules = contracts_mod.load(engine, source.name)
    rows = []
    for h in heads:
        traj = trajs.get(h["trajectory_id"])
        if traj is not None:
            rows += result_rows(tenant, run_id, h, checks_for(traj, refs.get(h["case_id"]), rules))
    ingest.upsert(engine, store.eval_results, rows, "result_id")
    counts = Counter((r["field"], r["status"]) for r in rows)
    return {"run_id": run_id, "trajectories": len(heads), "running": running, "results": len(rows),
            "checks": {f: {"pass": counts[(f, "pass")], "fail": counts[(f, "fail")]} for f in CHECKS
                       if counts[(f, "pass")] + counts[(f, "fail")]}}


def detail(engine: Engine, source, tenant: str, trajectory_id: str) -> Optional[dict]:
    """One trajectory with everything the trace view shows."""
    traj = source.trajectory(trajectory_id)
    if traj is None:
        return None
    ref = references(engine, tenant, [traj["case_id"]]).get(traj["case_id"]) if traj.get("case_id") else None
    rules = contracts_mod.load(engine, source.name)
    ev = evaluate_one(traj, ref, rules)
    failed = [c for c in CHECKS if (c == "answer" and ev["answer"] is False)
              or (c == "end_state" and ev["end_state"] and not all(a["passed"] for a in ev["end_state"]))
              or (c == "tool_calls" and ev["tool_calls"] and not ev["tool_calls"]["passed"])
              or (c == "safety" and any(b["severity"] == "critical" for b in ev["safety"]))
              or (c == "efficiency" and not ev["efficiency"]["passed"])]
    first_bad = credit(traj, ref, rules, None, ev) if failed else None
    ser = lambda v: v.isoformat() if isinstance(v, datetime) else v
    return {**{k: ser(v) for k, v in traj.items() if k != "steps"},
            "steps": [{k: ser(v) for k, v in s.items()} for s in traj["steps"]],
            "reference": {k: ser(v) for k, v in ref.items()} if ref else None,
            "answer_ok": ev["answer"], "end_state": ev["end_state"], "world": {
                k: v for k, v in end_state(traj).items()}, "tool_calls": ev["tool_calls"],
            "safety": ev["safety"], "efficiency": ev["efficiency"], "failed": failed, "first_bad": first_bad}


def features(traj: dict) -> set:
    """What a trajectory looked like, to compare failing cases with passing ones."""
    f = {f"task={traj.get('task')}"} if traj.get("task") else set()
    n = len(traj["steps"])
    f.add(f"steps={'1-5' if n <= 5 else '6-10' if n <= 10 else '11+'}")
    for c in tool_calls(traj):
        f.add(f"used:{c['name']}")
        if c["error"]:
            f.add(f"tool_error:{c['name']}")
    return f


# ---------- a run, against its baseline ----------

def summary(engine: Engine, source, tenant: str, run_id: str, baseline: Optional[str] = None) -> Optional[dict]:
    heads = run_trajectories(engine, tenant, run_id)
    if not heads:
        return None
    runs = agent_runs(engine, tenant)
    if baseline is None:
        this = next(r for r in runs if r["run_id"] == run_id)
        earlier = [r for r in runs if r["start"] < this["start"]]
        baseline = earlier[0]["run_id"] if earlier else None
    rules = contracts_mod.load(engine, source.name)

    def stats(rid):
        hs = run_trajectories(engine, tenant, rid)
        trajs = source.trajectories([h["trajectory_id"] for h in hs])
        refs = references(engine, tenant, {h["case_id"] for h in hs if h["case_id"]})
        per, mech, by_case = [], Counter(), defaultdict(list)
        checks = defaultdict(lambda: [0, 0])
        for h in hs:
            t = trajs.get(h["trajectory_id"])
            if t is None:
                continue
            ref = refs.get(h["case_id"])
            ev = evaluate_one(t, ref, rules)
            res = checks_for(t, ref, rules)
            for c in res:
                checks[c["field"]][0] += c["status"] == "pass"
                checks[c["field"]][1] += 1
            if any(c["status"] == "fail" for c in res):
                mech[credit(t, ref, rules, None, ev)["mechanism"]] += 1
            e = ev["efficiency"]
            row = {"trajectory_id": h["trajectory_id"], "case_id": h["case_id"], "attempt": h["attempt"],
                   "task": h["task"], **{k: e[k] for k in ("steps", "tool_calls", "repeated_calls", "tool_errors",
                                                           "recovered", "tokens", "cost_usd", "seconds")},
                   "precision": (ev["tool_calls"] or {}).get("precision"),
                   "recall": (ev["tool_calls"] or {}).get("recall"),
                   "checks": {c["field"]: c["status"] for c in res}}
            per.append(row)
            by_case[h["case_id"]].append(row)
        agg = lambda k, f=mean: f([r[k] for r in per if r[k] is not None]) if any(r[k] is not None for r in per) else None
        return {"trajectories": len(per), "cases": len(by_case),
                "checks": {k: {"passed": v[0], "total": v[1], "rate": v[0] / v[1]} for k, v in checks.items()},
                "mechanisms": dict(mech.most_common()),
                "efficiency": {"steps": agg("steps"), "tool_calls": agg("tool_calls"),
                               "repeated_calls": agg("repeated_calls"), "tool_errors": agg("tool_errors"),
                               "recovery_rate": (sum(r["recovered"] for r in per) / sum(r["tool_errors"] for r in per))
                               if sum(r["tool_errors"] for r in per) else None,
                               "tokens": agg("tokens"), "cost_usd": agg("cost_usd"), "seconds": agg("seconds"),
                               "precision": agg("precision"), "recall": agg("recall")},
                "per_case": {c: {"steps": median(r["steps"] for r in rs), "cost_usd": median(r["cost_usd"] for r in rs),
                                 "tool_calls": median(r["tool_calls"] for r in rs)} for c, rs in by_case.items()},
                "trajectories_list": per}

    cur = stats(run_id)
    base = stats(baseline) if baseline else None
    costlier = []
    if base:
        for c, x in cur["per_case"].items():
            b = base["per_case"].get(c)
            if b and x["steps"] >= 1.5 * b["steps"] and x["steps"] - b["steps"] >= 2:
                costlier.append({"case_id": c, "steps_before": b["steps"], "steps_now": x["steps"],
                                 "cost_before": b["cost_usd"], "cost_now": x["cost_usd"]})
        costlier.sort(key=lambda r: r["steps_before"] - r["steps_now"])
    return {"run_id": run_id, "baseline": baseline, "lineage": next(r["lineage"] for r in runs if r["run_id"] == run_id),
            "evaluated": _evaluated(engine, tenant, run_id), "current": {k: v for k, v in cur.items() if k != "per_case"},
            "previous": {k: v for k, v in base.items() if k not in ("per_case", "trajectories_list")} if base else None,
            "costlier": costlier[:50]}


def _evaluated(engine: Engine, tenant: str, run_id: str) -> bool:
    t = store.eval_results
    with engine.connect() as conn:
        return conn.execute(select(t.c.result_id).where(and_(t.c.tenant == tenant, t.c.run_id == run_id,
                                                             t.c.evaluator == EVALUATOR)).limit(1)).first() is not None


def agent_runs(engine: Engine, tenant: str) -> List[dict]:
    t = store.agent_trajectories
    with engine.connect() as conn:
        rows = conn.execute(select(t.c.run_id, t.c.case_id, t.c.started_at, t.c.lineage)
                            .where(and_(t.c.tenant == tenant, t.c.run_id.is_not(None)))).all()
    runs = defaultdict(lambda: {"trajectories": 0, "cases": set(), "start": None, "lineage": None})
    for r in rows:
        x = runs[r.run_id]
        x["trajectories"] += 1
        x["cases"].add(r.case_id)
        x["start"] = min(x["start"] or r.started_at, r.started_at)
        x["lineage"] = x["lineage"] or r.lineage
    return sorted([{"run_id": k, "trajectories": v["trajectories"], "cases": len(v["cases"]),
                    "start": v["start"].isoformat(), "lineage": v["lineage"] or {},
                    "evaluated": _evaluated(engine, tenant, k)} for k, v in runs.items()],
                  key=lambda r: r["start"], reverse=True)
