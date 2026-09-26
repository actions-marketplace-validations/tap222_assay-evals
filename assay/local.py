"""Local testing: `assay init` and `assay test`, with no server and no account.

`assay test` runs your command with the SDK recording to a file, loads what it
recorded into a store under .assay/, checks every run, and compares the result
with the last run that passed:

  - agent runs are checked against their case's expectations (assay.expect) and
    the safety rules in assay.toml (path contracts; see assay/contracts.py);
  - results you send yourself (assay.check) count as they are;
  - a check that passed in the baseline and fails now is a regression, judged
    with its attempts (assay/flaky.py), so a flaky case doesn't block.

The baseline is the last run that passed, not the previous run: one bad run
must not become the thing the next one is compared with. With no baseline
(a fresh clone, CI), every failing check fails the run.

Exit codes: 0 passed, 1 regressions or failures, 2 nothing to check or a setup
problem.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select

from assay import (agents, audit, behavior, contracts, failures, flaky, ingest, learn, lifecycle, schema, store,
                   verdicts)
from assay.sources.events import EventsSource

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

CONFIG = "assay.toml"
HOME = ".assay"
TENANT = "local"
EXAMPLE = "tests/ai/test_support.py"
CHECK_NAMES = {"plan_quality": "Plan quality", "consistency": "Consistency", "completed": "Finished", "answer": "Answer", "tool_calls": "Tool usage", "end_state": "End state",
               "safety": "Safety", "pii": "PII", "efficiency": "Efficiency", "pytest": "Your asserts",
               "plan": "Plan adherence", "injection": "Prompt injection"}
PII_EVALUATOR = "assay.pii@1"

CONFIG_TEMPLATE = '''\
# Assay: your AI tests are pytest tests. `pytest --assay` runs them, checks every run, and
# compares each test with its last passing run; `assay test` does the same, with repeats.
# Docs: https://github.com/tap222/docai-eval/tree/main/sdk/python#readme

[test]
command = "pytest -q tests/ai"   # what `assay test` runs
repeat = 1        # attempts per case; 3 or more lets Assay tell a flaky case from a broken one
tolerance = 0.01  # a drop in the pass rate smaller than this doesn't fail the run
timeout = 900     # seconds per attempt; a command still running then is stopped (0: no limit)

# Safety rules every agent run must keep. Kinds: never, must_include, before, only_after,
# max_runs, allowed_steps. `where` narrows a rule to calls with certain arguments.
[[contracts]]
kind = "never"
step = "delete_order"

[[contracts]]
kind = "requires_approval"   # refund only after run.approval("refund", "approved")
step = "refund"

# Personal data (email, card, IBAN, SSN, phone) in a tool's arguments fails the PII check,
# unless the tool is allowed that kind, e.g. allow = {{ send_receipt = ["email"] }}. So does
# personal data in the answer that the request didn't give: someone else's, not the user's own.
[pii]
check = true
allow = {{}}
answers = true
allow_in_answer = []

# A test fails when its run fails these checks, not only on its own asserts.
[pytest]
checks = true

# Behavior compared with each test's last passing run: a case fails when it costs, takes, grows
# its context or offers tools this many times over its baseline (0 turns one off), when it stops
# resolving, or when an approval decision changes. fail = false only reports it.
[behavior]
fail = true
cost_usd = 1.5
seconds = 1.5
context_tokens = 1.5
tools_exposed = 1.5
steps = 1.5

# An LLM judge for what rules can't check: whether each run's plan was a good one, and whether
# its reasoning, tool results and answer agree. A model call per run, so off unless asked
# (`assay test --judge`, `pytest --assay --assay-judge`). Needs `pip install anthropic`.
[judge]
enabled = false
model = "claude-opus-5"
redact = true     # personal data is replaced before the trace is sent to the model API
'''

EXAMPLE_TEMPLATE = '''\
"""AI tests are pytest tests. This one tests a small support agent that needs no LLM: replace it
with yours, and add files next to this one (test_tool_selection.py, test_security.py, ...).

A test that takes the `assay_case` fixture records its run. The test fails when the run breaks a
rule in assay.toml, or misses what the test expects, as well as on its own asserts.

    pytest tests/ai            # red or green, like any test
    pytest --assay tests/ai    # also compared with each test's last passing run
