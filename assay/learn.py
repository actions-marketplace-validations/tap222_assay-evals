"""Learning from production: anomalous traces → patterns → candidate tests → a permanent suite.

Most production failures are never reported. This scores every trace from
signals that need no label, each shown with its reason:

  contract       a path contract broke (critical ones weigh most)
  reported       someone reported a wrong value on it
  feedback       a user gave a thumbs down, complained, escalated or retried
  failed_step    a step failed; tool_error: an agent's tool errored, never recovered
  loop           the same call with the same arguments three or more times
  fallback       a fallback model answered
  outlier_*      far more steps, time or cost than other traces of the same task
                 (robust z-score against that task's median, so long tasks aren't
                 flagged for being long)
  rare_path      a path under 1% of that task's traces
  stuck          never finished

A trace is anomalous when its signals add up past a threshold. Anomalous
traces are clustered into patterns by their strongest signal, where it
happened and the task; each pattern is described by what sets its traces
apart from normal ones (the same contrast as failure causes), when it
started, and whether it came in a burst. Infrastructure patterns (a tool or
step that was down) are listed but not turned into tests: a test can't
replay an outage.

For a pattern, a few representative traces become candidate test cases
(the most typical one, then the most different, never near-duplicates).
Each expectation says where it came from:

  correction      a person reported the right value: reliable
  property        what the failure broke, as a rule that must hold whatever
                  the right answer is (the contract; no repeated calls; a step
                  budget): reliable
  passing_traces  what normal traces of the same task did (their tool calls and
                  step count): a guess, marked so

A developer edits and approves or rejects each one. Approved cases go into
a named suite, linked to the pattern and trace they came from; for agents,
the reference is stored so evaluation runs check it. Inputs are scanned for
personal data and redacted on approval unless told otherwise.

Each pattern moves through open → protected (a test guards it) → fixed (no
longer seen in production) → recurred (seen again after it was fixed).
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from statistics import median
from typing import Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import contracts as contracts_mod
from assay import store
from assay.cost import is_fallback
from assay.models import FAILED_STATUSES, Window
from assay.rootcause import order_steps

THRESHOLD = 2.0  # signal weight that makes a trace anomalous
WEIGHTS = {"contract_critical": 3.0, "contract_warning": 1.0, "reported": 3.0, "feedback": 3.0, "retry": 1.5,
           "failed_step": 2.0, "tool_error": 2.0, "loop": 2.0, "fallback": 1.0, "outlier": 1.0,
           "rare_path": 1.0, "stuck": 1.5}
PRIORITY = ["contract", "reported", "tool_error", "loop", "failed_step", "feedback", "fallback", "stuck",
            "outlier_steps", "outlier_seconds", "outlier_cost", "rare_path"]
INFRA = re.compile(r"time ?out|timed out|connection|refused|unavailable|unreachable|\b5\d\d\b|rate.?limit|"
                   r"quota|throttl", re.I)
MAX_TRACES = 8000
REPRESENTATIVES = 3
Z = 3.5


# ---------- personal data ----------

# Most specific first: redaction applies them in order, and a card number also looks like a phone number.
PII = {
    "email": re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    "card": re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)"),
    "iban": re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"),
    "ssn": re.compile(r"\b\d{3}-\d{2}-\d{4}\b"),
    "phone": re.compile(r"(?<!\w)\+?\d[\d ().-]{7,}\d(?!\w)"),
}


def _luhn(digits: str) -> bool:
    d = [int(c) for c in digits if c.isdigit()]
    # From the right, double every second digit; subtract 9 when that goes over 9.
    s = sum(x if i % 2 == 0 else (x * 2 - 9 if x * 2 > 9 else x * 2) for i, x in enumerate(reversed(d)))
    return len(d) >= 13 and s % 10 == 0


def pii_scan(value) -> List[dict]:
    """Personal data an input seems to hold: [{"kind", "sample"}], sample partly masked."""
    text = value if isinstance(value, str) else json.dumps(value, default=str) if value is not None else ""
    out, taken = [], []
    for kind, rx in PII.items():
        for m in rx.finditer(text):
            v = m.group(0)
            if any(m.start() < b and a < m.end() for a, b in taken):
                continue  # already found as something more specific
            if kind == "card" and not _luhn(v):
                continue
            if kind == "phone" and len(re.sub(r"\D", "", v)) < 9:
                continue
            taken.append((m.start(), m.end()))
            out.append({"kind": kind, "sample": v[:3] + "…" + v[-2:] if len(v) > 6 else "…"})
    seen, uniq = set(), []
    for x in out:
        if (x["kind"], x["sample"]) not in seen:
            seen.add((x["kind"], x["sample"]))
            uniq.append(x)
    return uniq[:20]


def redact(value):
    """The input with personal data replaced by placeholders, keeping its shape."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    out = value
    for kind, rx in PII.items():
        out = rx.sub(lambda m: m.group(0) if (kind == "card" and not _luhn(m.group(0))) or
                     (kind == "phone" and len(re.sub(r"\D", "", m.group(0))) < 9) else f"<{kind}>", out)
    return out


# ---------- scoring traces ----------

def _robust(values: List[float]):
    if len(values) < 20:
        return None
    m = median(values)
    mad = median(abs(v - m) for v in values)
    return m, max(1.4826 * mad, 0.1 * abs(m), 1e-9)


