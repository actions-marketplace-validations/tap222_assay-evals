"""`assay report`: what the evaluation found, told as findings rather than metrics.

Don't sell a team on evals; show them what was found in the data. For a week (or a month):

  caught by tests     regressions the tests caught and that were fixed before a later run passed:
                      what broke, what fixed it (the commit, the prompt or model version, the input
                      that changed between the failing run and the passing one), and how serious.
                      "We caught 47 issues before users saw them" is this count.
  in production       the top failure modes: categories from reading conversations
                      (assay/review.py) and patterns from the traces (assay/learn.py), each with
                      its share now and before, the high-impact ones first
  fixed               patterns that stopped showing up once tests guarded them, those that came
                      back, and categories whose share fell
  surprising          new kinds of task, new failure categories, rare paths
  the log             a running record: every finding above as it happened, and the notes people
                      add (assay log add "..."), kept across reports

Markdown, for a PR, a wiki or a channel; POST /v1/report/send posts it to the alert webhook.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store

HIGH = ("safety", "pii", "injection", "max_fragments", "max_context_tokens", "max_fixed_context_tokens")


def _severity(field: str) -> str:
    return "HIGH" if field in HIGH or field.startswith("expect.must_get_approval") else "MEDIUM"


# ---------- caught by tests ----------

def caught(engine: Engine, tenant: str, since: datetime, until: datetime,
           revisions: Optional[Dict[str, dict]] = None) -> List[dict]:
    """Checks that passed, then failed in a run (a regression caught), with the run that passed again
    and what changed between them: [{"case", "field", "caught_at", "fixed_at", "reason", "fix"}]."""
    t = store.eval_results
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.run_id != "baseline",
                                                 t.c.status.in_(("pass", "fail"))))).all()
    by: Dict[tuple, Dict[str, list]] = defaultdict(lambda: defaultdict(list))
    for r in rows:
        by[(r.case_id, r.field or "result")][r.run_id].append(r)
    out = []
    for (case, field), runs in by.items():
        order = sorted(runs.items(), key=lambda kv: max(r.ts for r in kv[1]))
        passed_before, open_ = False, None
        for rid, rs in order:
            ok = all(r.status == "pass" for r in rs)
            ts = max(r.ts for r in rs)
            if not ok and passed_before and open_ is None:
                bad = next(r for r in rs if r.status == "fail")
                open_ = {"case": case, "field": field, "caught_run": rid, "caught_at": ts, "rows": rs,
                         "reason": bad.reason or (f"expected {bad.expected}, got {bad.actual}" if bad.expected else "failed")}
            elif ok and open_ is not None:
                open_.update(fixed_run=rid, fixed_at=ts, fix=_fix(engine, tenant, open_["rows"], rs, revisions,
                                                                  open_["caught_run"], rid))
                out.append(open_)
                open_ = None
            if ok:
                passed_before = True
        if open_ is not None:
            out.append({**open_, "fixed_run": None, "fixed_at": None, "fix": []})
    keep = [x for x in out if since <= x["caught_at"] < until or (x["fixed_at"] and since <= x["fixed_at"] < until)]
    for x in keep:
        x.pop("rows", None)
        x["severity"] = _severity(x["field"])
    return sorted(keep, key=lambda x: (x["fixed_at"] is None, x["severity"] != "HIGH", x["caught_at"]))


def _fix(engine: Engine, tenant: str, bad_rows, good_rows, revisions, bad_run: str, good_run: str) -> List[str]:
    """What changed between the failing run and the one that passed: the commit, and what the case ran."""
    from assay import local
    out = []
    revs = revisions or {}
    a, b = revs.get(bad_run) or {}, revs.get(good_run) or {}
    if a.get("commit") and b.get("commit") and a["commit"] != b["commit"]:
        out.append(f"commit {a['commit'][:7]} → {b['commit'][:7]}")
    elif a.get("commit") and a.get("commit") == b.get("commit") and a.get("changes") != b.get("changes"):
        out.append("uncommitted changes")
    la, lb = (bad_rows[0].lineage or {}), (good_rows[0].lineage or {})  # what the results say produced them
    out += [f"{k} {la.get(k)} → {lb.get(k)}" for k in sorted(set(la) | set(lb)) if la.get(k) != lb.get(k)]
    try:
        s = local.setup_changes(engine, tenant, list(good_rows), list(bad_rows))
        out += [local.change_text(x) for x in s["everywhere"] + [x for chs in s["cases"].values() for x in chs]]
    except Exception:  # a case with no recorded model calls: the commit is what's known
        pass
    return out


# ---------- production ----------

def production(engine: Engine, source, tenant: str, since: datetime, until: datetime) -> dict:
    from assay import learn, review
    from assay.models import Window
    cats = [c for c in review.categories(engine, tenant, now=until) if c["status"] in ("open", "confirmed")]
    try:
        pats = [p for p in learn.patterns(source, Window(since, until), engine, update_log=False)["patterns"]
                if p.get("kind", "failure") == "failure" and p.get("status") != "dismissed"]
    except Exception:
        pats = []
    t = store.pattern_log
    with engine.connect() as conn:
        log = [dict(r._mapping) for r in conn.execute(select(t).where(t.c.source == source.name))] if source else []
    fixed = [x for x in log if x.get("fixed_at") and since <= x["fixed_at"] < until]
    recurred = [x for x in log if x.get("recurred_at") and since <= x["recurred_at"] < until]
    c = store.review_categories
    with engine.connect() as conn:
        new_cats = [r.name for r in conn.execute(select(c.c.name).where(and_(
            c.c.tenant == tenant, c.c.created_at >= since, c.c.created_at < until)))]
    at = store.agent_trajectories
    with engine.connect() as conn:
        firsts = conn.execute(select(at.c.task, at.c.started_at).where(and_(at.c.tenant == tenant, at.c.run_id.is_(None),
                                                                            at.c.task.is_not(None)))).all()
    first_seen: Dict[str, datetime] = {}
    for task, ts in firsts:
        first_seen[task] = min(first_seen.get(task, ts), ts)
    new_tasks = sorted(k for k, v in first_seen.items() if since <= v < until)
    rare = [p for p in pats if p.get("type") == "rare_path"]
    return {"categories": cats[:8], "patterns": sorted(pats, key=lambda p: -p["traces"])[:8],
            "fixed": fixed, "recurred": recurred, "falling": [x for x in cats if x["share"] is not None and
                                                              x["share_before"] and x["share"] < x["share_before"] / 2],
            "new_categories": new_cats, "new_tasks": new_tasks, "rare_paths": rare,
            "saturation": review.saturation(engine, tenant)}


# ---------- the log ----------

def note(engine: Engine, tenant: str, text: str, by: Optional[str] = None, key: Optional[str] = None,
         kind: str = "note", when: Optional[datetime] = None) -> bool:
    """Add to the running log; with a key, only once."""
    t = store.report_log
    with engine.begin() as conn:
        if key and conn.execute(select(t.c.id).where(and_(t.c.tenant == tenant, t.c.key == key))).first():
            return False
        conn.execute(t.insert().values(tenant=tenant, ts=when or datetime.utcnow(), kind=kind, text=text[:2000],
                                       by=by, key=key))
    return True


def log(engine: Engine, tenant: str, since: datetime, until: datetime) -> List[dict]:
    t = store.report_log
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(t).where(and_(
            t.c.tenant == tenant, t.c.ts >= since, t.c.ts < until)).order_by(t.c.ts))]


# ---------- the report ----------

def build(engine: Engine, tenant: str, days: float = 7, source=None, revisions: Optional[Dict[str, dict]] = None,
          now: Optional[datetime] = None) -> dict:
    now = now or datetime.utcnow()
    since = now - timedelta(days=days)
    found = caught(engine, tenant, since, now, revisions)
    prod = production(engine, source, tenant, since, now) if source is not None else None
    for x in found:  # the log keeps what was found, as it was found
        what = f"{x['case']} {x['field']}: {x['reason'][:200]}"
        note(engine, tenant, f"Caught by tests: {what}", key=f"caught:{x['case']}:{x['field']}:{x['caught_run']}",
             kind="caught", when=x["caught_at"])
        if x["fixed_at"]:
            note(engine, tenant, f"Fixed: {x['case']} {x['field']}" + (f" ({'; '.join(x['fix'])})" if x["fix"] else ""),
                 key=f"fixed:{x['case']}:{x['field']}:{x['fixed_run']}", kind="fixed", when=x["fixed_at"])
    if prod:
        for p in prod["fixed"]:
            note(engine, tenant, f"Stopped seeing “{p['name']}” in production once tests guarded it",
                 key=f"pattern-fixed:{p['key']}:{p['fixed_at']}", kind="fixed", when=p["fixed_at"])
        for name in prod["new_categories"]:
            note(engine, tenant, f"New failure mode found reading conversations: {name}",
                 key=f"category:{tenant}:{name}", kind="found")
    return {"since": since, "until": now, "caught": found, "production": prod, "log": log(engine, tenant, since, now)}


def _pct(v: Optional[float]) -> str:
    return "–" if v is None else f"{v:.0%}"


def markdown(r: dict) -> str:
    from assay.local import _code, _md
    fixed = [x for x in r["caught"] if x["fixed_at"]]
    open_ = [x for x in r["caught"] if not x["fixed_at"]]
    out = [f"# What we found: {r['since']:%b %d} – {r['until']:%b %d}", ""]
    out += [f"**{len(r['caught'])} issue{'s' * (len(r['caught']) != 1)} caught by tests before users saw them**"
            f" ({len(fixed)} fixed, {len(open_)} still open)", ""]
    for x in (fixed + open_)[:15]:
        line = f"- **{x['severity']}** {_code(x['case'])} {_md(x['field'])}: {_md(x['reason'][:160])}"
        line += f" — fixed {x['fixed_at']:%b %d}" + (f" by {_md('; '.join(x['fix']))}" if x["fix"] else "") \
            if x["fixed_at"] else " — still failing"
        out.append(line)
    if len(r["caught"]) > 15:
        out.append(f"- …and {len(r['caught']) - 15} more")
    p = r.get("production")
    if p:
        out += ["", "## Failure modes in production", ""]
        rows = [(c["name"], "reading conversations", c["share"], c["share_before"]) for c in p["categories"]]
        rows += [(x["name"], f"{x['traces']:,} traces", None, None) for x in p["patterns"]]
        if rows:
            out += ["| Failure mode | Found by | Share now | Before |", "|---|---|---|---|"]
            out += [f"| {_md(n)} | {_md(by)} | {_pct(a)} | {_pct(b)} |" for n, by, a, b in rows]
        else:
            out.append("Nothing found this period.")
        sat = p.get("saturation") or {}
        if sat.get("reviewed"):
            out += ["", f"People read {sat['reviewed']} conversation{'s' * (sat['reviewed'] != 1)} "
                        f"(a pool of {sat['pool']} is a useful round). " + (
                        f"The last {sat['window']} found no new failure mode: saturated." if sat["saturated"] else
                        f"The last {sat['window']}: {sat['new_modes']} new failure mode{'s' * (sat['new_modes'] != 1)}, "
                        f"{sat['changed']} changed, {sat['ungrouped']} not grouped yet: keep reading.")]
        good = [f"- “{_md(x['name'])}”: no longer seen in production since {x['fixed_at']:%b %d}" for x in p["fixed"]]
        good += [f"- {_md(c['name'])}: {_pct(c['share_before'])} → {_pct(c['share'])} of conversations"
                 for c in p["falling"]]
        back = [f"- “{_md(x['name'])}” came back on {x['recurred_at']:%b %d}" for x in p["recurred"]]
        if good or back:
            out += ["", "## Fixed, and staying fixed", "", *good, *back]
        odd = [f"- A new kind of task: {_md(t)}" for t in p["new_tasks"]]
        odd += [f"- A new failure mode: {_md(n)}" for n in p["new_categories"]]
        odd += [f"- {_md(x['name'])} ({x['traces']:,} traces)" for x in p["rare_paths"]]
        if odd:
            out += ["", "## Surprising usage", "", *odd]
    if r["log"]:
        out += ["", "## The log", ""]
        out += [f"- {x['ts']:%b %d}: {_md(x['text'])}" + (f" ({_md(x['by'])})" if x.get("by") else "") for x in r["log"]]
    return "\n".join(out) + "\n"


def as_json(r: dict) -> dict:
    iso = lambda v: v.isoformat() if isinstance(v, datetime) else v
    clean = lambda d: {k: iso(v) for k, v in d.items()}
    p = r.get("production")
    return {"since": iso(r["since"]), "until": iso(r["until"]), "caught": [clean(x) for x in r["caught"]],
            "production": None if p is None else {
                "categories": p["categories"], "patterns": [clean(x) for x in p["patterns"]],
                "fixed": [clean(x) for x in p["fixed"]], "recurred": [clean(x) for x in p["recurred"]],
                "new_categories": p["new_categories"], "new_tasks": p["new_tasks"],
                "saturation": p.get("saturation")},
            "log": [clean(x) for x in r["log"]]}
