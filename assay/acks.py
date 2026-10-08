"""Acknowledged failures: "I already know", said about one check, for a while, and never as a mute.

    assay ack tests/test_support.py::test_refund consistency --reason "judge disagrees, #412" --for 14d
    assay acks                    # what's acknowledged, what expires soon, what woke up and why
    assay acks --prune            # drop the expired and the spent

An acknowledgement is one test case and one check (a field, or behavior.<metric>), never a
pattern, so it can't quiet anything else. It records who, why, when and until when, in
assay.acks.toml next to assay.toml: in the repository, so CI sees it and a reviewer sees it in
the pull request. A pull request that adds one is loosening the checks that judge it, and is
held to the base branch's acknowledgements unless that's accepted (trusted policy).

It stores what the case was showing when it was acknowledged, not one score:

  band        its score over its last runs (lowest, highest, median): a noisy judge that
              swings between 2 and 3 on its own has a band of 2-3
  pass_rate   how many of its attempts passed
  classes     how it failed: the kind of check and the mechanism (Security · Unsafe action,
              Output quality · Wrong answer, Your asserts · AssertionError), or the category the
              evaluator named (Reasoning · grounding, Reasoning · policy_refusal); never the raw
              text. A judge that names no category is "low score", and only its band can wake it

While it holds, the case is quiet: it doesn't fail the run, and it's one line in the report.
It wakes, and fails the run, only when it has something new to say:

  - a score below the band (inside it is the noise it already showed);
  - a pass rate lower than it was, beyond chance;
  - a failure of a class it didn't have (a grounding miss that became a safety breach);
  - for behavior, a number past the highest it showed, by the metric's minimum.

It ends on its own: at `until` (DEFAULT_DAYS, at most MAX_DAYS: nothing is permanent), after
which the failure is reported as it would be without it, and when the check passes in a later
run (spent), so a failure that comes back is news again.
"""
from __future__ import annotations

import getpass
import json
import math
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional, Tuple

from sqlalchemy import select

try:
    import tomllib
except ImportError:  # Python 3.10
    import tomli as tomllib

from assay import store

DEFAULT_DAYS, MAX_DAYS, SOON_DAYS = 14, 90, 3
HISTORY = 10  # runs of the check the band is taken from
Z = 1.96  # a pass rate lower beyond chance: a one-sided two-proportion test at about 2.5%


class AckError(ValueError):
    pass


