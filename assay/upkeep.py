"""`assay evals audit`: what each evaluator costs to keep, code checks and judges apart.

A code check (an assertion, a reference answer, a pattern) is cheap to build and keep. A judge needs
100 or so labels, calibration against them, and upkeep every week or so, since prompts, models and
what users ask move under it. This lists every evaluator in recent runs and, for each judge, what
it's missing: labels, a recent calibration, a trust label that holds, and whether a code check
would do its job (most of its failures are about length or format).
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List

from sqlalchemy import select

from assay import store

LABELS = 100  # labels a judge needs before its scores are worth much
WEEK = 7  # days: a judge not calibrated since is due
FORMAT = re.compile(r"\b(too (long|short|verbose|wordy)|length|word count|\d+ words|characters|format|formatt|json|"
                    r"markdown|bullet|heading|table|capital|uppercase|lowercase|emoji|concise|brevity|list)\b", re.I)


def is_judge(r) -> bool:
    return bool(getattr(r, "judge_model", None) or getattr(r, "judge_prompt", None)
                or "judge" in (r.evaluator or "").lower())


def audit(engine, tenant: str, days: float, golden_items: Dict[str, int]) -> dict:
    from assay.local import trust
    t = store.eval_results
    since = datetime.utcnow() - timedelta(days=days)
    with engine.connect() as conn:
        rows = conn.execute(select(t).where((t.c.tenant == tenant) & (t.c.ts >= since))).all()
    code, judges = defaultdict(int), defaultdict(list)
    for r in rows:
        f = r.field or "result"
        if is_judge(r):
            judges[f].append(r)
        else:
            code[f] += 1
    tr = trust(engine, tenant, [r for rs in judges.values() for r in rs])
    out = []
    for f, rs in sorted(judges.items()):
        x = tr.get(f) or {"state": "none"}
        labels = x.get("n") or golden_items.get(f) or 0
        failed = [r for r in rs if r.status == "fail"]
        about_format = sum(1 for r in failed if FORMAT.search(r.reason or ""))
        issues = []
        if labels < LABELS:
            issues.append(f"{labels} labels; a judge needs {LABELS} or so before its scores mean much "
                          "(assay golden suggest picks what to label)")
        if x["state"] == "none":
            issues.append("never calibrated against people (assay calibrate)")
        elif x.get("age") is not None and x["age"] > WEEK:
            issues.append(f"not calibrated in {x['age']} days: a judge needs upkeep every week or so")
        if x["state"] in ("regressed", "other_judge"):
            issues.append("its trust label doesn't hold: " + {"regressed": "the last calibration regressed",
                                                                  "other_judge": "calibrated for another judge"}[x["state"]])
        if len(failed) >= 3 and about_format / len(failed) >= 0.5:
            issues.append(f"{about_format} of its {len(failed)} failures are about length or format: a code check "
                          "(a word limit, a pattern, valid JSON) would do that without the upkeep")
        out.append({"field": f, "results": len(rs), "failures": len(failed), "labels": labels,
                    "calibrated_days_ago": x.get("age"), "trust": x["state"], "issues": issues,
                    "models": sorted({r.judge_model for r in rs if getattr(r, "judge_model", None)})})
    return {"days": days, "code_checks": len(code), "code_results": sum(code.values()), "judges": out}


def text(a: dict) -> str:
    j = a["judges"]
    head = (f"Evaluators in the last {a['days']:g} days: {a['code_checks']} code check{'s' * (a['code_checks'] != 1)}, "
            f"{len(j)} judge{'s' * (len(j) != 1)}.")
    if not j:
        return head + " No judges: nothing to label, calibrate or keep up."
    lines = [head, "", f"  {'judge':<24} {'labels':>8}  {'calibrated':<14} trust"]
    for x in j:
        when = "never" if x["calibrated_days_ago"] is None else \
            "today" if x["calibrated_days_ago"] == 0 else f"{x['calibrated_days_ago']} days ago"
        lines.append(f"  {x['field'][:24]:<24} {str(x['labels']) + '/' + str(LABELS):>8}  {when:<14} {x['trust']}")
    due = [(x["field"], i) for x in j for i in x["issues"]]
    if due:
        lines.append("")
        lines += [f"  - {f}: {i}" for f, i in due]
    else:
        lines += ["", "Every judge has its labels, a calibration this week, and a trust label that holds."]
    return "\n".join(lines)


def cli(root: Path, days: float, fmt: str) -> int:
    from assay import calibrate, local
    engine, _ = local._open(root)
    if engine is None:
        print("No test runs yet: run `assay test` first.", file=sys.stderr)
        return 2
    golden = {}
    try:
        ccfg = local._calib_cfg(root)
        items = calibrate.load_golden(root / ccfg["golden"])
        if ccfg.get("field"):
            golden[ccfg["field"]] = len(items)
    except Exception:
        pass
    a = audit(engine, local.TENANT, days, golden)
    print(json.dumps(a, indent=1) if fmt == "json" else text(a))
    return 0
