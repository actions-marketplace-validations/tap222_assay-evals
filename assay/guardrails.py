"""`assay evals guardrails`: which evaluators could run as a guardrail, in the request path, and which
should stay where they are: after the fact.

Assay doesn't block or fix anything in production. It measures. An evaluator is a guardrail
candidate when it's fast and cheap enough for the request path, it gives the same verdict for the
same output, and it rarely blocks a good output (a false positive frustrates users). Where letting a
bad output through does harm (medical advice), its false-negative rate matters as much. The
thresholds depend on the stakes, so they're yours to set:

    [guardrails]
    max_ms = 50                 # p95 an evaluator may take in the request path
    max_false_positive = 0.01   # good outputs it may fail
    max_false_negative = 0.05   # bad outputs it may pass (unset: not required)
    min_labeled = 20            # labeled outputs before a rate means anything
    judges = false              # an LLM judge is never a candidate unless this is true

Latency and cost come from recorded results. False positives and negatives come from people's
labels: golden-set items labeled from a recorded run are matched with what each evaluator said of
that same output, and a judge's calibration (its catch rate) is used for the judge it calibrated.
Candidates export to a file for whatever guardrail layer you run; the tests keep checking them.
"""
from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from statistics import median
from typing import Dict, List, Optional

from sqlalchemy import select

from assay import store
from assay.upkeep import is_judge

DEFAULTS = {"max_ms": 50.0, "max_false_positive": 0.01, "max_false_negative": None, "min_labeled": 20,
            "judges": False}


def settings(raw: dict) -> dict:
    g = raw.get("guardrails") or {}
    out = dict(DEFAULTS)
    for k in ("max_ms", "max_false_positive", "max_false_negative"):
        if g.get(k) is not None:
            out[k] = float(g[k])
    if g.get("min_labeled") is not None:
        out["min_labeled"] = int(g["min_labeled"])
    out["judges"] = bool(g.get("judges", False))
    return out


def _p95(xs: List[float]) -> Optional[float]:
    if not xs:
        return None
    xs = sorted(xs)
    return xs[min(len(xs) - 1, math.ceil(0.95 * len(xs)) - 1)]