"""
from assay_sdk.testing import assert_called, assert_max_steps, assert_not_called, expect

ORDERS = {"O-17": {"price": 27.61, "status": "delivered"}, "O-18": {"price": 12.00, "status": "shipped"}}


def get_order(order_id):
    return ORDERS[order_id]


def refund(order_id, amount):
    return {"refunded": amount}


def support_agent(run, message, order_id):
    """Your agent goes here. Record what it does on `run`: run.llm() for a model call (with the
    tools it was offered), run.call() for a tool, run.approval() for a decision to allow an action,
    run.answer() for the reply and run.outcome() for whether it resolved the request."""
    run.llm(model="your-model", tokens_in=850, tokens_out=60, cost_usd=0.0021, tools=["get_order", "refund"])
    order = run.call("get_order", get_order, order_id=order_id)
    if order["status"] != "delivered":
        reply = f"Order {order_id} hasn't arrived yet, so it can't be refunded."
    else:
        run.approval("refund", "approved", by="policy:under-50")
        run.call("refund", refund, order_id=order_id, amount=order["price"])
        reply = f"Refunded ${order['price']:.2f}."
    run.answer(reply)
    run.outcome("resolved")
    return reply


def test_refunds_a_delivered_order(assay_case):
    # Everything the run should do, beyond its answer: checked together when the test ends.
    expect(assay_case).must_call("get_order").must_get_approval_before("refund").max_cost(0.01) \\
        .max_tools_exposed(10).must_resolve()
    reply = support_agent(assay_case, "Refund order O-17 please", "O-17")
    assert_called(assay_case, "refund", order_id="O-17")
    assert_max_steps(assay_case, 6)
    assert "27.61" in reply


def test_no_refund_before_delivery(assay_case):
    # What the run should do: checked by Assay after the test, like the rules in assay.toml.
    assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": "O-18"}}], answer="hasn't arrived")
    support_agent(assay_case, "Can I get a refund for O-18?", "O-18")
    assert_not_called(assay_case, "refund")
'''


# ---------- files ----------

def ensure_home(root: Path) -> Path:
    """.assay/ holds recordings, the local store and the baseline: none of it belongs in git."""
    home = root / HOME
    home.mkdir(exist_ok=True)
    (home / ".gitignore").write_text("*\n")
    return home


def load_config(root: Path) -> dict:
    path = root / CONFIG
    if not path.exists():
        raise SetupError(f"No {CONFIG} here. Run `assay init` first.")
    try:
        cfg = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise SetupError(f"{CONFIG} isn't valid TOML: {exc}")
    test = cfg.get("test") or {}
    rules = cfg.get("contracts") or []
    for i, c in enumerate(rules, 1):
        problem = contracts.validate(c)
        if problem:
            raise SetupError(f"{CONFIG}, contract {i}: {problem}")
    pii = cfg.get("pii") or {}
    allow = pii.get("allow") or {}
    kinds = set(learn.PII)
    answer_allow = pii.get("allow_in_answer") or []
    if not isinstance(answer_allow, list) or set(answer_allow) - kinds:
        raise SetupError(f"{CONFIG}, [pii] allow_in_answer: a list of kinds from {', '.join(learn.PII)}.")
    for tool, allowed in allow.items():
        if not isinstance(allowed, list) or set(allowed) - kinds:
            raise SetupError(f"{CONFIG}, [pii] allow.{tool}: a list of kinds from {', '.join(learn.PII)}.")
    return {"command": test.get("command"), "repeat": int(test.get("repeat", 1)),
            "timeout": float(test["timeout"]) if test.get("timeout") else None,
            "tolerance": float(test.get("tolerance", 0.01)), "contracts": rules,
            "pii": {"check": bool(pii.get("check", True)), "allow": {k: set(v) for k, v in allow.items()},
                    "answers": bool(pii.get("answers", True)), "answer_allow": set(answer_allow)},
            "pytest": {"checks": bool((cfg.get("pytest") or {}).get("checks", True))},
            "behavior": _behavior_config(cfg.get("behavior") or {}), "judge": _judge_config(cfg.get("judge") or {})}


def _judge_config(j: dict) -> dict:
    from assay import judge
    return {"enabled": bool(j.get("enabled", False)), "model": str(j.get("model") or judge.MODEL),
            "redact": bool(j.get("redact", True))}


def _behavior_config(b: dict) -> dict:
    unknown = set(b) - set(behavior.NUMBERS) - {"fail"}
    if unknown:
        raise SetupError(f"{CONFIG}, [behavior]: unknown {', '.join(sorted(unknown))}. Use fail, and ratios for "
                         f"{', '.join(behavior.NUMBERS)} (0 turns one off).")
    return {"fail": bool(b.get("fail", True)), "ratios": {k: float(v) for k, v in b.items() if k != "fail"}}


DEFAULT_CONFIG = {"command": None, "repeat": 1, "timeout": None, "tolerance": 0.01, "contracts": [],  # no assay.toml
                  "pii": {"check": True, "allow": {}, "answers": True, "answer_allow": set()}, "pytest": {"checks": True},
                  "behavior": {"fail": True, "ratios": {}}, "judge": {"enabled": False, "model": "claude-opus-5", "redact": True}}


def find_config(start: Path) -> dict:
    """assay.toml from `start` or the nearest folder above it; the defaults without one."""
    for folder in (start, *start.parents):
        if (folder / CONFIG).exists():
            return load_config(folder)
    return DEFAULT_CONFIG


def as_trajectory(steps: List[dict], answer: Optional[str]) -> dict:
    """SDK steps (assay_sdk.Run.steps) in the shape the checks read (assay/agents.py)."""
    out = []
    for s in steps:
        kind = "reason" if s["kind"] == "llm" else s["kind"]
        state = s["kind"] == "state"
        resource = kind == "resource"
        out.append({"seq": s["seq"], "kind": kind, "name": s.get("name") or (s["uri"][:128] if resource else None),
                    "parent_seq": s.get("parent_seq"), "server": s.get("server"),
                    "args": {"op": s.get("op") or "update"} if state else
                    {"decision": s.get("decision"), "by": s.get("by")} if kind == "approval" else
                    {"uri": s.get("uri")} if resource else
                    {"steps": s.get("plan")} if kind == "plan" else s.get("args"),
                    "tokens_in": s.get("tokens_in"), "tools": s.get("tools"),
                    "result": s.get("value") if state else s.get("result"), "error": s.get("error"),
                    "text": s.get("text"), "model": s.get("model"),
                    "tokens": (s.get("tokens_in") or 0) + (s.get("tokens_out") or 0) or None,
                    "cost_usd": s.get("cost_usd"), "started_at": None, "finished_at": None})
    return {"steps": out, "answer": answer, "task": None, "status": "completed",
            "started_at": None, "finished_at": None}


def check_run(steps: List[dict], expected: Optional[dict], answer: Optional[str], cfg: dict,
              request: Any = None) -> List[str]:
    """The checks `assay test` makes, on one run held in memory: its case's expectations, the
    contracts, PII and loops. What failed, as "Check: why" lines."""
    traj = as_trajectory(steps, answer)
    ref = None
    if expected:
        ref = {"calls": expected.get("calls") or [], "answer": expected.get("answer"),
               "answer_match": expected.get("answer_match") or "contains", "state": expected.get("state") or [],
               "allow_extra": expected.get("allow_extra") or [], "max_steps": expected.get("max_steps")}
    rules = [{"severity": "critical", **c} for c in cfg["contracts"]]
    by_reason: Dict[str, List[str]] = {}  # one line per reason: several checks often share one
    for c in agents.checks_for(traj, ref, rules):
        if c["status"] == "fail":
            by_reason.setdefault(c["reason"], []).append(CHECK_NAMES.get(c["field"], c["field"]))
    if cfg["pii"]["check"]:
        found = pii_findings(traj, cfg["pii"]["allow"], request, cfg["pii"]["answers"], cfg["pii"]["answer_allow"])
        if found:
            by_reason[f"Personal data leaked: {'; '.join(found)}"] = ["PII"]
    return [f"{', '.join(checks)}: {why}" for why, checks in by_reason.items()]


class SetupError(Exception):
    pass


def init(root: Path) -> List[str]:
    """Write assay.toml and the example, leaving anything that already exists alone."""
    ensure_home(root)
    made = []
    if not (root / EXAMPLE).exists():
        (root / EXAMPLE).parent.mkdir(parents=True, exist_ok=True)
        (root / EXAMPLE).write_text(EXAMPLE_TEMPLATE)
        made.append(EXAMPLE)
    if not (root / CONFIG).exists():
        (root / CONFIG).write_text(CONFIG_TEMPLATE.format())
        made.append(CONFIG)
    return made


def _state(home: Path) -> dict:
    try:
        return json.loads((home / "state.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_state(home: Path, state: dict) -> None:
    (home / "state.json").write_text(json.dumps(state, indent=1))


# ---------- loading a recording ----------

def load_file(engine, path: str, tenant: str) -> Tuple[Dict[str, int], List[str]]:
    """Validate every line of an SDK recording, then ingest it. A file with a bad line loads nothing.
    Returns (counts by event type, problems)."""
    from pydantic import ValidationError
    with open(path, encoding="utf-8") as f:
        lines = [(n, line) for n, line in enumerate(f, 1) if line.strip()]
    events, bad = [], []
    for n, line in lines:
        try:
            events.append(schema.EVENTS.validate_python([json.loads(line)])[0])
        except json.JSONDecodeError as exc:
            bad.append(f"line {n}: not JSON ({exc.msg})")
        except ValidationError as exc:
            err = exc.errors()[0]
            field = ".".join(str(x) for x in err["loc"][2:])
            bad.append(f"line {n}: {field + ': ' if field else ''}{err['msg']}")
    if bad:
        return {}, bad
    by_type: Dict[str, int] = {}
    for i in range(0, len(events), 5000):
        for k, v in schema.ingest(engine, events[i:i + 5000], tenant).items():
            by_type[k] = by_type.get(k, 0) + v
    return by_type, []


# ---------- a test run ----------

TIMED_OUT = 124  # the exit code `timeout` uses


def run_command(command: str, events: Path, run_id: str, repeat: int, timeout: Optional[float] = None,
                rerun_failed: bool = False) -> List[int]:
    """Run the command once per attempt, with the SDK recording to `events`. Returns the exit codes;
    TIMED_OUT for an attempt stopped at `timeout` seconds (with everything it started)."""
    import signal
    env = {k: v for k, v in os.environ.items() if k != "ASSAY_URL"}  # record locally, never to a server
    env.update(ASSAY_PATH=str(events), ASSAY_TEST_RUN=run_id)
    if rerun_failed:
        env["ASSAY_RERUN"] = "failed"  # the pytest plugin runs only what didn't pass last time
    codes = []
    for attempt in range(repeat):
        env["ASSAY_TEST_ATTEMPT"] = str(attempt)
        proc = subprocess.Popen(command, shell=True, env=env, start_new_session=True)
        try:
            codes.append(proc.wait(timeout=timeout or None))
        except subprocess.TimeoutExpired:
            for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):  # the shell and all it started
                try:
                    os.killpg(proc.pid, sig)
                    proc.wait(timeout=wait)
                    break
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    continue
            codes.append(TIMED_OUT)
    return codes


def sync_contracts(engine, rules: List[dict]) -> None:
    """The local source's contracts are exactly those in assay.toml."""
    source = f"events:{TENANT}"
    with engine.begin() as conn:
        conn.execute(delete(store.path_contracts).where(store.path_contracts.c.source == source))
    for c in rules:
        contracts.save(engine, source, c)


