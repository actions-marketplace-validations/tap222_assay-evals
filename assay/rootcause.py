"""Where did a wrong output go wrong? Localize an error to a pipeline step.

Given a reported error (document, field, correct value) and what each step of
that document's run produced, walk the steps in order and classify:

  introduced  the first step that produced the field got it wrong, although
              the correct value was available in an earlier step's text
  corrupted   the field was correct after one step and a later step changed it
  dropped     the field was present (and right) and a later step lost it
  after       every step that records the field has it right, yet the output
              was wrong: it went wrong after the pipeline (delivery, mapping,
              or the downstream system)
  upstream    the correct value never appears in any step's output or text:
              the input, or the text step (OCR), is the likely cause
  caused_by   the step that got it wrong had its own input already wrong:
              another reported error on this document sits at an earlier step
  unlocalized no step records this field, so there's nothing to compare

It also collects what else was unusual at the origin step (a failure, a
stage that did nothing, a fallback model, a declared/served mismatch), which
is usually the first thing an engineer checks.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from assay.models import FAILED_STATUSES, ErrorReport, StageRun, Window

TEXT_MIN = 120  # a string output at least this long counts as text evidence

VERDICTS = {
    "introduced": "Introduced",
    "corrupted": "Corrupted",
    "dropped": "Dropped",
    "after": "After the pipeline",
    "upstream": "Input / upstream",
    "caused_by": "Caused by an earlier error",
    "unlocalized": "Not localized",
}

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}


def normalize(v: Any) -> Optional[str]:
    """Canonical form for comparing values: case, spacing, currency and
    thousands separators, and common date formats don't count as differences."""
    if v is None:
        return None
    s = str(v).strip().lower()
    s = re.sub(r"\s+", " ", s)
    num = re.sub(r"[\s,$€£¥]|usd|eur|gbp", "", s)
    if re.fullmatch(r"-?\d+(\.\d+)?", num):
        return repr(round(float(num), 6))
    m = re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})(?:[t ].*)?", s)
    if m:
        return f"{int(m[1]):04d}-{int(m[2]):02d}-{int(m[3]):02d}"
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)  # US style M/D/YYYY
    if m:
        return f"{int(m[3]):04d}-{int(m[1]):02d}-{int(m[2]):02d}"
    m = re.fullmatch(r"(\d{1,2}) ([a-z]{3})[a-z]* (\d{4})", s)
    if m and m[2] in _MONTHS:
        return f"{int(m[3]):04d}-{_MONTHS[m[2]]:02d}-{int(m[1]):02d}"
    m = re.fullmatch(r"([a-z]{3})[a-z]* (\d{1,2}),? (\d{4})", s)
    if m and m[1] in _MONTHS:
        return f"{int(m[3]):04d}-{_MONTHS[m[1]]:02d}-{int(m[2]):02d}"
    return s


def _text_forms(expected: str) -> List[str]:
    """Ways the correct value might be written in raw text."""
    raw = str(expected).strip().lower()
    forms = {raw, re.sub(r"\s+", " ", raw)}
    n = normalize(expected)
    if n and re.fullmatch(r"-?\d+(\.\d+)?", n):
        f = float(n)
        forms |= {f"{f:,.2f}", f"{f:.2f}"}
        if f == int(f):
            forms |= {f"{int(f):,}", str(int(f))}
    elif n and re.fullmatch(r"\d{4}-\d{2}-\d{2}", n):  # a date: the ways it's usually written on a page
        d = datetime.strptime(n, "%Y-%m-%d")
        forms |= {d.strftime(f).lower() for f in ("%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y",
                                                   "%m/%d/%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d")}
        forms |= {f"{d.day} {d.strftime('%b %Y').lower()}", f"{d.strftime('%b').lower()} {d.day}, {d.year}"}
    return [x for x in forms if len(x) >= 2]