def _path_key(runs) -> tuple:
    out, prev = [], None
    for r in runs:
        if r.stage != prev:
            out.append(r.stage)
        prev = r.stage
    return tuple(out)


def score(source, window: Window, engine: Engine, threshold: float = THRESHOLD) -> dict:
    """Every trace in the window, scored from label-free signals; anomalous ones listed."""
    docs = list(source.documents(window) or [])[-MAX_TRACES:]
    ids = [d.document_id for d in docs]
    details = source.document_details(ids) if hasattr(source, "document_details") else \
        {i: d for i in ids if (d := source.document_detail(i))}
    trajs = source.trajectories(ids) if hasattr(source, "trajectories") else {}
    # Evaluation runs aren't production: leave test-case trajectories out.
    tests = {d for d, tr in trajs.items() if tr.get("run_id")}
    docs = [d for d in docs if d.document_id not in tests]
    trajs = {d: tr for d, tr in trajs.items() if d not in tests}
    rules = contracts_mod.load(engine, source.name)
    tenant = source.name.split(":", 1)[1] if source.name.startswith("events:") else source.name
    errors = defaultdict(list)
    for e in (source.errors(window) if hasattr(source, "errors") else None) or []:
        errors[e.document_id].append(e)
    fb = defaultdict(list)
    t = store.trace_feedback
    with engine.connect() as conn:
        for r in conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.ts >= window.start,
                                                   t.c.ts < window.end + timedelta(days=2)))):
            fb[r.trace_id].append(r.kind)

    # Per task: typical steps, time and cost, and how common each path is.
    facts, by_task = {}, defaultdict(list)
    for d in docs:
        det = details.get(d.document_id)
        if det is None:
            continue
        runs = order_steps(det[1])
        cost = sum(c.cost_usd or 0 for c in det[2])
        secs = (d.completed_at - d.received_at).total_seconds() if d.completed_at else None
        task = d.document_type or "(no task)"
        tr = trajs.get(d.document_id)
        facts[d.document_id] = {"task": task, "runs": runs, "calls": det[2], "doc": d,
                                "steps": len(tr["steps"]) if tr is not None else len(runs),
                                "seconds": secs, "cost": cost, "path": _path_key(runs)}
        by_task[task].append(d.document_id)
    stats = {}
    for task, members in by_task.items():
        stats[task] = {k: _robust([facts[m][k] for m in members if facts[m][k] is not None])
                       for k in ("steps", "seconds", "cost")}
        stats[task]["paths"] = Counter(facts[m]["path"] for m in members)
        stats[task]["n"] = len(members)

    scored = []
    for d_id, f in facts.items():
        sig = []
        path = [contracts_mod.Step(r.stage, (r.outputs or {}).get("_args")) for r in f["runs"]]
        for c in rules:
            if not contracts_mod.applies(c, f["doc"]):
                continue
            b = contracts_mod.breaks(c, path, finished=bool(f["doc"].completed_at))
            if b:
                crit = c.get("severity", "critical") == "critical"
                sig.append({"type": "contract", "weight": WEIGHTS["contract_critical" if crit else "contract_warning"],
                            "stage": b["stage"], "key": str(c.get("id")), "text": f"Broke “{c.get('label')}”: "
                                                                                   f"{b['detail']}"})
        for e in errors.get(d_id, [])[:3]:
            sig.append({"type": "reported", "weight": WEIGHTS["reported"], "stage": None, "key": e.field,
                        "text": f"Reported: {e.field} should be {e.expected!r}, was {e.observed!r}",
                        "field": e.field, "expected": e.expected, "observed": e.observed})
        kinds = Counter(fb.get(d_id, []))
        bad = {k: n for k, n in kinds.items() if k in ("thumbs_down", "complaint", "escalation")}
        if bad:
            sig.append({"type": "feedback", "weight": WEIGHTS["feedback"], "stage": None, "key": ",".join(sorted(bad)),
                        "text": "Users: " + ", ".join(f"{k.replace('_', ' ')}" + (f" ×{n}" if n > 1 else "")
                                                       for k, n in bad.items())})
        elif kinds.get("retry"):
            sig.append({"type": "feedback", "weight": WEIGHTS["retry"], "stage": None, "key": "retry",
                        "text": f"The user retried ({kinds['retry']}×)"})
        traj = trajs.get(d_id)
        if traj is not None:
            calls = [s for s in traj["steps"] if s["kind"] == "tool"]
            for i, c in enumerate(calls):
                if c["error"] and not any(x["name"] == c["name"] and not x["error"] for x in calls[i + 1:]):
                    sig.append({"type": "tool_error", "weight": WEIGHTS["tool_error"], "stage": c["name"],
                                "infra": bool(INFRA.search(c["error"])), "key": c["name"],
                                "text": f"{c['name']} failed ({c['error']}) and the agent went on without it"})
                    break
            same = Counter((c["name"], json.dumps(c["args"] or {}, sort_keys=True, default=str)) for c in calls)
            (name, args), n = same.most_common(1)[0] if same else ((None, None), 0)
            if n >= 3:
                sig.append({"type": "loop", "weight": WEIGHTS["loop"], "stage": name, "key": name,
                            "text": f"Called {name} {n} times with identical arguments"})
        else:
            failed = [r for r in f["runs"] if r.status in FAILED_STATUSES]
            if failed:
                r = failed[0]
                sig.append({"type": "failed_step", "weight": WEIGHTS["failed_step"], "stage": r.stage,
                            "infra": r.status == "timeout", "key": r.stage, "text": f"{r.stage} {r.status}"})
        fallback = [c for c in f["calls"] if is_fallback(c)]
        if fallback:
            c = fallback[0]
            sig.append({"type": "fallback", "weight": WEIGHTS["fallback"], "stage": c.stage, "key": c.stage,
                        "infra": bool(INFRA.search(c.gate_reason or "")),
                        "text": f"A fallback answered at {c.stage} ({c.model_served}"
                                + (f", {c.gate_reason})" if c.gate_reason else ")")})
        st = stats[f["task"]]
        for k, label in (("steps", "steps"), ("seconds", "time"), ("cost", "cost")):
            rb, v = st[k], f[k]
            if rb and v is not None and (v - rb[0]) / rb[1] >= Z and v >= 1.5 * rb[0] and v > 0:
                times = f"{v / rb[0]:.1f}×" if rb[0] else "far above"
                sig.append({"type": f"outlier_{k}", "weight": WEIGHTS["outlier"], "stage": None, "key": k,
                            "text": f"{times} the usual {label} for {f['task']}"})
        share = st["paths"][f["path"]] / st["n"]
        if st["n"] >= 100 and share < 0.01:
            sig.append({"type": "rare_path", "weight": WEIGHTS["rare_path"], "stage": None, "key": "",
                        "text": f"A path {st['paths'][f['path']]} of {st['n']} {f['task']} traces took"})
        if not f["doc"].completed_at and f["doc"].received_at < window.end - timedelta(days=1):
            sig.append({"type": "stuck", "weight": WEIGHTS["stuck"], "stage": None, "key": "",
                        "text": "Never finished"})
        total = sum(s["weight"] for s in sig)
        if total >= threshold:
            scored.append({"trace_id": d_id, "task": f["task"], "score": round(total, 1), "signals": sig,
                           "received_at": f["doc"].received_at, "steps": f["steps"], "cost": f["cost"],
                           "agent": traj is not None})
    scored.sort(key=lambda x: -x["score"])
    return {"traces": len(facts), "anomalous": scored, "threshold": threshold,
            "facts": facts, "trajs": trajs, "tasks": {t: s["n"] for t, s in stats.items()}}