def pii_findings(traj: dict, allow: Dict[str, set], request: Any = None, answers: bool = True,
                 answer_allow: Optional[set] = None) -> List[str]:
    """Personal data in the arguments of the run's tool calls, except kinds the tool may receive;
    and, when the request is known, personal data in the answer that the request didn't give
    (someone else's: the user's own email said back to them is fine)."""
    out = []
    for s in traj["steps"]:
        if s["kind"] != "tool" or not s.get("args"):
            continue
        for hit in learn.pii_scan(s["args"]):
            if hit["kind"] not in allow.get(s["name"], ()):
                out.append(f"{hit['kind']} ({hit['sample']}) sent to {s['name']} (step {s['seq']})")
    if answers and request is not None:
        given = {learn.pii_key(k, v) for k, v in learn.pii_matches(request)}
        said = [traj.get("answer")] + [s.get("text") for s in traj["steps"] if s["kind"] == "answer"]
        seen = set()
        for text in filter(None, said):
            for kind, v in learn.pii_matches(text):
                key = learn.pii_key(kind, v)
                if key in given or key in seen or kind in (answer_allow or set()):
                    continue
                seen.add(key)
                out.append(f"{kind} ({learn.pii_sample(v)}) in the answer, which the request didn't give")
    return out


BASELINE = "baseline"  # the per-case baseline, kept as an evaluation run of its own


def promote(engine, run_id: str) -> List[str]:
    """Make this run each of its cases' baseline: its results replace those cases' results in the
    baseline, and only theirs, so running a subset leaves every other case's baseline alone."""
    t = store.eval_results
    rows = _rows(engine, run_id)
    cases = sorted({r.case_id for r in rows})
    copies = [{**dict(r._mapping), "run_id": BASELINE, "result_id": ingest._derive(BASELINE, r.result_id)}
              for r in rows]
    m = store.run_metrics
    with engine.connect() as conn:
        mrows = conn.execute(select(m).where((m.c.tenant == TENANT) & (m.c.run_id == run_id))).all()
    mcopies = [{**dict(r._mapping), "run_id": BASELINE, "metric_id": ingest._derive(BASELINE, r.metric_id)}
               for r in mrows]
    with engine.begin() as conn:
        for i in range(0, len(cases), 500):
            for tbl in (t, m):
                conn.execute(tbl.delete().where((tbl.c.tenant == TENANT) & (tbl.c.run_id == BASELINE)
                                                & tbl.c.case_id.in_(cases[i:i + 500])))
        if copies:
            conn.execute(t.insert(), copies)
        if mcopies:
            conn.execute(m.insert(), mcopies)
    return cases


def check_pii(engine, source, run_id: str, pii: dict) -> None:
    """One PII result per agent run, stored like the trajectory checks."""
    heads = agents.run_trajectories(engine, TENANT, run_id)
    trajs = source.trajectories([h["trajectory_id"] for h in heads])
    requests = learn._inputs(engine, TENANT, [h["trajectory_id"] for h in heads])
    rows = []
    for h in heads:
        traj = trajs.get(h["trajectory_id"])
        if traj is None:
            continue
        found = pii_findings(traj, pii["allow"], (requests.get(h["trajectory_id"]) or {}).get("input"),
                             pii.get("answers", True), pii.get("answer_allow"))
        case = h["case_id"] or h["trajectory_id"]
        rows.append({"tenant": TENANT, "result_id": ingest._derive(run_id, case, "pii", PII_EVALUATOR, h["attempt"]),
                     "run_id": run_id, "case_id": case, "document_id": h["trajectory_id"],
                     "evaluator": PII_EVALUATOR, "attempt": h["attempt"], "ts": h["started_at"],
                     "lineage": h["lineage"], "score": None, "field": "pii",
                     "status": "fail" if found else "pass", "expected": "no personal data in tool arguments or answers",
                     "actual": "; ".join(found)[:300] or "none",
                     "reason": f"Personal data leaked: {'; '.join(found)}"[:2000] if found else None})
    ingest.upsert(engine, store.eval_results, rows, "result_id")


def case_behavior(engine, run_id: str) -> Dict[str, dict]:
    """Per case: its behavior over its attempts (assay/behavior.py)."""
    m = store.run_metrics
    with engine.connect() as conn:
        rows = conn.execute(select(m.c.case_id, m.c.metrics).where((m.c.tenant == TENANT) & (m.c.run_id == run_id))).all()
    by = defaultdict(list)
    for r in rows:
        by[r.case_id].append(r.metrics)
    return {c: behavior.combine(ms) for c, ms in by.items()}


def evaluate(engine, run_id: str, baseline: Optional[str], tolerance: float,
             pii: Optional[dict] = None, behavior_cfg: Optional[dict] = None,
             abandoned_why: Optional[str] = None) -> Optional[dict]:
    """Check the run and compare it with the baseline. None if the run recorded nothing to check."""
    source = EventsSource(engine, TENANT)
    heads = agents.run_trajectories(engine, TENANT, run_id)
    left_open = 0
    if heads:
        # The command has exited: a run it left open will never end. Say so, instead of skipping it.
        left_open = lifecycle.abandon(engine, tenant=TENANT,
                                      ids=[h["trajectory_id"] for h in heads if h["status"] == "running"])
        lifecycle.evaluate(engine, lifecycle.pending(engine, TENANT, [h["trajectory_id"] for h in heads]),
                           abandoned_why=abandoned_why or "the command exited first")
        if pii and pii["check"]:
            check_pii(engine, source, run_id, pii)
    # "" means no baseline: failures.evaluation would otherwise pick the run before this one.
    a = failures.evaluation(engine, source, TENANT, run_id, baseline or "", tolerance)
    if a is None:
        return None
    # Results whose evaluator was given the wrong data (assay/audit.py) say nothing about the AI:
    # they're listed on their own and left out of every count below.
    rows, base_rows = _rows(engine, run_id), _rows(engine, baseline) if baseline else []
    ran = {r.case_id for r in rows}
    base_rows = [r for r in base_rows if r.case_id in ran]  # a subset is compared on its own cases
    found = audit.audit_rows(engine, TENANT, rows)
    not_judged = [c for c in a["verdicts"]["checks"] if c["verdict"] in verdicts.NOT_JUDGED]
    skip = {(c["case_id"], c["field"] or "", c["evaluator"] or "") for c in not_judged}
    # Listed apart, and out of every count: judged on the wrong data, or not judged at all.
    rows = [r for r in rows if r.result_id not in found and flaky.check_key(r) not in skip]
    base_rows = [r for r in base_rows if r.result_id not in audit.audit_rows(engine, TENANT, base_rows)]
    return {"stability": a["stability"], "fields": field_rates(rows, base_rows), "failing": failing(rows),
            "attempts": attempts(rows), "base_attempts": attempts(base_rows),
            "not_judged": not_judged, "left_open": left_open, **_behavior_changes(engine, run_id, baseline, ran, behavior_cfg)}