def transcribed(value: Any) -> bool:
    """Is this the kind of value copied from the page (amounts, dates, ids, names),
    as opposed to a label a step decides (invoice, approved, high_risk)? Only
    transcribed values can be checked against the text; a label is judged
    at the step that decided it."""
    s = str(value or "").strip()
    return bool(re.search(r"\d", s)) or len(s.split()) >= 2


def lookup(outputs: Optional[dict], path: str) -> Tuple[bool, Any]:
    """(present, value) for a dotted path like 'line_items.0.total'."""
    cur: Any = outputs
    if cur is None:
        return False, None
    for part in path.split("."):
        if isinstance(cur, dict) and part in cur:
            cur = cur[part]
        elif isinstance(cur, list) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return False, None
    return True, cur


def _texts(outputs: Optional[dict]) -> List[str]:
    out = []

    def walk(v):
        if isinstance(v, str) and len(v) >= TEXT_MIN:
            out.append(v.lower())
        elif isinstance(v, dict):
            for x in v.values():
                walk(x)
        elif isinstance(v, list):
            for x in v:
                walk(x)
    walk(outputs or {})
    return out


def order_steps(runs: List[StageRun]) -> List[StageRun]:
    return sorted(runs, key=lambda r: (r.sequence is None, r.sequence if r.sequence is not None else 0,
                                       r.started_at or datetime.min))


def _signals(step: StageRun, calls) -> List[str]:
    out = []
    if step.status in FAILED_STATUSES:
        out.append(f"the step itself failed ({step.status})")
    if step.did_work is False:
        out.append("the step reported success without doing any work")
    for c in calls:
        if c.stage != step.stage:
            continue
        if c.model_declared and c.model_served and c.model_declared != c.model_served:
            out.append(f"its model call was served by {c.model_served} instead of {c.model_declared}")
        if c.resolving_layer and c.resolving_layer.lower() not in ("primary", "tier_0", "0", "default"):
            out.append(f"a fallback tier answered ({c.resolving_layer}" + (f": {c.gate_reason})" if c.gate_reason else ")"))
        if c.status in FAILED_STATUSES:
            out.append(f"a model call returned {c.status}")
    return list(dict.fromkeys(out))