def _wilson_hi(k: int, n: int, z: float = 1.96) -> Optional[float]:
    if not n:
        return None
    p = k / n
    return min(1.0, (p + z * z / (2 * n) + z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / (1 + z * z / n))


def labeled_errors(rows: List, golden: List[dict], bad_below: float) -> Dict[str, dict]:
    """{field: {"good", "bad", "false_positive", "false_negative"}}: each labeled output (a golden item
    from a recorded run) against what each evaluator said of it in that run."""
    said = defaultdict(dict)  # (run, case) -> {field: failed?}
    for r in rows:
        if r.status in ("pass", "fail"):
            said[(r.run_id, r.case_id)].setdefault(r.field or "result", r.status == "fail")
    out: Dict[str, dict] = defaultdict(lambda: {"good": 0, "bad": 0, "false_positive": 0, "false_negative": 0})
    for x in golden:
        got = said.get((x.get("run"), x["id"]))
        if not got or x.get("label") is None:
            continue
        bad = x["label"] < bad_below
        for f, failed in got.items():
            o = out[f]
            o["bad" if bad else "good"] += 1
            if failed and not bad:
                o["false_positive"] += 1
            elif bad and not failed:
                o["false_negative"] += 1
    return out


def from_calibration(engine, tenant: str) -> Dict[str, dict]:
    """A judge's error rates as its latest calibration measured them: failing good answers (1 - tpr) and
    passing bad ones (1 - tnr)."""
    c = store.calibrations
    with engine.connect() as conn:
        cals = conn.execute(select(c.c.result, c.c.created_at).where(c.c.tenant == tenant).order_by(c.c.created_at)).all()
    out = {}
    for x in cals:
        cal = (x.result or {}).get("calibration") or {}
        ct = cal.get("catch") or {}
        if cal.get("field") and ct:
            good, bad = ct.get("good") or 0, ct.get("bad") or 0
            out[cal["field"]] = {"good": good, "bad": bad,
                                 "false_positive": good - (ct.get("confirmed") or 0),
                                 "false_negative": bad - len(ct.get("caught") or []),
                                 "from": "calibration"}
    return out


def report(engine, tenant: str, days: float, golden: List[dict], bad_below: float, cfg: dict) -> dict:
    t = store.eval_results
    since = datetime.utcnow() - timedelta(days=days)
    with engine.connect() as conn:
        rows = conn.execute(select(t).where((t.c.tenant == tenant) & (t.c.ts >= since))).all()
    by = defaultdict(list)
    for r in rows:
        by[r.field or "result"].append(r)
    errs = labeled_errors(rows, golden, bad_below)
    cal = from_calibration(engine, tenant)
    out = []
    for f, rs in sorted(by.items()):
        judge = any(is_judge(r) for r in rs)
        ms = [r.duration_ms for r in rs if getattr(r, "duration_ms", None) is not None]
        costs = [r.cost_usd for r in rs if getattr(r, "cost_usd", None) is not None]
        e = cal.get(f) if judge and f in cal else errs.get(f)
        e = dict(e) if e else {"good": 0, "bad": 0, "false_positive": 0, "false_negative": 0}
        e.setdefault("from", "labels")
        fp = e["false_positive"] / e["good"] if e["good"] else None
        fn = e["false_negative"] / e["bad"] if e["bad"] else None
        x = {"field": f, "evaluator": sorted({r.evaluator for r in rs if r.evaluator}) or [None], "judge": judge,
             "results": len(rs), "p50_ms": median(ms) if ms else None, "p95_ms": _p95(ms),
             "cost_usd": sum(costs) / len(costs) if costs else (0.0 if not judge else None),
             "labeled_good": e["good"], "labeled_bad": e["bad"], "false_positive": fp, "false_negative": fn,
             "false_positive_hi": _wilson_hi(e["false_positive"], e["good"]), "errors_from": e["from"],
             "example_case": rs[0].case_id}
        x["verdict"], x["why"] = decide(x, cfg)
        out.append(x)
    order = {"candidate": 0, "unknown": 1, "not a candidate": 2}
    return {"days": days, "settings": cfg, "evaluators": sorted(out, key=lambda x: (order[x["verdict"]], x["field"]))}


def decide(x: dict, cfg: dict) -> tuple:
    no, unknown = [], []
    if x["judge"] and not cfg["judges"]:
        no.append("an LLM judge: slow, and not the same verdict every time ([guardrails] judges = true to consider it)")
    if x["p95_ms"] is None:
        unknown.append("its latency isn't recorded")
    elif x["p95_ms"] > cfg["max_ms"]:
        no.append(f"{_ms(x['p95_ms'])} p95, over {_ms(cfg['max_ms'])}")
    if x["labeled_good"] < cfg["min_labeled"]:
        unknown.append(f"{x['labeled_good']} good outputs labeled; {cfg['min_labeled']} needed to tell its false "
                       "positives (assay golden add)")
    elif x["false_positive"] > cfg["max_false_positive"]:
        no.append(f"{x['false_positive']:.0%} false positives, over {cfg['max_false_positive']:.0%}")
    if cfg["max_false_negative"] is not None:
        if x["labeled_bad"] < cfg["min_labeled"]:
            unknown.append(f"{x['labeled_bad']} bad outputs labeled; {cfg['min_labeled']} needed to tell its false negatives")
        elif x["false_negative"] > cfg["max_false_negative"]:
            no.append(f"{x['false_negative']:.0%} false negatives, over {cfg['max_false_negative']:.0%}")
    if no:
        return "not a candidate", "; ".join(no)
    if unknown:
        return "unknown", "; ".join(unknown)
    why = [f"{_ms(x['p95_ms'])} p95", f"{x['false_positive']:.0%} false positives on {x['labeled_good']} good"]
    if x["false_negative"] is not None:
        why.append(f"{x['false_negative']:.0%} false negatives on {x['labeled_bad']} bad")
    return "candidate", ", ".join(why)


def _ms(v: float) -> str:
    return f"{v:.2g} ms" if v < 10 else f"{v:.0f} ms" if v < 1000 else f"{v / 1000:.1f} s"


def text(r: dict) -> str:
    s = r["settings"]
    head = (f"Guardrail candidates, from the last {r['days']:g} days: at most {_ms(s['max_ms'])} p95 and "
            f"{s['max_false_positive']:.0%} false positives" + (f", {s['max_false_negative']:.0%} false negatives"
                                                               if s["max_false_negative"] is not None else "") + ".")
    lines = [head, ""]
    for label in ("candidate", "unknown", "not a candidate"):
        xs = [x for x in r["evaluators"] if x["verdict"] == label]
        if xs:
            lines.append({"candidate": "Could run in the request path:", "unknown": "Can't tell yet:",
                          "not a candidate": "Keep them after the fact:"}[label])
            lines += [f"  {x['field']}: {x['why']}" for x in xs]
            lines.append("")
    if not r["evaluators"]:
        lines.append("No evaluator results in this window: run `assay test` first.")
    lines.append("Assay doesn't block or fix anything; --export writes the candidates for your guardrail layer.")
    return "\n".join(lines)


def cli(root: Path, days: float, fmt: str, export: Optional[str]) -> int:
    from assay import calibrate, local, synth
    engine, _ = local._open(root)
    if engine is None:
        print("No test runs yet: run `assay test` first.", file=sys.stderr)
        return 2
    try:
        cfg = settings(synth.local_toml(root))
    except (TypeError, ValueError) as exc:
        print(f"[guardrails]: {exc}", file=sys.stderr)
        return 2
    golden, bad_below = [], 0.5
    try:
        ccfg = local._calib_cfg(root)
        golden = calibrate.load_golden(root / ccfg["golden"])
        lo, hi = ccfg.get("score_range") or (0, 1)
        thr = ccfg.get("threshold") if ccfg.get("threshold") is not None else (lo + hi) / 2
        bad_below = calibrate._to_labels(thr, (lo, hi), tuple(ccfg.get("label_range") or (lo, hi)))  # in the labels' range
    except Exception:
        pass
    r = report(engine, local.TENANT, days, golden, bad_below, cfg)
    print(json.dumps(r, indent=1, default=str) if fmt == "json" else text(r))
    if export:
        keep = [{k: x[k] for k in ("field", "evaluator", "p50_ms", "p95_ms", "cost_usd", "false_positive",
                                   "false_negative", "labeled_good", "labeled_bad", "example_case")}
                for x in r["evaluators"] if x["verdict"] == "candidate"]
        Path(export).write_text(json.dumps({"generated": datetime.utcnow().isoformat(), "settings": cfg,
                                            "candidates": keep}, indent=1, default=str) + "\n")
        print(f"\n{len(keep)} candidate{'s' * (len(keep) != 1)} written to {export}.")
    return 0