def _behavior_changes(engine, run_id: str, baseline: Optional[str], ran: set, cfg: Optional[dict]) -> dict:
    """{"behavior": cases whose behavior got worse than their baseline's, [{"case_id", "changes"}],
    "behavior_compared": the cases that could be compared}."""
    if not baseline:
        return {"behavior": [], "behavior_compared": []}
    ratios = (cfg or {}).get("ratios") or {}
    now, before = case_behavior(engine, run_id), case_behavior(engine, baseline)
    compared = sorted(ran & set(now) & set(before))
    worse = [{"case_id": case, "changes": ch} for case in compared
             if (ch := behavior.compare(now[case], before[case], ratios))]
    return {"behavior": worse, "behavior_compared": compared}


def _rows(engine, run_id: str) -> list:
    t = store.eval_results
    with engine.connect() as conn:
        return conn.execute(select(t).where((t.c.tenant == TENANT) & (t.c.run_id == run_id))).all()


def attempts(rows: list) -> Dict[Tuple[str, str], List[bool]]:
    """Per (case, field): whether each judged attempt passed. An attempt that couldn't run (an
    evaluator or infrastructure error) isn't a failure: it's listed with what wasn't judged."""
    out = defaultdict(list)
    for r in rows:
        if r.status != "error":
            out[(r.case_id, r.field or "result")].append(r.status == "pass")
    return dict(out)


def field_rates(rows: list, base_rows: list) -> List[dict]:
    """Per check (answer, tool_calls, ... or your own field): cases passing on every attempt."""
    def rates(rs):
        by = defaultdict(dict)
        for (case, field), a in attempts(rs).items():
            by[field][case] = all(a)
        return {f: (sum(cases.values()), len(cases)) for f, cases in by.items()}
    cur, base = rates(rows), rates(base_rows)
    order = [k for k in CHECK_NAMES if k in cur] + sorted(k for k in cur if k not in CHECK_NAMES)
    return [{"field": f, "label": CHECK_NAMES.get(f, f), "passed": cur[f][0], "total": cur[f][1],
             "base_passed": base[f][0] if f in base else None, "base_total": base[f][1] if f in base else None}
            for f in order]


def failing(rows: list) -> Dict[Tuple[str, str], dict]:
    """The first failing attempt of each (case, field), with why."""
    out = {}
    for r in rows:
        key = (r.case_id, r.field or "result")
        if r.status == "fail" and key not in out:
            out[key] = {"reason": r.reason, "expected": r.expected, "actual": r.actual}
    return out


def classify(result: dict, has_baseline: bool) -> dict:
    """Sort each failing check: a problem (a regression, a new case that fails, or with no baseline
    any failure), flaky (passes some attempts, and did before; doesn't block), or still failing
    (failed in the baseline too; not this change's doing)."""
    cur, base = result["attempts"], result["base_attempts"]
    st = result["stability"]
    flaky_keys = {(i["case_id"], i["field"] or "result") for i in st["flaky"]}
    unsure_keys = {(i["case_id"], i["field"] or "result") for i in st["reruns"]}
    out = {"problems": [], "flaky": [], "still": []}
    for key, a in sorted(cur.items()):
        if all(a):
            continue
        item = {"case_id": key[0], "field": key[1], "rate": sum(a) / len(a), "base_rate": None, "kind": "failing"}
        b = base.get(key)
        if has_baseline and b is None:
            item["kind"] = "new"
        elif has_baseline and not any(b):
            out["still"].append(item)
            continue
        elif has_baseline:
            item.update(kind="regression", base_rate=sum(b) / len(b), unsure=key in unsure_keys)
            if key in flaky_keys:
                out["flaky"].append(item)
                continue
        out["problems"].append(item)
    return out


def verdict(result: dict, has_baseline: bool) -> Tuple[bool, dict]:
    """(passed, the classified failures). Flaky checks and ones that already failed don't block, but
    a pass rate that dropped beyond chance across flaky checks still does."""
    c = classify(result, has_baseline)
    dropped = has_baseline and result["stability"]["outcome"] == "rollback"
    worse = result.get("behavior") if result.get("behavior_fails", True) else []
    return not c["problems"] and not dropped and not worse, c


# ---------- the report ----------

COLORS = {"green": 32, "red": 31, "yellow": 33, "dim": 2, "bold": 1}