def localize(error: ErrorReport, runs: List[StageRun], calls=(),
             other_origins: Optional[Dict[str, Tuple[Optional[int], bool]]] = None) -> dict:
    """Classify where `error` entered the pipeline. `other_origins` maps other
    reported fields on this document to (step index they were localized to,
    whether that field is a label decision)."""
    steps = order_steps(runs)
    expected = normalize(error.expected)
    # Text evidence only means something for values copied from the page.
    forms = _text_forms(error.expected) if error.expected and transcribed(error.expected) else []
    timeline = []
    for i, s in enumerate(steps):
        present, raw = lookup(s.outputs, error.field)
        if present and not isinstance(raw, (dict, list)):
            state = ("extra" if error.kind == "extra"
                     else "correct" if expected is not None and normalize(raw) == expected else "wrong")
        else:
            state = "absent"
        evidence = bool(forms) and any(f in t for t in _texts(s.outputs) for f in forms)
        timeline.append({"index": i, "stage": s.stage, "state": state, "value": None if not present else raw,
                         "evidence": evidence, "status": s.status, "records_outputs": s.outputs is not None,
                         "sequence": s.sequence})

    present_idx = [t["index"] for t in timeline if t["state"] != "absent"]
    correct_idx = [t["index"] for t in timeline if t["state"] == "correct"]
    evidence_idx = [t["index"] for t in timeline if t["evidence"]]
    verdict, origin, why = "unlocalized", None, ""

    if error.kind == "extra":
        if present_idx:
            verdict, origin = "introduced", present_idx[0]
            why = f"{steps[origin].stage} is the first step that output {error.field}, which shouldn't exist."
    elif correct_idx:
        last_ok = correct_idx[-1]
        later = timeline[last_ok + 1:]
        wrong_after = next((t for t in later if t["state"] == "wrong"), None)
        # A later step that simply doesn't output the field isn't evidence of loss,
        # unless the report says the value is missing from the output.
        dropped_at = next((t for t in later if t["state"] == "absent" and t["records_outputs"]), None)
        if wrong_after:
            verdict, origin = "corrupted", wrong_after["index"]
            why = (f"{error.field} was correct after {steps[last_ok].stage} and {steps[origin].stage} changed it "
                   f"to {wrong_after['value']!r}.")
        elif error.kind == "missing" and dropped_at:
            verdict, origin = "dropped", dropped_at["index"]
            why = f"{error.field} was correct after {steps[last_ok].stage}; {steps[origin].stage} no longer has it."
        else:
            verdict, origin = "after", None
            why = (f"Every step that records {error.field} has it right (last: {steps[last_ok].stage}), so it went "
                   "wrong after the pipeline: in delivery, field mapping, or the downstream system.")
    elif present_idx:
        first = present_idx[0]
        if evidence_idx and evidence_idx[0] <= first:
            verdict, origin = "introduced", first
            why = (f"The correct value is in {steps[evidence_idx[0]].stage}'s text, and {steps[first].stage} "
                   f"is the first step to produce {error.field}, as {timeline[first]['value']!r}.")
        elif forms and any(_texts(s.outputs) for s in steps[:first + 1]):
            text_steps = [i for i, s in enumerate(steps[:first + 1]) if _texts(s.outputs)]
            verdict, origin = "upstream", text_steps[0]
            why = (f"The correct value doesn't appear in {steps[origin].stage}'s text, so {steps[first].stage} "
                   "never had it to extract. Check the source document or the text step.")
        else:
            verdict, origin = "introduced", first
            why = (f"{steps[first].stage} is the first step to produce {error.field}, as "
                   f"{timeline[first]['value']!r}." + ("" if not forms else
                                                       " No step records text to check whether the input had it."))
    elif error.kind == "missing" or error.expected:
        if evidence_idx:
            nxt = next((t["index"] for t in timeline[evidence_idx[0] + 1:] if t["records_outputs"]), None)
            verdict, origin = "introduced", nxt
            why = (f"The value is in {steps[evidence_idx[0]].stage}'s text but no step ever extracted "
                   f"{error.field}" + (f"; {steps[nxt].stage} is the first step after it." if nxt is not None else "."))
        elif forms and any(_texts(s.outputs) for s in steps):
            origin = next(i for i, s in enumerate(steps) if _texts(s.outputs))
            verdict = "upstream"
            why = (f"No step ever produced {error.field}, and the value isn't in {steps[origin].stage}'s text: "
                   "check the source document or the text step.")
        else:
            why = f"No step records {error.field} or any text, so there's nothing to compare against."
    else:
        why = f"No step records {error.field}."

    # A wrong *decision* earlier on this document (a label such as the document
    # type, which steers later steps) is the more likely root cause. A wrong
    # transcribed value (an amount, a date) rarely breaks a different field.
    if origin is not None and other_origins:
        earlier = {f: i for f, (i, label) in other_origins.items()
                   if f != error.field and i is not None and i < origin and label}
        if earlier:
            f, i = min(earlier.items(), key=lambda kv: kv[1])
            why += f" {steps[i].stage} had already got {f} wrong on this document, which likely caused this."
            return _result(error, steps, timeline, "caused_by", origin, why, calls, caused_by={"field": f, "stage": steps[i].stage})
    return _result(error, steps, timeline, verdict, origin, why, calls)


def _result(error, steps, timeline, verdict, origin, why, calls, caused_by=None):
    return {"error_id": error.error_id, "document_id": error.document_id, "field": error.field,
            "expected": error.expected, "observed": error.observed, "kind": error.kind,
            "reported_at": error.reported_at.isoformat() if error.reported_at else None,
            "reporter": error.reporter, "source": error.source,
            "verdict": verdict, "verdict_label": VERDICTS[verdict],
            "origin_stage": steps[origin].stage if origin is not None else None, "origin_index": origin,
            "explanation": why, "caused_by": caused_by,
            "signals": _signals(steps[origin], calls) if origin is not None else [],
            "timeline": timeline}