# ---------- patterns ----------

NAMES = {
    "contract": "Breaks “{label}”", "reported": "Reported wrong {key}", "tool_error": "{stage} errors, never recovered",
    "loop": "Loops on {stage}", "failed_step": "{stage} fails", "feedback": "Users react badly ({key})",
    "fallback": "A fallback model answers at {stage}", "stuck": "Never finishes",
    "outlier_steps": "Far more steps than usual", "outlier_seconds": "Far slower than usual",
    "outlier_cost": "Far costlier than usual", "rare_path": "Takes a rare path",
}


CAUSES = {"contract", "reported", "tool_error", "loop", "failed_step", "fallback"}
TASK_RELATIVE = {"outlier_steps", "outlier_seconds", "outlier_cost", "rare_path", "feedback", "stuck"}


def _primary(sig: List[dict]) -> dict:
    """The signal a trace is grouped by: a cause (a broken contract, a tool error) before a
    symptom (a thumbs down, an outlier), then the heaviest."""
    return sorted(sig, key=lambda s: (s["type"] not in CAUSES, -s["weight"],
                                      PRIORITY.index(s["type"]) if s["type"] in PRIORITY else 99))[0]


def _kind(p: dict) -> str:
    if p.get("infra"):
        return "infrastructure"
    if p["type"] in ("outlier_steps", "outlier_seconds", "outlier_cost", "rare_path", "fallback"):
        return "unusual"  # worth a look, not necessarily wrong
    return "failure"


def _features(f: dict, traj: Optional[dict]) -> set:
    from assay import agents
    from assay.failures import doc_features
    out = doc_features((f["doc"], f["runs"], f["calls"]))
    return out | (agents.features(traj) if traj is not None else set())