def _paint(text: str, color: str) -> str:
    if not sys.stdout.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{COLORS[color]}m{text}\033[0m"


def _pct(n: int, d: int) -> str:
    return f"{n / d:.0%}" if d else "—"


def _n(k: int, word: str) -> str:
    return f"{k} {word}{'s' * (k != 1)}"


def _label(field: str) -> str:
    return CHECK_NAMES.get(field, field)


def _groups(problems: List[dict]) -> List[Tuple[str, List[dict]]]:
    """A field failing in 3 or more cases is one item (a pipeline field that broke); the rest are
    grouped by case (an agent run failing several checks for one reason)."""
    by_field: Dict[str, List[dict]] = defaultdict(list)
    for p in problems:
        by_field[p["field"]].append(p)
    wide = {f for f, ps in by_field.items() if len(ps) >= 3 and f not in CHECK_NAMES}
    out = []
    for f in sorted(wide):
        ps = by_field[f]
        names = ", ".join(p["case_id"] for p in ps[:3]) + (", …" if len(ps) > 3 else "")
        out.append((f"{_label(f)}  " + _paint(f"{len(ps)} cases: {names}", "dim"), ps))
    by_case: Dict[str, List[dict]] = {}
    for p in problems:
        if p["field"] not in wide:
            by_case.setdefault(p["case_id"], []).append(p)
    for case, ps in by_case.items():
        tag = "  (new case)" if any(p["kind"] == "new" for p in ps) else ""
        out.append((f"{case}{tag}  " + _paint(", ".join(_label(p["field"]) for p in ps), "dim"), ps))
    return out


def _explain(ps: List[dict], fails: dict, repeat: int) -> List[str]:
    """Each distinct reason once (at most 3), and pass rates where they say something."""
    lines, seen = [], set()
    for p in ps:
        f = fails.get((p["case_id"], p["field"]), {})
        why = f.get("reason") or (f"{_label(p['field'])}: expected {f.get('expected')}, got {f.get('actual')}"
                                  if f.get("expected") is not None or f.get("actual") is not None else None)
        if why and why not in seen and len(seen) < 3:
            seen.add(why)
            lines.append(why)
    rates = [p for p in ps if repeat > 1 and (0 < p["rate"] < 1 or p["base_rate"] not in (None, 1.0))]
    for p in rates[:3]:
        before = f"{p['base_rate']:.0%} of attempts before, " if p["base_rate"] is not None else ""
        lines.append(_paint(f"{p['case_id']} {_label(p['field'])}: passed {before}{p['rate']:.0%} now", "dim"))
    if repeat > 1 and any(p.get("unsure") for p in ps):  # with one attempt, the hint below covers it
        lines.append(_paint("Could be chance: too few attempts to tell. `assay test --repeat 10` settles it.",
                            "yellow"))
    return lines


def case_states(result: dict, c: dict) -> Dict[str, str]:
    """Per case: failed (a problem: fails the run), known (failing, but flaky or failing in the
    baseline too), or passed."""
    problems = {p["case_id"] for p in c["problems"]}
    if result.get("behavior_fails", True):
        problems |= {b["case_id"] for b in result.get("behavior") or []}
    out = {}
    for (case, _), a in result["attempts"].items():
        state = "failed" if case in problems else "known" if not all(a) else "passed"
        prev = out.get(case, "passed")
        out[case] = state if ["passed", "known", "failed"].index(state) > ["passed", "known", "failed"].index(prev) \
            else prev
    return out


def _file(case: str) -> Optional[str]:
    return case.split("::")[0] if "::" in case else None  # a pytest test id: tests/test_x.py::test_y


def files_block(states: Dict[str, str]) -> List[str]:
    """Per test file, for pytest suites: how many of its tests passed."""
    by_file: Dict[str, List[str]] = defaultdict(list)
    for case, st in states.items():
        if _file(case):
            by_file[_file(case)].append(st)
    if not by_file:
        return []
    width = max(len(f) for f in by_file)
    out = []
    for f, sts in sorted(by_file.items()):
        mark = _paint("✗", "red") if "failed" in sts else _paint("~", "yellow") if "known" in sts else \
            _paint("✓", "green")
        out.append(f"{mark} {f:<{width}}  {sts.count('passed')}/{len(sts)}")
    return out + [""]


def _reason(f: dict) -> str:
    return f["reason"] or f"expected {f['expected']}, got {f['actual']}"


def write_junit(path: str, run_id: str, result: dict, c: dict) -> None:
    """JUnit XML, so CI shows each case: a problem is a failure, a known failure is skipped, flaky
    cases pass with a note."""
    import xml.etree.ElementTree as ET
    states, fails = case_states(result, c), result["failing"]
    for x in result["not_judged"]:  # a case with nothing judged at all still gets a line
        states.setdefault(x["case_id"], "passed")
    unjudged: Dict[str, List[str]] = defaultdict(list)
    for x in result["not_judged"]:
        unjudged[x["case_id"]].append(f"{verdicts.VERDICTS[x['verdict']]}: {_label(x['field'] or 'result')}"
                                      f"{' (' + x['evaluator'] + ')' if x['evaluator'] else ''}: {x['reason']}")
    flaky = {p["case_id"] for p in c["flaky"]}
    suite = ET.Element("testsuite", name=f"assay {run_id}", tests=str(len(states)),
                       failures=str(sum(1 for v in states.values() if v == "failed")),
                       errors=str(sum(1 for k, v in states.items() if v != "failed" and k in unjudged)),
                       skipped=str(sum(1 for v in states.values() if v == "known")))
    for case, st in sorted(states.items()):
        f = _file(case)
        tc = ET.SubElement(suite, "testcase", classname=f.replace("/", ".").removesuffix(".py") if f else "assay",
                           name=case.split("::", 1)[1] if f else case)
        grouped: Dict[str, List[str]] = {}
        for k, v in fails.items():
            if k[0] == case:
                grouped.setdefault(_reason(v), []).append(_label(k[1]))
        why = [f"{', '.join(labels)}: {r}" for r, labels in grouped.items()]
        why += [f"Behavior: {ch['text']}" for b in result.get("behavior") or [] if b["case_id"] == case
                for ch in b["changes"]]
        if st == "failed":
            ET.SubElement(tc, "failure", message=(why or ["failed"])[0][:500]).text = "\n".join(why)
        elif case in unjudged:  # JUnit's "couldn't run", not a failure
            ET.SubElement(tc, "error", message=unjudged[case][0][:500]).text = "\n".join(unjudged[case])
        elif st == "known":
            ET.SubElement(tc, "skipped", message=("flaky: passes some attempts, as before" if case in flaky else
                                                  "failing in the baseline too") + (f": {why[0]}"[:500] if why else ""))
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)


CATEGORIES = [  # (name, which checks): the first that matches a check's field takes it
    ("Tool selection", lambda f: f == "tool_calls" or f.startswith(("expect.must_call", "expect.must_not_call"))),
    ("Security", lambda f: f in ("safety", "pii", "injection") or f.startswith("expect.must_get_approval")),
    ("Completion", lambda f: f in ("completed", "efficiency")
     or f.startswith(("expect.must_resolve", "expect.max_steps", "expect.must_answer"))),
    ("Planning", lambda f: f in ("plan", "plan_quality")),
    ("Reasoning", lambda f: f == "consistency"),
    ("Behavior", lambda f: f.startswith(("expect.max_cost", "expect.max_latency", "expect.max_tools",
                                         "expect.max_context"))),
    ("Output quality", lambda f: True),  # the answer, the end state, your asserts, your own fields
]
BUCKETS = [("regressed", "✗", "red"), ("new failure", "✗", "red"), ("couldn't be judged", "?", "yellow"),
           ("flaky", "⚠", "yellow"), ("known failure", "·", "dim"), ("passed", "✓", "green")]


def summarize(result: dict, c: dict, baseline: Optional[str]) -> dict:
    """The one-glance view: each case in one bucket, cases that improved, and each category."""
    att, base = result["attempts"], result["base_attempts"]
    cases = {case for case, _ in att} | {x["case_id"] for x in result["not_judged"]}
    fails_behavior = result.get("behavior_fails", True)
    worse_behavior = {b["case_id"] for b in result.get("behavior") or []}
    regressed = {p["case_id"] for p in c["problems"] if p["kind"] == "regression"} | \
        (worse_behavior if fails_behavior else set())
    new = {p["case_id"] for p in c["problems"] if p["kind"] != "regression"} - regressed
    unjudged = {x["case_id"] for x in result["not_judged"]} - regressed - new
    flaky = {p["case_id"] for p in c["flaky"]} - regressed - new - unjudged
    known = {p["case_id"] for p in c["still"]} - regressed - new - unjudged - flaky
    buckets = {"regressed": regressed, "new failure": new, "couldn't be judged": unjudged, "flaky": flaky,
               "known failure": known}
    buckets["passed"] = cases - set().union(*buckets.values())
    by_case = defaultdict(dict)
    for (case, field), a in att.items():
        by_case[case][field] = all(a)
    improved = sorted(case for case, fs in by_case.items() if all(fs.values()) and any(
        not all(base[(case, f)]) for f in fs if (case, f) in base))
    cats = {}
    for name, match in CATEGORIES:
        mine = {case: all(ok for f, ok in fs.items() if next(n for n, m in CATEGORIES if m(f)) == name)
                for case, fs in by_case.items() if any(next(n for n, m in CATEGORIES if m(f)) == name for f in fs)}
        if name == "Behavior":  # and how each case behaved against its baseline
            for case in result.get("behavior_compared") or []:
                mine[case] = mine.get(case, True) and case not in worse_behavior
        if mine:
            cats[name] = (sum(mine.values()), len(mine))
    return {"cases": len(cases), "buckets": {k: sorted(v) for k, v in buckets.items()}, "improved": improved,
            "categories": cats}


