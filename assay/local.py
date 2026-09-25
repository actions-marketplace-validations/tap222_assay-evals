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
from typing import Dict, List, Optional, Tuple

from sqlalchemy import delete, select

from assay import agents, audit, contracts, failures, ingest, learn, lifecycle, schema, store
from assay.sources.events import EventsSource

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

CONFIG = "assay.toml"
HOME = ".assay"
TENANT = "local"
EXAMPLE = "assay_example.py"
CHECK_NAMES = {"completed": "Finished", "answer": "Answer", "tool_calls": "Tool usage", "end_state": "End state", "safety": "Safety",
               "pii": "PII", "efficiency": "Efficiency", "pytest": "Your asserts"}
PII_EVALUATOR = "assay.pii@1"

CONFIG_TEMPLATE = '''\
# Assay: `assay test` runs the command below with the SDK recording, checks every run,
# and compares the results with the last run that passed.
# Docs: https://github.com/tap222/docai-eval/tree/main/sdk/python#readme

[test]
command = "python {example}"   # your tests, e.g. "pytest -q tests/ai" with the assay_case fixture
repeat = 1        # attempts per case; 3 or more lets Assay tell a flaky case from a broken one
tolerance = 0.01  # a drop in the pass rate smaller than this doesn't fail the run

# Safety rules every agent run must keep. Kinds: never, must_include, before, only_after,
# max_runs, allowed_steps. `where` narrows a rule to calls with certain arguments.
[[contracts]]
kind = "never"
step = "delete_order"

[[contracts]]
kind = "only_after"
step = "refund"
other = "get_order"

# Personal data (email, card, IBAN, SSN, phone) in a tool's arguments fails the PII check,
# unless the tool is allowed that kind, e.g. allow = {{ send_receipt = ["email"] }}.
[pii]
check = true
allow = {{}}
'''