def patterns(source, window: Window, engine: Engine, threshold: float = THRESHOLD, update_log: bool = True) -> dict:
    """Anomalous traces clustered into patterns, each with its status in the loop."""
    from assay.failures import burst, distinguishing, onset
    sc = score(source, window, engine, threshold)
    facts, trajs = sc["facts"], sc["trajs"]
    anomalous = {a["trace_id"] for a in sc["anomalous"]}
    normal = [_features(f, trajs.get(d)) for d, f in facts.items() if d not in anomalous][:6000]
    pop_times = [f["doc"].received_at for f in facts.values()]
    labels = {c["id"]: c["label"] for c in contracts_mod.load(engine, source.name)}
    groups = defaultdict(list)
    for a in sc["anomalous"]:
        p = _primary(a["signals"])
        a["primary"] = p
        # The task splits a pattern only when the signal is about the task (an outlier for it, a rare
        # path in it, what its users said); a broken contract or a failing step is one cause across tasks.
        task = a["task"] if p["type"] in TASK_RELATIVE else ""
        groups["|".join([p["type"], p.get("key") or "", p.get("stage") or "", task])].append(a)
    log = _log(engine, source.name)
    suite = _suite_patterns(engine, source.name)
    out = []
    for key, members in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        p = members[0]["primary"]
        kind = "infrastructure" if sum(_kind(m["primary"]) == "infrastructure" for m in members) / len(members) >= 0.5 \
            else _kind(p)
        times = [m["received_at"] for m in members]
        name = NAMES.get(p["type"], p["type"]).format(stage=p.get("stage") or "a step", key=p.get("key") or "",
                                                      label=labels.get(int(p["key"]) if (p.get("key") or "").isdigit()
                                                                       else None, p.get("key")))
        other = Counter(s["type"] for m in members for s in m["signals"] if s is not m["primary"])
        name = name if re.match(r"\S*[_@\d]", name) else name[0].upper() + name[1:]
        tasks = Counter(m["task"] for m in members)
        top = tasks.most_common(1)[0][0]
        where = top if len(tasks) == 1 else f"{len(tasks)} tasks, mostly {top}"
        entry = {"key": key, "name": f"{name} · {where}", "type": p["type"],
                 "kind": kind, "stage": p.get("stage"), "task": top, "tasks": dict(tasks), "traces": len(members),
                 "share": len(members) / max(sum(sc["tasks"].get(t, 0) for t in tasks), 1),
                 "signals": dict(other.most_common(5)), "example": p["text"],
                 "distinguishing": distinguishing([_features(facts[m["trace_id"]], trajs.get(m["trace_id"]))
                                                   for m in members], normal),
                 "onset": onset(times, pop_times), "burst": burst(times, pop_times, timedelta(hours=2)),
                 "first": min(times).isoformat(), "last": max(times).isoformat(),
                 "trace_ids": [m["trace_id"] for m in sorted(members, key=lambda m: -m["score"])][:200],
                 "cases": suite.get(key, 0)}
        out.append(entry)
    if update_log:
        _update_log(engine, source.name, out, window)
        log = _log(engine, source.name)
    seen = {g["key"] for g in out}
    for g in out:
        g["status"] = log.get(g["key"], {}).get("status", "open")
        g["first_seen"] = log.get(g["key"], {}).get("first_seen")
        g["ticket_url"] = log.get(g["key"], {}).get("ticket_url")
        g["ticket_id"] = log.get(g["key"], {}).get("ticket_id")
    # Patterns in the log not seen this window: protected ones are now fixed.
    quiet = [dict(v, key=k, traces=0) for k, v in log.items() if k not in seen and v["status"] in ("fixed", "protected")]
    rank = {"recurred": 0, "open": 1, "protected": 2, "fixed": 3, "dismissed": 4}
    out.sort(key=lambda g: (rank.get(g["status"], 5), g["kind"] != "failure", -g["traces"]))
    loop = loop_metrics(engine, source.name)
    return {"traces": sc["traces"], "anomalous": len(sc["anomalous"]), "threshold": threshold,
            "patterns": out, "quiet": quiet, "loop": loop,
            "window": [window.start.isoformat(), window.end.isoformat()]}


def _log(engine: Engine, source: str) -> Dict[str, dict]:
    t = store.pattern_log
    with engine.connect() as conn:
        return {r.key: {k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r._mapping.items()
                        if k not in ("source", "key")}
                for r in conn.execute(select(t).where(t.c.source == source))}


def _suite_patterns(engine: Engine, source: str) -> Dict[str, int]:
    t = store.suite_cases
    with engine.connect() as conn:
        return dict(Counter(r.pattern for r in conn.execute(select(t.c.pattern).where(t.c.source == source))))


def _update_log(engine: Engine, source: str, found: List[dict], window: Window) -> None:
    """Record patterns seen; protected ones that stopped showing up are fixed; fixed ones seen again recurred."""
    t = store.pattern_log
    now = window.end
    with engine.begin() as conn:
        rows = {r.key: r for r in conn.execute(select(t).where(t.c.source == source))}
        seen = set()
        for g in found:
            seen.add(g["key"])
            first, last = datetime.fromisoformat(g["first"]), datetime.fromisoformat(g["last"])
            r = rows.get(g["key"])
            if r is None:
                conn.execute(t.insert().values(source=source, key=g["key"], name=g["name"], kind=g["kind"],
                                               first_seen=first,
                                               last_seen=last, traces=g["traces"],
                                               status="protected" if g["cases"] else "open",
                                               protected_at=now if g["cases"] else None))
                continue
            vals = {"last_seen": max(r.last_seen, last), "traces": g["traces"], "name": g["name"], "kind": g["kind"]}
            if r.status == "fixed" and r.fixed_at and last > r.fixed_at:
                vals |= {"status": "recurred", "recurred_at": last}
            conn.execute(t.update().where(and_(t.c.source == source, t.c.key == g["key"])).values(**vals))
        for k, r in rows.items():
            if k not in seen and r.status in ("protected",) and r.last_seen < window.start:
                conn.execute(t.update().where(and_(t.c.source == source, t.c.key == k))
                             .values(status="fixed", fixed_at=now))


def set_status(engine: Engine, source: str, key: str, status: str) -> None:
    t = store.pattern_log
    with engine.begin() as conn:
        conn.execute(t.update().where(and_(t.c.source == source, t.c.key == key)).values(status=status))


def loop_metrics(engine: Engine, source: str) -> dict:
    """How well the loop is closing: coverage, time from first seen to a test, recurrences."""
    log = _log(engine, source)
    # Failure patterns only: infrastructure and merely unusual ones are never made into tests.
    real = {k: v for k, v in log.items() if v["status"] != "dismissed" and (v.get("kind") or "failure") == "failure"}
    protected = [v for v in real.values() if v["status"] in ("protected", "fixed", "recurred")]
    loop_hours = [(datetime.fromisoformat(v["protected_at"]) - datetime.fromisoformat(v["first_seen"])).total_seconds()
                  / 3600 for v in protected if v.get("protected_at")]
    return {"patterns": len(real), "protected": len(protected),
            "coverage": len(protected) / len(real) if real else None,
            "fixed": sum(1 for v in real.values() if v["status"] == "fixed"),
            "recurred": sum(1 for v in real.values() if v["status"] == "recurred"),
            "median_hours_to_test": median(loop_hours) if loop_hours else None}