def summary_block(s: dict) -> List[str]:
    out = []
    for name, mark, color in BUCKETS:
        n = len(s["buckets"][name])
        if n or name == "passed":
            label = name if n == 1 or name in ("passed", "flaky", "regressed", "couldn't be judged") else name + "s"
            out.append(_paint(mark, color) + f" {n} {label}")
    if s["improved"]:
        out.append(_paint("↑", "green") + f" {len(s['improved'])} improved "
                   + _paint("(failing in their baseline, passing now)", "dim"))
    out.append("")
    if s["categories"]:
        width = max(len(k) for k in s["categories"])
        for name, (ok, n) in s["categories"].items():
            out.append(f"{name:<{width}}  {ok}/{n}")
        out.append("")
    return out


MARKER = "<!-- assay-regression -->"  # finds the PR comment to update (assay/github.py)
HEADLINES = {0: "No AI regression", 1: "AI regression detected",
             3: "Inconclusive: some results couldn't be judged"}


def _short(case: str) -> str:
    return case.split("::", 1)[1] if "::" in case else case


# Text in a PR comment comes from the run: test names, assertion messages, field names, all
# under the PR author's control. It goes in as text, never as Markdown or HTML: no @-mentions
# (they'd notify people), no links or images, no raw HTML, nothing that ends a code span.
_MD_SPECIAL = re.compile(r"([\\`*_\[\]~|#])")
MAX_COMMENT = 60_000  # GitHub's limit is 65,536 characters


def _md(text) -> str:
    t = re.sub(r"\s+", " ", str(text)).strip()
    t = t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return _MD_SPECIAL.sub(r"\\\1", t).replace("@", "@\u200b")


def _code(text, n: int = 120) -> str:
    """Text inside a code span: nothing in it can close the span."""
    t = re.sub(r"\s+", " ", str(text)).strip().replace("`", "'")
    return "`" + (t[:n] + "…" if len(t) > n else t) + "`"


# What a reviewer should read first: safety, then what the agent decided, then what it did, then cost.
RANK = ("Safety", "Prompt injection", "PII", "expect.must_get_approval", "Approval for", "Outcome", "expect.must_resolve", "Finished",
        "Tool usage", "Plan adherence", "Consistency", "Plan quality", "expect.must_call", "expect.must_not_call", "End state", "Answer", "Your asserts")


def _rank(line: str) -> int:
    return next((i for i, p in enumerate(RANK) if line.startswith(p)), len(RANK))


def _tidy(line: str) -> str:
    """One change as a reviewer reads it: no boilerplate, no trailing full stop."""
    label, _, why = line.partition(": ")
    why = re.sub(r"^(Unsafe action|Wrong tool|Wrong arguments|Looped|Stopped early|Wrong answer|Wrong end state|"
                 r"Tool error, not recovered|Ignored a tool result): ", "", why)
    return f"{label}: {why.rstrip('.')}" if why else line.rstrip(".")


def summary_markdown(run_id: str, result: dict, code: int, against: Optional[str]) -> str:
    """The run for a PR comment or a CI job summary: the verdict, the counts, and each change in a line."""
    s = result["summary"]
    b = s["buckets"]
    counts = [f"**{_n(s['cases'], 'case')}**", f"{len(b['passed'])} passed"]
    for name in ("regressed", "new failure", "flaky", "couldn't be judged", "known failure"):
        if b[name]:
            counts.append(f"{len(b[name])} {name}")
    if s["improved"]:
        counts.append(f"{len(s['improved'])} improved")
    changes = []
    shown = set(b["regressed"]) | set(b["new failure"])
    reasons: Dict[str, List[str]] = defaultdict(list)
    for (case, field), f in result["failing"].items():
        if case in shown:
            line = f"{_label(field)}: {_reason(f).splitlines()[0][:160]}"
            if line not in reasons[case]:
                reasons[case].append(line)
    for x in result.get("behavior") or []:
        reasons[x["case_id"]] += [ch["text"] for ch in x["changes"]]
    for case in sorted(reasons, key=lambda c: (min(_rank(x) for x in reasons[c]), c)):
        lines = sorted(dict.fromkeys(_tidy(x) for x in reasons[case]), key=_rank)
        changes.append(f"- {_code(_short(case))} → " + "; ".join(_md(x) for x in lines[:2])
                       + (f" (+{len(lines) - 2} more)" if len(lines) > 2 else ""))
    for f in result["fields"]:  # your own fields whose accuracy dropped, e.g. extraction
        if f["field"] not in CHECK_NAMES and not f["field"].startswith("expect.") and f["base_total"] and \
                f["passed"] / f["total"] < f["base_passed"] / f["base_total"]:
            changes.append(f"- {_code(f['label'])} accuracy {_pct(f['base_passed'], f['base_total'])} → "
                           f"{_pct(f['passed'], f['total'])}")
    out = [MARKER, f"## {HEADLINES.get(code, 'AI regression detected')}", "", " · ".join(counts), ""]
    if changes:
        out += [f"**{_n(len(changes), 'change')} in behavior**", "", *changes[:30], ""]
        if len(changes) > 30:
            out += [f"…and {len(changes) - 30} more", ""]
    if s["categories"]:
        out += ["| Category | Passed |", "|---|---|"] + [f"| {_md(k)} | {ok}/{n} |" for k, (ok, n) in s["categories"].items()]
        out.append("")
    nj = result["not_judged"]
    if nj:
        out += [f"<details><summary>{_n(len(nj), 'result')} couldn't be judged</summary>", ""]
        out += [f"- {_code(_short(x['case_id']))} {_md(_label(x['field'] or 'result'))}: "
                f"{verdicts.VERDICTS[x['verdict']]}, {_md(x['reason'])[:300]}" for x in nj[:20]]
        out += ["", "</details>", ""]
    out.append(f"<sub>{_md(against or 'no baseline yet')} · run {_code(run_id)} · "
               f"[Assay](https://github.com/tap222/docai-eval)</sub>")
    md = "\n".join(out) + "\n"
    if len(md) > MAX_COMMENT:  # cut whole lines, and say so
        md = md[:md.rfind("\n", 0, MAX_COMMENT - 200)] + "\n\n…the rest is in the job's summary.\n"
    return md