def analyze_document(source, document_id: str, errors: Optional[List[ErrorReport]] = None,
                     detail=None) -> Optional[dict]:
    """Localize every reported error on one document, resolving cross-field causes."""
    detail = detail or source.document_detail(document_id)
    if detail is None:
        return None
    doc, runs, calls = detail
    if errors is None:
        errors = source.errors(None, document_id) if hasattr(source, "errors") else None
    errors = errors or []
    first_pass = {e.field: (localize(e, runs, calls)["origin_index"], not transcribed(e.expected)) for e in errors}
    results = [localize(e, runs, calls, first_pass) for e in errors]
    steps = order_steps(runs)
    fields = sorted({k for s in steps for k in (s.outputs or {}) if not (isinstance((s.outputs or {}).get(k), str)
                                                                          and len(s.outputs[k]) >= TEXT_MIN)})
    lineage = [{"field": f, "values": [lookup(s.outputs, f)[1] if lookup(s.outputs, f)[0] else None for s in steps]}
               for f in fields]
    return {"document": doc, "steps": [{"stage": s.stage, "status": s.status, "sequence": s.sequence,
                                        "records_outputs": s.outputs is not None,
                                        "text": (_texts(s.outputs) or [None])[0]} for s in steps],
            "lineage": lineage, "errors": results}


def summarize(source, window: Window, limit: int = 1000) -> Optional[dict]:
    """Where errors come from across many documents."""
    errors = source.errors(window) if hasattr(source, "errors") else None
    if errors is None:
        return None
    errors = errors[-limit:]
    by_doc = defaultdict(list)
    for e in errors:
        by_doc[e.document_id].append(e)
    docs = {d.document_id: d for d in (source.documents(window) or [])}
    results, model_at_origin = [], Counter()
    # One bulk lookup instead of one per document, where the source supports it.
    bulk = source.document_details(list(by_doc)) if hasattr(source, "document_details") else None
    positions = defaultdict(list)  # stage -> its position in each document's run, for pipeline order
    for doc_id, errs in by_doc.items():
        detail = bulk.get(doc_id) if bulk is not None else source.document_detail(doc_id)
        if detail:
            for i, r in enumerate(order_steps(detail[1])):
                positions[r.stage].append(i)
        if detail is None:
            results += [localize(e, []) | {"document_type": None, "segment": None} for e in errs]
            continue
        a = analyze_document(source, doc_id, errs, detail=detail)
        d, calls = a["document"], detail[2]
        for r in a["errors"]:
            results.append(r | {"document_type": d.document_type, "segment": d.segment})
            if r["origin_stage"]:  # which model answered at the step where it went wrong
                served = {c.model_served for c in calls if c.stage == r["origin_stage"] and c.model_served}
                for m in served or {"(no model call)"}:
                    model_at_origin[(r["origin_stage"], m)] += 1

    def count(key):
        return Counter(r[key] or "(none)" for r in results).most_common()

    stages = sorted(positions, key=lambda st: sum(positions[st]) / len(positions[st]))
    return {"errors": len(results), "documents_with_errors": len(by_doc), "stages": stages,
            "by_origin_and_verdict": [{"stage": st, "verdict": v, "errors": n} for (st, v), n in
                                      Counter((r["origin_stage"], r["verdict"]) for r in results).most_common()],
            "documents_in_window": len(docs) or None,
            "by_origin_stage": count("origin_stage"), "by_verdict": count("verdict"),
            "by_field": count("field"), "by_document_type": count("document_type"), "by_segment": count("segment"),
            "by_origin_model": [{"stage": s, "model": m, "errors": n} for (s, m), n in model_at_origin.most_common()],
            "recent": sorted(results, key=lambda r: r["reported_at"] or "", reverse=True)[:100]}