# ---------- candidate test cases ----------

def _inputs(engine: Engine, tenant: str, ids: List[str]) -> Dict[str, dict]:
    t = store.trace_inputs
    out = {}
    with engine.connect() as conn:
        for i in range(0, len(ids), 500):
            for r in conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.trace_id.in_(ids[i:i + 500])))):
                out[r.trace_id] = {"input": r.input, "input_ref": r.input_ref}
    return out


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def representatives(members: List[str], feats: Dict[str, set], k: int = REPRESENTATIVES,
                    min_distance: float = 0.15) -> List[str]:
    """The most typical member, then the most different ones, skipping near-duplicates."""
    if not members:
        return []
    pool = members[:300]
    medoid = max(pool, key=lambda m: sum(_jaccard(feats[m], feats[o]) for o in pool))
    chosen = [medoid]
    while len(chosen) < k:
        far = max((m for m in pool if m not in chosen), key=lambda m: min(1 - _jaccard(feats[m], feats[c])
                                                                        for c in chosen), default=None)
        if far is None or min(1 - _jaccard(feats[far], feats[c]) for c in chosen) < min_distance:
            break
        chosen.append(far)
    return chosen


def _shape(values: List) -> Optional[str]:
    """A regex for identifier-like values ("O-10017", "C-2000"): letters kept, digit runs generalized.
    None when the values have no common shape, or look like free text."""
    shapes = Counter()
    for v in values[:50]:
        if not isinstance(v, str) or not re.fullmatch(r"[A-Za-z]{0,6}[-_#]?\d{2,}", v):
            continue
        shapes[re.sub(r"\d+", r"\\d+", re.escape(v).replace("\\-", "-"))] += 1
    if not shapes:
        return None
    shape, n = shapes.most_common(1)[0]
    return rf"(?<![\w-]){shape}(?!\w)" if n / len(values[:50]) >= 0.8 else None


def _in_input(value, inp) -> bool:
    text = inp if isinstance(inp, str) else json.dumps(inp, default=str) if inp is not None else ""
    return value is not None and str(value) != "" and str(value) in text


def draft(trace_id: str, pattern: dict, facts: dict, trajs: dict, anomalous: set, inputs: dict,
          rules: List[dict], errors: List, signals: Optional[List[dict]] = None) -> dict:
    """A candidate test case for one trace, with where every expectation came from."""
    f = facts[trace_id]
    task = f["task"]
    inp = inputs.get(trace_id, {})
    ref, props, prov = {}, [], []
    peers = [d for d, x in facts.items() if x["task"] == task and d not in anomalous]
    traj = trajs.get(trace_id)
    if traj is not None and peers:
        seqs = Counter()
        for d in peers:
            t = trajs.get(d)
            if t is None:
                continue
            names, prev = [], None
            for s in t["steps"]:
                if s["kind"] == "tool" and not s["error"] and s["name"] != prev:
                    names.append(s["name"])
                    prev = s["name"]
            seqs[tuple(names)] += 1
        if seqs:
            common, n = seqs.most_common(1)[0]
            used = Counter(s["name"] for d in peers if trajs.get(d) for s in trajs[d]["steps"] if s["kind"] == "tool")
            # Keep the arguments whose values appear in the request (an order id, an email).
            args_seen = {}
            for s in traj["steps"]:
                if s["kind"] == "tool":
                    for k, v in (s["args"] or {}).items():
                        # Values from the request, but never personal data: a test mustn't expect an email.
                        if _in_input(v, inp.get("input")) and not pii_scan(str(v)):
                            args_seen.setdefault(s["name"], {})[k] = v
            # An expected call the failing trace never made: take its arguments from the request
            # when a value there looks like what normal traces pass (O-10017 → O-\d+).
            peer_args = defaultdict(lambda: defaultdict(list))
            for d in peers[:300]:
                for x in (trajs.get(d) or {}).get("steps", []):
                    if x["kind"] == "tool":
                        for k, v in (x["args"] or {}).items():
                            peer_args[x["name"]][k].append(v)
            text = inp.get("input") if isinstance(inp.get("input"), str) else json.dumps(inp.get("input"), default=str)
            filled = []
            for c in common:
                for k, vals in peer_args.get(c, {}).items():
                    if k in args_seen.get(c, {}) or not text:
                        continue
                    shape = _shape(vals)
                    m = re.search(shape, text) if shape else None
                    if m and not pii_scan(m.group(0)):
                        args_seen.setdefault(c, {})[k] = m.group(0)
                        filled.append(f"{c}.{k}")
            calls = [{"tool": c, **({"args": args_seen[c]} if c in args_seen else {})} for c in common]
            if filled:
                prov.append({"part": "calls", "from": "request", "reliable": False,
                             "detail": f"{', '.join(filled)} taken from the request, shaped like the values other "
                                       "traces pass"})
            ref["calls"] = calls
            ref["allow_extra"] = sorted(t for t in used if t not in common)
            prov.append({"part": "calls", "from": "passing_traces", "reliable": False,
                         "detail": f"The tool sequence {n} of {sum(seqs.values())} normal {task} traces took"
                                   + (" (arguments kept where their value is in the request)" if args_seen else "")})
    if peers:
        steps = sorted(facts[d]["steps"] for d in peers)
        budget = steps[min(len(steps) - 1, int(math.ceil(0.95 * len(steps))) - 1)]
        ref["max_steps"] = max(budget, 1)
        prov.append({"part": "max_steps", "from": "passing_traces", "reliable": False,
                     "detail": f"95% of {len(peers)} normal {task} traces took {budget} steps or fewer"})
    for e in errors:
        if e.field == "answer":
            ref["answer"] = e.expected
        else:
            ref.setdefault("fields", {})[e.field] = e.expected
        prov.append({"part": f"fields.{e.field}" if e.field != "answer" else "answer", "from": "correction",
                     "reliable": True, "detail": f"{e.reporter or e.source or 'Someone'} reported {e.field} should be "
                                                 f"{e.expected!r} (it was {e.observed!r})"})
    path = [contracts_mod.Step(r.stage, (r.outputs or {}).get("_args")) for r in f["runs"]]
    for c in rules:
        if contracts_mod.applies(c, f["doc"]) and contracts_mod.breaks(c, path, finished=True):
            props.append({"contract": c.get("id"), "label": c.get("label")})
            prov.append({"part": "properties", "from": "property", "reliable": True,
                         "detail": f"This trace broke “{c.get('label')}”; the test requires it to hold"})
    for s in {x["type"] for x in (signals or [])}:
        if s == "loop":
            props.append({"max_identical_calls": 2})
            prov.append({"part": "properties", "from": "property", "reliable": True,
                         "detail": "This trace repeated a call; the test allows at most 2 identical calls"})
    case_id = "prod-" + hashlib.sha1(trace_id.encode()).hexdigest()[:10]
    case = {"case_id": case_id, "task": task, "input": inp.get("input"), "input_ref": inp.get("input_ref"),
            "reference": ref, "properties": props, "origin_trace": trace_id}
    missing = [] if inp else ["No input was captured for this trace, so it can't be replayed: send inputs "
                              "(POST /v1/events/inputs, or input on trajectories)."]
    if "answer" not in ref and traj is not None:
        missing.append("No expected answer: add one, or rely on the tool calls, end state and properties.")
    return {"case": case, "provenance": prov, "pii": pii_scan(inp.get("input")), "missing": missing}