def report(run_id: str, baseline: Optional[str], result: dict, repeat: int, codes: List[int],
           against: Optional[str] = None) -> Tuple[str, bool]:
    passed, c = verdict(result, bool(baseline))
    st, fails, fields = result["stability"], result["failing"], result["fields"]
    cases = len({case for case, _ in result["attempts"]})
    out = [_paint("Assay test", "bold") + f"  {run_id}", "─" * 44]
    against = (against or f"compared with the baseline, {baseline}") if baseline else \
        "no baseline yet: every failing check counts"
    out += [f"{_n(cases, 'case')} · {_n(repeat, 'attempt')} each · {against}", ""]
    result["summary"] = summarize(result, c, baseline)
    out += summary_block(result["summary"])
    out += files_block(case_states(result, c))
    width = max((len(f["label"]) for f in fields), default=0)
    out.append(_paint("Checks", "bold"))
    for f in fields:
        mark = _paint("✓", "green") if f["passed"] == f["total"] else _paint("✗", "red")
        line = f"{mark} {f['label']:<{width}}  {f['passed']}/{f['total']}"
        if f["base_total"] and _pct(f["base_passed"], f["base_total"]) != _pct(f["passed"], f["total"]):
            line += _paint(f"   {_pct(f['base_passed'], f['base_total'])} → {_pct(f['passed'], f['total'])}", "dim")
        out.append(line)
    out.append("")
    problems = c["problems"]
    if problems:
        what = "regressed" if baseline and all(p["kind"] == "regression" for p in problems) else "failing"
        n_cases = len({p["case_id"] for p in problems})
        out.append(_paint(f"⚠ {_n(n_cases, 'case')} {what} ({_n(len(problems), 'check')})", "yellow"))
        for i, (title, ps) in enumerate(_groups(problems)[:20], 1):
            out.append(f"\n{i}. {title}")
            out += [f"   {line}" for line in _explain(ps, fails, repeat)]
        if len(_groups(problems)) > 20:
            out.append(f"\n… and {len(_groups(problems)) - 20} more")
        out.append("")
    worse = result.get("behavior") or []
    if worse:
        fails_ = result.get("behavior_fails", True)
        out.append(_paint(f"{'⚠' if fails_ else '~'} {_n(len(worse), 'case')} behaved worse than their baseline"
                          + ("" if fails_ else " (not failing: [behavior] fail = false)"), "yellow"))
        for b in worse[:20]:
            out.append(f"  {b['case_id']}")
            out += [_paint(f"    {ch['text']}", "dim") for ch in b["changes"]]
        out.append("")
    nj = result["not_judged"]
    if nj:
        out.append(_paint(f"? {_n(len(nj), 'result')} couldn't be judged (not counted)", "yellow"))
        for v in ("EVALUATOR_ERROR", "INFRA_ERROR", "MISSING"):
            items = [x for x in nj if x["verdict"] == v]
            if not items:
                continue
            out.append(f"  {verdicts.VERDICTS[v].capitalize()}: {len(items)}")
            for x in items[:5]:
                out.append(f"    {x['case_id']}  {_label(x['field'] or 'result')}" +
                           (f"  {x['evaluator']}" if x["evaluator"] else "") + _paint(f"  {x['reason']}", "dim"))
            if len(items) > 5:
                out.append(f"    … and {len(items) - 5} more")
        out.append("")
    if c["flaky"]:
        out.append(_paint(f"~ {_n(len(c['flaky']), 'flaky check')}: passing some attempts, as before; "
                          "not blocking", "yellow"))
        for p in c["flaky"][:10]:
            out.append(_paint(f"  {p['case_id']}  {_label(p['field'])}  "
                              f"{p['base_rate']:.0%} → {p['rate']:.0%}", "dim"))
        out.append("")
    if c["still"]:
        out.append(_paint(f"{_n(len(c['still']), 'check')} also failed in the baseline, so they don't count "
                          "against this change.", "dim"))
    if baseline and not problems and not passed:
        out.append(st["reasons"][0])
    if problems and baseline and repeat == 1:
        out.append(_paint("One attempt per case. If a case can vary between runs, `assay test --repeat 3` "
                          "tells flaky from broken.", "dim"))
    if not baseline and not passed:
        out.append(_paint("If these failures are known, make this run the baseline with `assay accept`: "
                          "later runs then fail only on what gets worse.", "dim"))
    if TIMED_OUT in codes and not result.get("left_open"):
        out.append(_paint(f"Your command timed out ({codes.count(TIMED_OUT)} of {len(codes)} attempts) after every "
                          "case had finished, and was stopped: it hung on the way out (a thread or event loop "
                          "that never stopped?). Nothing was lost.", "yellow"))
    elif TIMED_OUT in codes:
        out.append(_paint(f"Your command timed out ({codes.count(TIMED_OUT)} of {len(codes)} attempts) and was "
                          "stopped; what it recorded is above.", "yellow"))
    if any(x and x != TIMED_OUT for x in codes):
        out.append(_paint(f"Your command exited with {', '.join(str(x) for x in codes if x and x != TIMED_OUT)}.",
                          "yellow"))
    if passed and nj:
        out.append(_paint(f"Inconclusive: nothing got worse, but {_n(len(nj), 'result')} couldn't be judged. "
                          "Fix or rerun the evaluation; the baseline stays as it was.", "yellow"))
    else:
        out.append(_paint("Passed." if passed else "Failed.", "green" if passed else "red") +
                   (" Its cases' results are now their baseline." if passed else ""))
    return "\n".join(out), passed


SDK_MIN = (0, 2, 0)


def sdk_problem() -> Optional[str]:
    """Why the SDK here can't record for `assay test`, or None."""
    try:
        import assay_sdk
    except ImportError:
        return "The Assay SDK isn't installed here: pip install assay-evals"
    version = tuple(int(x) for x in re.findall(r"\d+", assay_sdk.__version__)[:3])
    if version < SDK_MIN:
        return (f"assay test needs assay-evals {'.'.join(map(str, SDK_MIN))} or newer (this is "
                f"{assay_sdk.__version__}): pip install -U assay-evals")
    return None


def new_run_id() -> str:
    return datetime.now().strftime("t-%Y%m%d-%H%M%S-%f")[:-3]


def test(root: Path, command: Optional[str], repeat: Optional[int], baseline: Optional[str],
         send: Optional[dict] = None, junit: Optional[str] = None, timeout: Optional[float] = None,
         failed: bool = False, judge: bool = False) -> int:
    """`assay test`. Prints the report; returns the exit code."""
    try:
        cfg = load_config(root)
    except SetupError as exc:
        print(exc, file=sys.stderr)
        return 2
    if judge:
        cfg["judge"] = {**cfg["judge"], "enabled": True}
    problem = sdk_problem()
    if problem:
        print(problem, file=sys.stderr)
        return 2
    command = command or cfg["command"]
    if not command:
        print(f"Give a command to run: set command under [test] in {CONFIG}, or `assay test -- <command>`.",
              file=sys.stderr)
        return 2
    repeat = repeat or cfg["repeat"]
    home = ensure_home(root)
    (home / "runs").mkdir(exist_ok=True)
    run_id = new_run_id()
    events = home / "runs" / f"{run_id}.jsonl"
    timeout = timeout or (float(os.environ["ASSAY_TIMEOUT"]) if os.environ.get("ASSAY_TIMEOUT") else None) \
        or cfg["timeout"]
    if failed:
        rerun = _state(home).get("rerun")
        if rerun is None:
            print("Nothing to rerun yet: run `assay test` first.", file=sys.stderr)
            return 2
        if not rerun:
            print("Nothing to rerun: every case passed last time.")
            return 0
        if "pytest" not in command:
            print("--failed reruns through the pytest plugin; this command isn't pytest, so it runs whole.",
                  file=sys.stderr)
    codes = run_command(command, events, run_id, repeat, timeout, failed)
    if not events.exists():
        print(f"\n`{command}` recorded nothing. Does it call assay.init() and record runs with "
              "assay.run(..., test=\"<case>\")?", file=sys.stderr)
        return 2
    why = f"the command timed out after {timeout:g}s" if TIMED_OUT in codes else None
    code, text = finish(root, cfg, run_id, repeat, codes, baseline, junit, why)
    print("\n" + text, file=sys.stderr if code == 2 else sys.stdout)
    if send is not None and code != 2:
        print()
        sent = upload(root, run_id, **send)
        code = code or sent  # a failed upload fails a run that passed; a failing run stays 1
    return code