def _now() -> datetime:
    # To the millisecond: a run in the same second as an acknowledgement is before it or after it.
    t = datetime.now(timezone.utc)
    return t.replace(microsecond=t.microsecond // 1000 * 1000)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo else t.replace(tzinfo=timezone.utc)


def path_for(config: Path) -> Path:
    """assay.toml → assay.acks.toml (and the trusted assay.base.toml → assay.base.acks.toml)."""
    return config.with_name(config.name[:-5] + ".acks.toml" if config.name.endswith(".toml")
                            else config.name + ".acks.toml")


def duration(text: str) -> timedelta:
    """14d, 2w, 36h: how long an acknowledgement holds."""
    m = re.fullmatch(r"\s*(\d+)\s*([hdw])\s*", str(text or "").lower())
    if not m:
        raise AckError(f"--for {text!r}: a number of hours, days or weeks, e.g. 36h, 14d or 2w.")
    n, unit = int(m.group(1)), m.group(2)
    d = timedelta(hours=n) if unit == "h" else timedelta(days=n * (7 if unit == "w" else 1))
    if d <= timedelta(0):
        raise AckError("--for: longer than nothing.")
    if d > timedelta(days=MAX_DAYS):
        raise AckError(f"--for {text}: at most {MAX_DAYS} days. An acknowledgement always ends; renew it if "
                       f"it still holds then.")
    return d


def who() -> str:
    try:
        name = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        name = ""
    return name or os.environ.get("GITHUB_ACTOR") or getpass.getuser()


# ---------- the file ----------

def load(config: Path) -> List[dict]:
    path = path_for(config)
    if not path.exists():
        return []
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise AckError(f"{path.name} isn't valid TOML: {exc}")
    out = []
    for i, a in enumerate(data.get("ack") or [], 1):
        for k in ("case", "check", "reason", "by", "at", "until"):
            if not a.get(k):
                raise AckError(f"{path.name}, acknowledgement {i}: {k} is missing.")
        a = dict(a)
        for k in ("at", "until"):
            if isinstance(a[k], str):
                try:
                    a[k] = datetime.fromisoformat(a[k].replace("Z", "+00:00"))
                except ValueError:
                    raise AckError(f"{path.name}, acknowledgement {i}: {k} isn't a date and time.")
            a[k] = _aware(a[k])
        if a["until"] - a["at"] > timedelta(days=MAX_DAYS, hours=1):
            raise AckError(f"{path.name}, acknowledgement {i} ({a['case']} {a['check']}): holds for more than "
                           f"{MAX_DAYS} days. Nothing is acknowledged for ever.")
        a["classes"] = list(a.get("classes") or [])
        out.append(a)
    return out


def _s(v) -> str:
    return json.dumps(v, ensure_ascii=False)  # a TOML basic string


def _t(v: datetime) -> str:
    v = _aware(v).astimezone(timezone.utc)
    return v.strftime("%Y-%m-%dT%H:%M:%S") + (f".{v.microsecond // 1000:03d}" if v.microsecond else "") + "Z"


def _inline(d: dict) -> str:
    return "{ " + ", ".join(f"{k} = {_s(v) if isinstance(v, str) else json.dumps(v)}" for k, v in d.items()
                            if v is not None) + " }"


HEADER = """\
# Failures someone has looked at: quiet until they get worse, and never for ever.
# Written by `assay ack`; `assay acks` lists them. See docs/testing.md#acknowledging-a-failure.
"""


def save(config: Path, acks: List[dict]) -> Path:
    path = path_for(config)
    out = [HEADER]
    for a in acks:
        out += ["[[ack]]", f"case = {_s(a['case'])}", f"check = {_s(a['check'])}", f"reason = {_s(a['reason'])}",
                f"by = {_s(a['by'])}", f"at = {_t(a['at'])}", f"until = {_t(a['until'])}",
                f"classes = {json.dumps(a.get('classes') or [], ensure_ascii=False)}"]
        for k in ("band", "pass_rate", "values"):
            if a.get(k):
                out.append(f"{k} = {_inline(a[k])}")
        out.append("")
    path.write_text("\n".join(out))
    return path


def key(a: dict) -> Tuple[str, str]:
    return a["case"], a["check"]


# ---------- how a check failed ----------

MECHANISMS = ("Unsafe action", "Tool error, not recovered", "Looped", "Wrong tool", "Wrong arguments", "Stopped early",
              "Ignored a tool result", "Wrong end state", "Wrong answer", "Followed injected instructions",
              "Strayed from its plan", "Personal data leaked")


def failure_class(field: str, reason: Optional[str], category: Optional[str] = None) -> str:
    """The kind of check and how it failed, without the specifics: what to dedup on. The
    evaluator's own category when it names one; else read from the reason's form."""
    from assay import learn, local
    family = next(n for n, m in local.CATEGORIES if m(field or "result"))
    if category:
        return f"{family} · {category.strip().lower().replace(' ', '_')}"
    text = (reason or "").strip()
    head = text.split(":", 1)[0].strip()
    if head in MECHANISMS:
        how = head
        if head == "Personal data leaked":  # a new kind of data leaking is a new failure
            kinds = sorted(k for k in learn.PII if re.search(rf"\b{re.escape(k)}\b", text, re.I))
            how += f" ({', '.join(kinds)})" if kinds else ""
    elif re.fullmatch(r"[A-Z]\w*(Error|Exception|Failure)", head):
        how = head  # an assert or exception: its type
    elif re.match(r"\d+(\.\d+)?/\d+", text) or field in ("plan_quality", "consistency"):
        how = "low score"  # a judge's score: how low is the band's business
    elif field.startswith(("expect.", "max_")):
        how = field
    else:
        how = "failed"
    return f"{family} · {how}"


# ---------- what the case was showing ----------

def _rows(engine, tenant: str, case: str, field: str) -> list:
    t = store.eval_results
    with engine.connect() as conn:
        return conn.execute(select(t.c.run_id, t.c.status, t.c.score, t.c.reason, t.c.ts, t.c.category).where(
            (t.c.tenant == tenant) & (t.c.case_id == case) & (t.c.field == field)
            & (t.c.run_id != "baseline"))).all()


def _by_run(rows) -> List[Tuple[str, datetime, list]]:
    runs: Dict[str, list] = {}
    for r in rows:
        runs.setdefault(r.run_id, []).append(r)
    out = [(rid, max(_aware(r.ts) for r in rs), rs) for rid, rs in runs.items()]
    return sorted(out, key=lambda x: x[1])


def _judged(rs) -> list:
    return [r for r in rs if r.status in ("pass", "fail")]


def snapshot(engine, tenant: str, run_id: str, case: str, check: str) -> dict:
    """What an acknowledgement of this check stores: its band, pass rate and failure classes."""
    if check.startswith("behavior."):
        return _behavior_snapshot(engine, tenant, run_id, case, check[len("behavior."):])
    runs = _by_run(_rows(engine, tenant, case, check))
    now = next((rs for rid, _, rs in runs if rid == run_id), None)
    if not now or not any(r.status == "fail" for r in now):
        raise AckError(f"{case} {check} doesn't fail in {run_id}: there's nothing to acknowledge.")
    upto = [x for x in runs if x[1] <= max(_aware(r.ts) for r in now)][-HISTORY:]
    scores = [r.score for _, _, rs in upto for r in _judged(rs) if r.score is not None]
    judged = _judged(now)
    band = {"min": min(scores), "max": max(scores), "median": median(scores), "n": len(scores)} if scores else None
    return {"band": band, "pass_rate": {"passed": sum(r.status == "pass" for r in judged), "total": len(judged)},
            "classes": sorted({failure_class(check, r.reason, r.category) for r in now if r.status == "fail"})}


def _metric_history(engine, tenant: str, case: str) -> List[Tuple[str, dict]]:
    from assay import behavior
    m = store.run_metrics
    with engine.connect() as conn:
        rows = conn.execute(select(m.c.run_id, m.c.metrics).where((m.c.tenant == tenant) & (m.c.case_id == case)
                                                                   & (m.c.run_id != "baseline"))).all()
    by: Dict[str, list] = {}
    for r in rows:
        by.setdefault(r.run_id, []).append(r.metrics)
    return [(rid, behavior.combine(ms)) for rid, ms in sorted(by.items())]


def _metrics_of(metric: str) -> List[str]:
    return ["fragments", "retrieved_tokens"] if metric == "retrieved_context" else [metric]


def _behavior_snapshot(engine, tenant: str, run_id: str, case: str, metric: str) -> dict:
    from assay import behavior
    names = _metrics_of(metric)
    if any(n not in behavior.NUMBERS for n in names):
        raise AckError(f"behavior.{metric}: one of {', '.join('behavior.' + k for k in behavior.NUMBERS)}, "
                       f"or behavior.retrieved_context.")
    hist = _metric_history(engine, tenant, case)
    ids = [rid for rid, _ in hist]
    if run_id not in ids:
        raise AckError(f"{case} has no behavior recorded in {run_id}.")
    upto = hist[:ids.index(run_id) + 1][-HISTORY:]
    values = {}
    for n in names:
        vs = [m[n] for _, m in upto if m.get(n) is not None]
        if vs:
            values[n] = max(vs)
    if not values:
        raise AckError(f"{case} has no {metric} recorded: there's nothing to acknowledge.")
    return {"values": values, "classes": [f"Behavior · {metric}"]}


# ---------- deciding ----------

def _lower_beyond_chance(p1: int, n1: int, p2: int, n2: int) -> bool:
    """p2/n2 lower than p1/n1 beyond chance."""
    if n1 < 2 or n2 < 2 or p2 / n2 >= p1 / n1:
        return False
    p = (p1 + p2) / (n1 + n2)
    se = math.sqrt(p * (1 - p) * (1 / n1 + 1 / n2))
    return se > 0 and (p1 / n1 - p2 / n2) / se > Z


def decide(engine, tenant: str, run_id: str, ack: dict, now: Optional[datetime] = None,
           current_metrics: Optional[dict] = None) -> Tuple[str, Optional[str]]:
    """(state, why) for one acknowledgement in this run: quiet, woke (worse than acknowledged),
    expired, spent (the check passed after it was made), or passing (it passes now)."""
    now = now or _now()
    if now >= ack["until"]:
        return "expired", f"acknowledged by {ack['by']} until {ack['until']:%Y-%m-%d}"
    case, check = ack["case"], ack["check"]
    if check.startswith("behavior."):
        return _decide_behavior(ack, current_metrics)
    runs = _by_run(_rows(engine, tenant, case, check))
    cur = next((rs for rid, _, rs in runs if rid == run_id), None)
    for rid, ts, rs in runs:
        if rid != run_id and ts > ack["at"] and _judged(rs) and all(r.status == "pass" for r in _judged(rs)):
            return "spent", f"passed in {rid}, after it was acknowledged"
    if not cur or not any(r.status == "fail" for r in cur):
        return "passing", None
    classes = {failure_class(check, r.reason, r.category) for r in cur if r.status == "fail"}
    new = sorted(classes - set(ack.get("classes") or []))
    if new and ack.get("classes"):
        return "woke", f"fails differently now: {', '.join(new)} (acknowledged: {', '.join(ack['classes'])})"
    band = ack.get("band")
    scores = [r.score for r in _judged(cur) if r.score is not None]
    if band and scores and median(scores) < band["min"]:
        return "woke", (f"score {median(scores):g}, below the {band['min']:g}–{band['max']:g} it showed when "
                        f"acknowledged")
    pr, judged = ack.get("pass_rate") or {}, _judged(cur)
    passed = sum(r.status == "pass" for r in judged)
    if pr and _lower_beyond_chance(pr.get("passed", 0), pr.get("total", 0), passed, len(judged)):
        return "woke", (f"passes {passed} of {len(judged)} attempts, down from {pr['passed']} of {pr['total']} when "
                        f"acknowledged")
    return "quiet", None


def _decide_behavior(ack: dict, current: Optional[dict]) -> Tuple[str, Optional[str]]:
    from assay import behavior
    if not current:
        return "passing", None
    for n, top in (ack.get("values") or {}).items():
        v = current.get(n)
        least, show = behavior.NUMBERS[n][1], behavior.NUMBERS[n][2]
        if v is not None and v - top >= least and v > top:
            return "woke", f"{behavior.LABELS[n].lower()} {show(v)}, past the {show(top)} it showed when acknowledged"
    return "quiet", None


def apply(engine, tenant: str, run_id: str, result: dict, acks: List[dict], now: Optional[datetime] = None) -> dict:
    """Decide every acknowledgement for this run's result, and record it in result["acks"]:
    {"quiet": {(case, check): ack}, "woke": {(case, check): (ack, why)}, "expired": [...],
    "spent": [...], "passing": [...]}. The comparison reads it (local.classify, the behavior list)."""
    from assay import local
    out = {"quiet": {}, "woke": {}, "expired": [], "spent": [], "passing": []}
    if not acks:
        result.pop("_suite", None)
        result["acks"] = out
        return out
    metrics = local.case_behavior(engine, run_id, tenant) if any(a["check"].startswith("behavior.") for a in acks) \
        else {}
    flagged = {(b["case_id"], f"behavior.{ch['metric']}") for b in result.get("behavior") or [] for ch in b["changes"]}
    for a in acks:
        state, why = decide(engine, tenant, run_id, a, now, metrics.get(a["case"]))
        if a["check"].startswith("behavior.") and state == "quiet" and key(a) not in flagged:
            state = "passing"  # nothing to quiet: it isn't worse than its baseline this run
        if state in ("quiet", "woke"):
            out[state][key(a)] = a if state == "quiet" else (a, why)
        else:
            out[state].append({**a, "why": why})
    # Behavior changes: an acknowledged metric that's quiet leaves the list; one that woke says why.
    kept = []
    for b in result.get("behavior") or []:
        changes = []
        for ch in b["changes"]:
            k = (b["case_id"], f"behavior.{ch['metric']}")
            if k in out["quiet"]:
                continue
            if k in out["woke"]:
                ch = {**ch, "text": f"{ch['text']} (worse than acknowledged: {out['woke'][k][1]})"}
            changes.append(ch)
        if changes:
            kept.append({**b, "changes": changes})
    result["behavior"] = kept
    # A case whose behavior someone acknowledged isn't news in the whole run's totals either.
    quiet_cases = {c for c, k in out["quiet"] if k.startswith("behavior.")}
    if quiet_cases and result.get("_suite"):
        from assay import behavior
        now_m, before_m, compared, ratio = result["_suite"]
        rest = [c for c in compared if c not in quiet_cases]
        result["behavior_suite"] = [{**x, "text": x["text"] + f", leaving out {len(quiet_cases)} acknowledged"}
                                    for x in behavior.suite_totals(now_m, before_m, rest, ratio)]
    result.pop("_suite", None)
    result["acks"] = out
    return out


def soon(a: dict, now: Optional[datetime] = None) -> bool:
    return a["until"] - (now or _now()) <= timedelta(days=SOON_DAYS)