def propose(source, window: Window, engine: Engine, key: str, threshold: float = THRESHOLD) -> List[dict]:
    """Draft candidate cases for one pattern and store them (skipping traces already proposed)."""
    sc = score(source, window, engine, threshold)
    pats = patterns(source, window, engine, threshold, update_log=False)
    pattern = next((p for p in pats["patterns"] if p["key"] == key), None)
    if pattern is None:
        return []
    facts, trajs = sc["facts"], sc["trajs"]
    anomalous = {a["trace_id"] for a in sc["anomalous"]}
    sigs = {a["trace_id"]: a["signals"] for a in sc["anomalous"]}
    tenant = source.name.split(":", 1)[1] if source.name.startswith("events:") else source.name
    feats = {m: _features(facts[m], trajs.get(m)) for m in pattern["trace_ids"] if m in facts}
    inputs = _inputs(engine, tenant, list(feats))
    # Traces we can replay come first.
    members = [m for m in pattern["trace_ids"] if m in inputs] or list(feats)
    rules = contracts_mod.load(engine, source.name)
    errors = defaultdict(list)
    for e in (source.errors(window) if hasattr(source, "errors") else None) or []:
        errors[e.document_id].append(e)
    t = store.regression_candidates
    made = []
    with engine.begin() as conn:
        taken = {r.trace_id for r in conn.execute(select(t.c.trace_id).where(t.c.source == source.name))}
        rejected = conn.execute(select(t.c.id).where(and_(t.c.source == source.name, t.c.pattern == key,
                                                          t.c.status == "rejected"))).all()
        for m in representatives([m for m in members if m not in taken], feats):
            d = draft(m, pattern, facts, trajs, anomalous, inputs, rules, errors.get(m, []), sigs.get(m))
            d["provenance"].insert(0, {"part": "pattern", "from": "pattern", "reliable": True,
                                       "detail": f"{pattern['name']}: {pattern['traces']} traces; e.g. "
                                                 f"{pattern['example']}"})
            if rejected:
                d["missing"].append(f"{len(rejected)} earlier candidate(s) from this pattern were rejected.")
            row = dict(source=source.name, pattern=key, trace_id=m, task=pattern["task"], status="proposed",
                       case=d["case"] | {"missing": d["missing"]}, provenance=d["provenance"], pii=d["pii"],
                       created_at=datetime.utcnow())
            row["id"] = conn.execute(t.insert().values(**row)).inserted_primary_key[0]
            made.append(row)
    return made


def candidates(engine: Engine, source: str, status: Optional[str] = None) -> List[dict]:
    t = store.regression_candidates
    cond = [t.c.source == source] + ([t.c.status == status] if status else [])
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(and_(*cond)).order_by(t.c.id.desc())).all()
    return [{k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in r._mapping.items()} for r in rows]


CONTENT = ("text", "args", "result", "value")  # step fields that can hold personal data