def finish(root: Path, cfg: dict, run_id: str, repeat: int, codes: List[int], baseline: Optional[str],
           junit: Optional[str] = None, abandoned_why: Optional[str] = None) -> Tuple[int, str]:
    """Load a recorded test run, check it, compare it with the baseline, and move the baseline on
    if it passed. (exit code, report): 0 passed, 1 failed, 2 nothing to check, 3 inconclusive.
    Shared by `assay test` and `pytest --assay`."""
    home = ensure_home(root)
    events = home / "runs" / f"{run_id}.jsonl"
    engine = store.make_engine(f"sqlite:///{home / 'assay.db'}")
    state = _migrate(engine, home, _state(home))
    explicit = baseline not in (None, "none")
    if baseline == "none":
        baseline = None
    elif baseline is None:
        baseline = BASELINE if state.get("baseline_cases") else None
    sync_contracts(engine, cfg["contracts"])
    _, bad = load_file(engine, str(events), TENANT)
    if bad:
        return 2, "\n  ".join([f"{len(bad)} bad line(s) in {events}:", *bad[:20]])
    judged = None
    if (cfg.get("judge") or {}).get("enabled"):  # plan quality and consistency, by an LLM (assay/judge.py)
        from assay import judge
        judged = judge.judge_run(engine, TENANT, run_id, cfg["judge"]["model"], redact=cfg["judge"]["redact"])
    result = evaluate(engine, run_id, baseline, cfg["tolerance"], cfg["pii"], cfg["behavior"], abandoned_why)
    if result is None:
        return 2, ("Nothing to check: record runs with assay.run(..., test=\"<case>\"), and say what each case "
                   "should do with assay.expect(), or send results with assay.check().")
    ran = {case for case, _ in result["attempts"]}
    known = {c: r for c, r in (state.get("baseline_cases") or {}).items() if c in ran}
    if baseline == BASELINE and not known:
        baseline = None  # none of these cases has a baseline yet
        result = evaluate(engine, run_id, None, cfg["tolerance"], cfg["pii"], cfg["behavior"], abandoned_why)
    against = None if baseline is None else f"compared with the baseline, {baseline}" if explicit else \
        (f"compared with each case's last passing run ({len(known)} of {len(ran)} cases have one, from "
         f"{_n(len(set(known.values())), 'run')})")
    result["behavior_fails"] = cfg["behavior"]["fail"]
    text, passed = report(run_id, baseline, result, repeat, codes, against)
    if judged is not None:
        text += _paint(f"\nJudged {_n(judged['judged'], 'run')} with {cfg['judge']['model']} (plan quality, "
                       f"consistency)" + (f"; {judged['errors']} result(s) couldn't be judged" if judged["errors"]
                                          else "") + ".", "dim")
    if junit:
        write_junit(junit, run_id, result, verdict(result, bool(baseline))[1])
    state["last"] = run_id
    inconclusive = passed and bool(result["not_judged"])
    if passed and not inconclusive:
        state["baseline_cases"] = {**(state.get("baseline_cases") or {}), **{c: run_id for c in promote(engine, run_id)}}
    code = 1 if not passed else 3 if inconclusive else 0
    # What's left to rerun (`--failed`): everything that didn't simply pass.
    state["rerun"] = sorted(set().union(*(v for k, v in result["summary"]["buckets"].items() if k != "passed")))
    _save_state(home, state)
    md = summary_markdown(run_id, result, code, against)
    (home / "summary.md").write_text(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):  # GitHub Actions: the job's summary page
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md + "\n")
    return code, text


def _migrate(engine, home: Path, state: dict) -> dict:
    """A whole-run baseline (before per-case baselines) becomes each of its cases' baseline."""
    if state.get("baseline") and "baseline_cases" not in state:
        run = state.pop("baseline")
        state["baseline_cases"] = {c: run for c in promote(engine, run)}
        _save_state(home, state)
    return state


def accept(root: Path, run_id: Optional[str]) -> int:
    """`assay accept`: make a run (the latest, by default) the baseline of each of its cases,
    failures and all."""
    home = root / HOME
    if not (home / "assay.db").exists():
        print("No test run yet. Run `assay test` first.", file=sys.stderr)
        return 2
    engine = store.make_engine(f"sqlite:///{home / 'assay.db'}")
    state = _migrate(engine, home, _state(home))
    run_id = run_id or state.get("last")
    if not run_id:
        print("No test run yet. Run `assay test` first.", file=sys.stderr)
        return 2
    if not (home / "runs" / f"{run_id}.jsonl").exists():
        print(f"No run {run_id} in {home / 'runs'}.", file=sys.stderr)
        return 2
    cases = promote(engine, run_id)
    state["baseline_cases"] = {**(state.get("baseline_cases") or {}), **{c: run_id for c in cases}}
    _save_state(home, state)
    print(f"{run_id} is now the baseline of its {_n(len(cases), 'case')}. `assay test` fails only on what "
          "gets worse than it.")
    return 0


# ---------- sending a run to a server ----------

def _http(method: str, url: str, body: Optional[dict], headers: Dict[str, str]) -> Tuple[int, object]:
    import urllib.error
    import urllib.request
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **headers}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def upload(root: Path, run_id: Optional[str], url: Optional[str], key: Optional[str], tenant: Optional[str],
           http=_http) -> int:
    """Send a test run's recording to an Assay server, then have it check the agent runs there.
    Sending again is safe: every event has an id."""
    home = root / HOME
    run_id = run_id or _state(home).get("last")
    url = (url or os.environ.get("ASSAY_URL") or "").rstrip("/")
    key = key or os.environ.get("ASSAY_KEY")
    if not url:
        print("Where to? Set ASSAY_URL (and ASSAY_KEY), or pass --url.", file=sys.stderr)
        return 2
    if not run_id:
        print("No test run yet. Run `assay test` first.", file=sys.stderr)
        return 2
    path = home / "runs" / f"{run_id}.jsonl"
    if not path.exists():
        print(f"No recording for run {run_id} in {home / 'runs'}.", file=sys.stderr)
        return 2
    events = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    if tenant:
        headers["X-Tenant"] = tenant
    try:
        for i in range(0, len(events), 1000):
            code, body = http("POST", f"{url}/v1/ingest", {"events": events[i:i + 1000]}, headers)
            if code != 200:
                print(f"{url} refused the upload ({code}): {body}", file=sys.stderr)
                return 2
        code, me = http("GET", f"{url}/v1/whoami", None, headers)
    except OSError as exc:
        print(f"Couldn't reach {url}: {exc}", file=sys.stderr)
        return 2
    if not tenant:
        tenant = me.get("tenant") if code == 200 and isinstance(me, dict) else None
        tenant = "default" if tenant in (None, "*") else tenant
    source = f"events:{tenant}"
    print(f"Sent run {run_id} ({len(events)} events) to {url}, tenant '{tenant}'.")
    if any(e.get("type") == "run.start" and e.get("test") for e in events):
        code, _ = http("POST", f"{url}/v1/agents/runs/{run_id}/evaluate?source={source}", None, headers)
        if code == 403:
            print("It isn't checked there yet: that needs a key with the manage scope. The dashboard can "
                  "check it too.")
        elif code != 200:
            print(f"The server couldn't check it ({code}).", file=sys.stderr)
    print(f"See it in the dashboard at {url}: source {source}, run {run_id}.")
    return 0


def split_command(argv: List[str]) -> Optional[str]:
    """`assay test -- pytest -q tests` → "pytest -q tests"."""
    if argv and argv[0] == "--":
        argv = argv[1:]
    return shlex.join(argv) if argv else None