EXAMPLE_TEMPLATE = '''\
"""An example for `assay test`: a small support agent that needs no LLM. Replace it with yours.

Each case runs inside assay.run(..., test="<case>"), and assay.expect() says what the case
should do. `assay test` runs this file, records every step, and checks each run."""
import assay_sdk as assay

ORDERS = {"O-17": {"price": 27.61, "status": "delivered"}, "O-18": {"price": 12.00, "status": "shipped"}}


def get_order(order_id):
    return ORDERS[order_id]


def refund(order_id, amount):
    return {"refunded": amount}


def agent(run, message, order_id):
    """Your agent goes here. This one looks the order up and refunds it if it was delivered."""
    order = run.call("get_order", get_order, order_id=order_id)
    if order["status"] != "delivered":
        run.answer(f"Order {order_id} hasn't arrived yet, so it can't be refunded.")
        return
    run.call("refund", refund, order_id=order_id, amount=order["price"])
    run.answer(f"Refunded ${order['price']:.2f}.")


CASES = {
    "refund_delivered": ("Refund order O-17 please", "O-17", dict(
        calls=[{"tool": "get_order", "args": {"order_id": "O-17"}},
               {"tool": "refund", "args": {"order_id": "O-17"}}],
        answer="27.61", max_steps=4)),
    "refund_not_delivered": ("Can I get a refund for O-18?", "O-18", dict(
        calls=[{"tool": "get_order", "args": {"order_id": "O-18"}}],
        answer="hasn't arrived", max_steps=3)),
}

assay.init()  # no server: records locally
for case, (message, order_id, expected) in CASES.items():
    assay.expect(case, **expected)
    with assay.run("refund_request", input=message, test=case) as run:
        agent(run, message, order_id)
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
    for tool, allowed in allow.items():
        if not isinstance(allowed, list) or set(allowed) - kinds:
            raise SetupError(f"{CONFIG}, [pii] allow.{tool}: a list of kinds from {', '.join(learn.PII)}.")
    return {"command": test.get("command"), "repeat": int(test.get("repeat", 1)),
            "tolerance": float(test.get("tolerance", 0.01)), "contracts": rules,
            "pii": {"check": bool(pii.get("check", True)), "allow": {k: set(v) for k, v in allow.items()}}}


class SetupError(Exception):
    pass


def init(root: Path) -> List[str]:
    """Write assay.toml and the example, leaving anything that already exists alone."""
    ensure_home(root)
    made = []
    if not (root / EXAMPLE).exists():
        (root / EXAMPLE).write_text(EXAMPLE_TEMPLATE)
        made.append(EXAMPLE)
    if not (root / CONFIG).exists():
        (root / CONFIG).write_text(CONFIG_TEMPLATE.format(example=EXAMPLE))
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

def run_command(command: str, events: Path, run_id: str, repeat: int) -> List[int]:
    """Run the command once per attempt, with the SDK recording to `events`. Returns the exit codes."""
    env = {k: v for k, v in os.environ.items() if k != "ASSAY_URL"}  # record locally, never to a server
    env.update(ASSAY_PATH=str(events), ASSAY_TEST_RUN=run_id)
    codes = []
    for attempt in range(repeat):
        env["ASSAY_TEST_ATTEMPT"] = str(attempt)
        codes.append(subprocess.run(command, shell=True, env=env).returncode)
    return codes


def sync_contracts(engine, rules: List[dict]) -> None:
    """The local source's contracts are exactly those in assay.toml."""
    source = f"events:{TENANT}"
    with engine.begin() as conn:
        conn.execute(delete(store.path_contracts).where(store.path_contracts.c.source == source))
    for c in rules:
        contracts.save(engine, source, c)


def pii_findings(traj: dict, allow: Dict[str, set]) -> List[str]:
    """Personal data in the arguments of the run's tool calls, except kinds the tool may receive."""
    out = []
    for s in traj["steps"]:
        if s["kind"] != "tool" or not s.get("args"):
            continue
        for hit in learn.pii_scan(s["args"]):
            if hit["kind"] not in allow.get(s["name"], ()):
                out.append(f"{hit['kind']} ({hit['sample']}) sent to {s['name']} (step {s['seq']})")
    return out


def check_pii(engine, source, run_id: str, allow: Dict[str, set]) -> None:
    """One PII result per agent run, stored like the trajectory checks."""
    heads = agents.run_trajectories(engine, TENANT, run_id)
    trajs = source.trajectories([h["trajectory_id"] for h in heads])
    rows = []
    for h in heads:
        traj = trajs.get(h["trajectory_id"])
        if traj is None:
            continue
        found = pii_findings(traj, allow)
        case = h["case_id"] or h["trajectory_id"]
        rows.append({"tenant": TENANT, "result_id": ingest._derive(run_id, case, "pii", PII_EVALUATOR, h["attempt"]),
                     "run_id": run_id, "case_id": case, "document_id": h["trajectory_id"],
                     "evaluator": PII_EVALUATOR, "attempt": h["attempt"], "ts": h["started_at"],
                     "lineage": h["lineage"], "score": None, "field": "pii",
                     "status": "fail" if found else "pass", "expected": "no personal data in tool arguments",
                     "actual": "; ".join(found)[:300] or "none",
                     "reason": f"Personal data in tool arguments: {'; '.join(found)}"[:2000] if found else None})
    ingest.upsert(engine, store.eval_results, rows, "result_id")


def evaluate(engine, run_id: str, baseline: Optional[str], tolerance: float,
             pii: Optional[dict] = None) -> Optional[dict]:
    """Check the run and compare it with the baseline. None if the run recorded nothing to check."""
    source = EventsSource(engine, TENANT)
    heads = agents.run_trajectories(engine, TENANT, run_id)
    if heads:
        # The command has exited: a run it left open will never end. Say so, instead of skipping it.
        lifecycle.abandon(engine, tenant=TENANT, ids=[h["trajectory_id"] for h in heads if h["status"] == "running"])
        lifecycle.evaluate(engine, lifecycle.pending(engine, TENANT, [h["trajectory_id"] for h in heads]),
                           abandoned_why="the command exited first")
        if pii and pii["check"]:
            check_pii(engine, source, run_id, pii["allow"])
    # "" means no baseline: failures.evaluation would otherwise pick the run before this one.
    a = failures.evaluation(engine, source, TENANT, run_id, baseline or "", tolerance)
    if a is None:
        return None
    # Results whose evaluator was given the wrong data (assay/audit.py) say nothing about the AI:
    # they're listed on their own and left out of every count below.
    rows, base_rows = _rows(engine, run_id), _rows(engine, baseline) if baseline else []
    found = audit.audit_rows(engine, TENANT, rows)
    rows = [r for r in rows if r.result_id not in found]
    base_rows = [r for r in base_rows if r.result_id not in audit.audit_rows(engine, TENANT, base_rows)]
    by_id = {r.result_id: r for r in _rows(engine, run_id)}
    return {"stability": a["stability"], "fields": field_rates(rows, base_rows), "failing": failing(rows),
            "attempts": attempts(rows), "base_attempts": attempts(base_rows),
            "suspect": [{"case_id": by_id[rid].case_id, "field": by_id[rid].field or "result",
                         "evaluator": by_id[rid].evaluator, "status": by_id[rid].status, "findings": fs}
                        for rid, fs in found.items()]}


def _rows(engine, run_id: str) -> list:
    t = store.eval_results
    with engine.connect() as conn:
        return conn.execute(select(t).where((t.c.tenant == TENANT) & (t.c.run_id == run_id))).all()


def attempts(rows: list) -> Dict[Tuple[str, str], List[bool]]:
    """Per (case, field): whether each attempt passed."""
    out = defaultdict(list)
    for r in rows:
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
        if r.status != "pass" and key not in out:
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
    return not c["problems"] and not dropped, c


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


def report(run_id: str, baseline: Optional[str], result: dict, repeat: int, codes: List[int]) -> Tuple[str, bool]:
    passed, c = verdict(result, bool(baseline))
    st, fails, fields = result["stability"], result["failing"], result["fields"]
    cases = len({case for case, _ in result["attempts"]})
    out = [_paint("Assay test", "bold") + f"  {run_id}", "─" * 44]
    against = f"compared with the baseline, {baseline}" if baseline else \
        "no baseline yet: every failing check counts"
    out += [f"{_n(cases, 'case')} · {_n(repeat, 'attempt')} each · {against}", ""]
    width = max((len(f["label"]) for f in fields), default=0)
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
    if result["suspect"]:
        n = len(result["suspect"])
        out.append(_paint(f"? {_n(n, 'result')} judged on data that doesn't match the trace (not counted)", "yellow"))
        for s_ in result["suspect"][:10]:
            out.append(f"  {s_['case_id']}  {_label(s_['field'])}" +
                       (f"  {s_['evaluator']}" if s_["evaluator"] else "") + f"  ({s_['status']})")
            out += [_paint(f"    {f}", "dim") for f in s_["findings"][:2]]
        if n > 10:
            out.append(f"  … and {n - 10} more")
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
    if any(codes):
        out.append(_paint(f"Your command exited with {', '.join(str(x) for x in codes if x)}.", "yellow"))
    out.append(_paint("Passed." if passed else "Failed.", "green" if passed else "red") +
               (" This run is now the baseline." if passed else ""))
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


def test(root: Path, command: Optional[str], repeat: Optional[int], baseline: Optional[str],
         send: Optional[dict] = None) -> int:
    """`assay test`. Prints the report; returns the exit code."""
    try:
        cfg = load_config(root)
    except SetupError as exc:
        print(exc, file=sys.stderr)
        return 2
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
    state = _state(home)
    if baseline is None:
        baseline = state.get("baseline")
    elif baseline == "none":
        baseline = None
    engine = store.make_engine(f"sqlite:///{home / 'assay.db'}")
    sync_contracts(engine, cfg["contracts"])

    run_id = datetime.now().strftime("t-%Y%m%d-%H%M%S-%f")[:-3]
    events = home / "runs" / f"{run_id}.jsonl"
    codes = run_command(command, events, run_id, repeat)
    if not events.exists():
        print(f"\n`{command}` recorded nothing. Does it call assay.init() and record runs with "
              "assay.run(..., test=\"<case>\")?", file=sys.stderr)
        return 2
    _, bad = load_file(engine, str(events), TENANT)
    if bad:
        print(f"{len(bad)} bad line(s) in {events}:", *bad[:20], sep="\n  ", file=sys.stderr)
        return 2
    result = evaluate(engine, run_id, baseline, cfg["tolerance"], cfg["pii"])
    if result is None:
        print("\nNothing to check: record runs with assay.run(..., test=\"<case>\"), and say what each case "
              "should do with assay.expect(), or send results with assay.check().", file=sys.stderr)
        return 2
    text, passed = report(run_id, baseline, result, repeat, codes)
    print("\n" + text)
    _save_state(home, {**state, "last": run_id, **({"baseline": run_id} if passed else {})})
    code = 0 if passed else 1
    if send is not None:
        print()
        sent = upload(root, run_id, **send)
        code = code or sent  # a failed upload fails a run that passed; a failing run stays 1
    return code


def accept(root: Path, run_id: Optional[str]) -> int:
    """`assay accept`: make a run (the latest, by default) the baseline, failures and all."""
    home = root / HOME
    state = _state(home)
    run_id = run_id or state.get("last")
    if not run_id:
        print("No test run yet. Run `assay test` first.", file=sys.stderr)
        return 2
    if not (home / "runs" / f"{run_id}.jsonl").exists():
        print(f"No run {run_id} in {home / 'runs'}.", file=sys.stderr)
        return 2
    _save_state(home, {**state, "baseline": run_id})
    print(f"{run_id} is now the baseline. `assay test` fails only on what gets worse than it.")
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