def _step(r: dict) -> dict:
    """A stored step in the event schema's shape (docs/event-schema.md): what was sent, back."""
    kind = "llm" if r["kind"] == "reason" else r["kind"]
    out = {"seq": r["seq"], "kind": kind, "name": r.get("name"), "parent_seq": r.get("parent_seq"),
           "status": "error" if r.get("error") else "ok", "error": r.get("error"),
           "started_at": r.get("started_at"), "ended_at": r.get("finished_at"), "text": r.get("text")}
    if kind == "llm":
        tokens_out = r.get("tokens_out")
        if tokens_out is None and r.get("tokens") is not None:  # recorded before tokens_out was kept
            tokens_out = r["tokens"] - (r.get("tokens_in") or 0) or None
        out |= {"model": r.get("model"), "prompt": r.get("prompt"), "tokens_in": r.get("tokens_in"),
                "tokens_out": tokens_out, "cost_usd": r.get("cost_usd"), "tools": r.get("tools")}
    elif kind in ("tool", "mcp_prompt"):
        out |= {"args": r.get("args"), "result": r.get("result"), "server": r.get("server")}
    elif kind == "resource":
        out |= {"uri": (r.get("args") or {}).get("uri"), "result": r.get("result"), "server": r.get("server")}
        if out["name"] == (out["uri"] or "")[:128]:  # named after its uri at ingest: don't repeat it
            out["name"] = None
    elif kind == "state":
        out |= {"op": (r.get("args") or {}).get("op"), "value": r.get("result")}
    elif kind == "approval":
        out |= {"decision": (r.get("args") or {}).get("decision"), "by": (r.get("args") or {}).get("by")}
    ser = lambda v: v.isoformat() if isinstance(v, datetime) else v
    return {k: ser(v) for k, v in out.items() if v is not None}


def snapshot(engine: Engine, source: str, trace_id: str, redact_pii: bool = True,
             with_conversation: bool = True) -> Optional[dict]:
    """The trace in full, to keep with a test case: its input, every step (model calls with their
    model, prompt, tokens and the tools they were offered; tool calls with arguments and results;
    MCP resource reads and prompts; approvals; state changes), its answer, and the run's metadata.
    A turn of a conversation also gets the turns before it (`conversation`), so the case can be
    replayed with what was said before. A saved case then doesn't lose what the agent did, even
    once the trace itself is gone. None if the trace isn't an agent run."""
    from assay.sources.events import EventsSource
    tenant = source.split(":", 1)[1] if source.startswith("events:") else source
    traj = EventsSource(engine, tenant).trajectories([trace_id]).get(trace_id)
    if traj is None:
        return None
    r = store.runs
    with engine.connect() as conn:
        run = conn.execute(select(r.c.tags, r.c.segment, r.c.parent_run_id, r.c.error).where(
            and_(r.c.tenant == tenant, r.c.run_id == trace_id))).first()
    inp = _inputs(engine, tenant, [trace_id]).get(trace_id) or {}
    clean = redact if redact_pii else (lambda v: v)
    steps = [_step(x) for x in traj["steps"]]
    for st in steps:
        for k in CONTENT:
            if k in st:
                st[k] = clean(st[k])
    ser = lambda v: v.isoformat() if isinstance(v, datetime) else v
    out = {"trace_id": trace_id, "task": traj.get("task"), "input": clean(inp.get("input")),
           "input_ref": inp.get("input_ref"), "steps": steps, "output": clean(traj.get("answer")),
           "status": traj.get("status"), "outcome": traj.get("outcome"), "error": run.error if run else None,
           "version": traj.get("lineage"), "tags": run.tags if run else None,
           "segment": run.segment if run else None, "parent_run_id": run.parent_run_id if run else None,
           "started_at": ser(traj.get("started_at")), "finished_at": ser(traj.get("finished_at")),
           "conversation_id": traj.get("conversation_id"), "turn": traj.get("turn")}
    if with_conversation and traj.get("conversation_id"):
        from assay import agents
        before = [t for t in agents.conversation_turns(engine, tenant, traj["conversation_id"])
                  if t["trajectory_id"] != trace_id and _earlier(t, traj)]
        out["conversation"] = [snapshot(engine, source, t["trajectory_id"], redact_pii, False) for t in before]
    return {k: v for k, v in out.items() if v is not None}


def _earlier(a: dict, b: dict) -> bool:
    """Turn a came before turn b: by turn number when both have one, else by start time."""
    if a.get("turn") is not None and b.get("turn") is not None:
        return a["turn"] < b["turn"]
    return a["started_at"] < b["started_at"]


def approve(engine: Engine, source: str, candidate_id: int, suite: str, case: Optional[dict] = None,
            redact_pii: bool = True, by: Optional[str] = None) -> Optional[dict]:
    """Add a candidate to a suite (with the developer's edits), and for agents store its reference."""
    t, sc = store.regression_candidates, store.suite_cases
    with engine.begin() as conn:
        row = conn.execute(select(t).where(and_(t.c.source == source, t.c.id == candidate_id))).first()
        if row is None:
            return None
        c = dict(row.case) | (case or {})
        inp = redact(c.get("input")) if redact_pii else c.get("input")
        ref = c.get("reference") or {}
        props = c.get("properties") or []
        conn.execute(sc.delete().where(and_(sc.c.source == source, sc.c.case_id == c["case_id"])))
        conn.execute(sc.insert().values(source=source, case_id=c["case_id"], suite=suite, candidate_id=candidate_id,
                                        pattern=row.pattern, origin_trace=row.trace_id, task=row.task, input=inp,
                                        input_ref=c.get("input_ref"), reference=ref, properties=props,
                                        trajectory=snapshot(engine, source, row.trace_id, redact_pii),
                                        added_at=datetime.utcnow(), added_by=by))
        conn.execute(t.update().where(t.c.id == candidate_id).values(
            status="approved", decided_at=datetime.utcnow(), decided_by=by, case=c | {"input": inp}))
        lg = store.pattern_log
        conn.execute(lg.update().where(and_(lg.c.source == source, lg.c.key == row.pattern,
                                            lg.c.status.in_(("open", "recurred"))))
                     .values(status="protected", protected_at=datetime.utcnow()))
    if ref.get("calls") or ref.get("answer") or ref.get("state"):
        from assay import ingest
        tenant = source.split(":", 1)[1] if source.startswith("events:") else source
        # Properties need no extra storage: contracts are checked by the safety check, and
        # "at most 2 identical calls" by the efficiency check (3 identical calls fail it).
        ingest.write_references(engine, [ingest.ReferenceEvent(
            case_id=c["case_id"], calls=ref.get("calls") or [], allow_extra=ref.get("allow_extra") or [],
            answer=ref.get("answer"), state=ref.get("state") or [], max_steps=ref.get("max_steps"))], tenant)
    return {"case_id": c["case_id"], "suite": suite, "pattern": row.pattern}


def reject(engine: Engine, source: str, candidate_id: int, note: Optional[str], by: Optional[str] = None) -> bool:
    t = store.regression_candidates
    with engine.begin() as conn:
        n = conn.execute(t.update().where(and_(t.c.source == source, t.c.id == candidate_id))
                         .values(status="rejected", note=note, decided_at=datetime.utcnow(), decided_by=by)).rowcount
    return bool(n)


def suites(engine: Engine, source: str) -> List[dict]:
    t = store.suite_cases
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(t.c.source == source)).all()
    by = defaultdict(list)
    for r in rows:
        by[r.suite].append(r)
    return [{"suite": k, "cases": len(v), "patterns": len({r.pattern for r in v}),
             "last_added": max(r.added_at for r in v).isoformat()} for k, v in sorted(by.items())]


def suite(engine: Engine, source: str, name: str) -> List[dict]:
    """Cases in a suite, each with where it came from and its latest evaluation results."""
    t, ev = store.suite_cases, store.eval_results
    tenant = source.split(":", 1)[1] if source.startswith("events:") else source
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(and_(t.c.source == source, t.c.suite == name))).all()
        ids = [r.case_id for r in rows]
        results = conn.execute(select(ev.c.case_id, ev.c.run_id, ev.c.status, ev.c.ts).where(
            and_(ev.c.tenant == tenant, ev.c.case_id.in_(ids)))).all() if ids else []
    runs = defaultdict(lambda: {"ts": None, "pass": 0, "fail": 0})  # (case, run) → counts
    for r in results:
        x = runs[(r.case_id, r.run_id)]
        x["ts"] = max(x["ts"] or r.ts, r.ts)
        x["pass" if r.status == "pass" else "fail"] += 1
    latest = {}
    for (case, run), x in runs.items():
        if case not in latest or x["ts"] > latest[case]["ts"]:
            latest[case] = {"run_id": run, **x}
    ser = lambda v: v.isoformat() if isinstance(v, datetime) else v
    return [{k: ser(v) for k, v in r._mapping.items() if k != "source"} |
            {"latest": {k: ser(v) for k, v in latest[r.case_id].items()} if r.case_id in latest else None}
            for r in rows]


def guards(engine: Engine, source: str) -> Dict[str, dict]:
    """Suite cases by case_id, with the pattern they guard: for flagging a production bug that's back."""
    t, lg = store.suite_cases, store.pattern_log
    with engine.connect() as conn:
        log = {r.key: r for r in conn.execute(select(lg).where(lg.c.source == source))}
        return {r.case_id: {"pattern": r.pattern, "suite": r.suite,
                            "name": log[r.pattern].name if r.pattern in log else r.pattern,
                            "first_seen": log[r.pattern].first_seen.isoformat() if r.pattern in log else None}
                for r in conn.execute(select(t).where(t.c.source == source))}


def export(cases: List[dict], fmt: str = "json") -> tuple:
    """A suite as a file a test tool can run: one case per entry, with input and expectations, and
    for an agent the trace it came from in full (`trajectory`): every step, not just input and output."""
    import csv
    import io
    rows = [{"case_id": c["case_id"], "task": c.get("task"), "input": c.get("input"), "input_ref": c.get("input_ref"),
             "expected": c.get("reference") or {}, "must_hold": c.get("properties") or [],
             "guards": c.get("pattern"), "from_trace": c.get("origin_trace"),
             **({"trajectory": c["trajectory"]} if c.get("trajectory") else {})} for c in cases]
    if fmt == "jsonl":
        return "\n".join(json.dumps(r, default=str) for r in rows) + "\n", "application/x-ndjson"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=list(rows[0]) if rows else ["case_id"])
        w.writeheader()
        for r in rows:
            w.writerow({k: json.dumps(v, default=str) if isinstance(v, (dict, list)) else v for k, v in r.items()})
        return buf.getvalue(), "text/csv"
    return json.dumps({"cases": rows}, indent=1, default=str), "application/json"
