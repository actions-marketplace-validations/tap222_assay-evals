"""Connecting without code: what's arrived, what each feature still needs, spreadsheet import,
and instructions to hand to a developer.

Everything here is worded for someone who doesn't write code: a checklist of what Assay has
received, which features that switches on, and the one next step for each that's missing.
"""
from __future__ import annotations

import csv
import io
import re
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from pydantic import ValidationError
from sqlalchemy import and_, func, select
from sqlalchemy.engine import Engine

from assay import ingest, store

# What arrives, in plain words: (table, time column, label, what it is)
RECORDS = {
    "documents": (store.event_documents, "received_at", "Items processed", "each document, request or job"),
    "stage_runs": (store.event_stage_runs, "started_at", "Steps", "each step of the pipeline on each item"),
    "calls": (store.event_calls, "ts", "AI calls", "each call to an AI model, with cost and speed"),
    "trajectories": (store.agent_trajectories, "started_at", "Agent runs", "each run of an AI agent, step by step"),
    "errors": (store.event_errors, "reported_at", "Corrections", "wrong values someone reported, with the right one"),
    "feedback": (store.trace_feedback, "ts", "User feedback", "thumbs up or down, retries, complaints"),
    "inputs": (store.trace_inputs, "captured_at", "Inputs", "what each item or agent run was given"),
    "eval_results": (store.eval_results, "ts", "Test results", "results from your test runs, passes and failures"),
    "reviews": (store.event_reviews, "ts", "Review time", "time people spent checking or fixing work"),
}

# Each feature: what it needs (any one of a group), and what to do if it's missing.
FEATURES = [
    {"id": "monitoring", "name": "Health, volume and alerts", "tab": "overview",
     "needs": [["documents", "trajectories"]], "next": "Send each item you process (or connect OpenTelemetry)."},
    {"id": "cost", "name": "Cost per item", "tab": "cost",
     "needs": [["calls", "trajectories"]], "next": "Send AI calls with their cost, or the tokens and model."},
    {"id": "workflow", "name": "Pipeline map and rules", "tab": "workflow",
     "needs": [["stage_runs", "trajectories"]], "next": "Send the steps each item goes through."},
    {"id": "errors", "name": "Where wrong answers start", "tab": "errors",
     "needs": [["errors"], ["stage_runs", "trajectories"]],
     "next": "Upload corrections (a spreadsheet of wrong values and the right ones) and send steps."},
    {"id": "agents", "name": "Agent step-by-step checks", "tab": "agents",
     "needs": [["trajectories"]], "next": "Send each agent run with its tool calls (OpenTelemetry works)."},
    {"id": "tests", "name": "Test results, flaky tests and release checks", "tab": "failures",
     "needs": [["eval_results", "trajectories"]], "next": "Upload test results, or have your test tool send them."},
    {"id": "learn", "name": "Find problems nobody reported", "tab": "learn",
     "needs": [["documents", "trajectories"], ["feedback", "errors", "stage_runs", "trajectories"]],
     "next": "Send user feedback (thumbs down, retries) to catch problems nobody reports."},
    {"id": "replay", "name": "Turn production failures into tests", "tab": "learn",
     "needs": [["inputs"]], "next": "Send what each item or agent run was given, so failures can be replayed."},
]


def tenant_of(source: str) -> str:
    return source.split(":", 1)[1] if source.startswith("events:") else source


def status(engine: Engine, source: str) -> dict:
    """What has arrived for a source (total, last 24 hours, last seen), and which features that turns on."""
    tenant = tenant_of(source)
    since = datetime.utcnow() - timedelta(hours=24)
    records = {}
    with engine.connect() as conn:
        for kind, (t, col, label, what) in RECORDS.items():
            c = t.c[col]
            total, last = conn.execute(select(func.count(), func.max(c)).where(t.c.tenant == tenant)).one()
            recent = conn.execute(select(func.count()).where(and_(t.c.tenant == tenant, c >= since))).scalar()
            records[kind] = {"label": label, "what": what, "total": total, "last_24h": recent,
                             "last_seen": last.isoformat() if last else None}
    have = {k for k, v in records.items() if v["total"]}
    features = []
    for f in FEATURES:
        missing = [g for g in f["needs"] if not have & set(g)]
        features.append({"id": f["id"], "name": f["name"], "tab": f["tab"], "ready": not missing,
                         "next": None if not missing else f["next"],
                         "missing": [" or ".join(RECORDS[k][2].lower() for k in g) for g in missing]})
    return {"source": source, "records": records, "features": features,
            "ready": sum(f["ready"] for f in features), "total": len(features),
            "receiving": any(v["last_24h"] for v in records.values())}


# ---------- spreadsheets ----------

# For each record type we accept from a spreadsheet: its fields, and header names that mean them.
SHEETS = {
    "errors": {"label": "Corrections (wrong values and the right ones)", "model": ingest.ErrorEvent,
               "fields": {"document_id": ["document", "doc", "document id", "item", "id", "file"],
                          "field": ["field", "column", "attribute", "name"],
                          "expected": ["expected", "correct", "right", "should be", "truth", "correct value"],
                          "observed": ["observed", "actual", "got", "was", "output", "extracted"],
                          "kind": ["kind", "type of error", "error type"],
                          "reporter": ["reporter", "reviewer", "reported by", "who"],
                          "source": ["source", "channel"], "reported_at": ["reported at", "date", "when", "time"]}},
    "eval_results": {"label": "Test results", "model": ingest.EvalResultEvent,
                     "fields": {"run_id": ["run", "run id", "test run", "batch"], "case_id": ["case", "case id", "test",
                                                                                        "test case", "id"],
                                "field": ["field", "check", "metric"], "expected": ["expected", "correct"],
                                "actual": ["actual", "output", "got"], "status": ["status", "result", "outcome",
                                                                                  "passed", "pass"],
                                "evaluator": ["evaluator", "checker", "grader", "judge"], "score": ["score"],
                                "reason": ["reason", "comment", "notes", "explanation"],
                                "attempt": ["attempt", "try", "repeat"], "document_id": ["document", "trace"],
                                "ts": ["time", "date", "timestamp"]}},
    "feedback": {"label": "User feedback", "model": ingest.FeedbackEvent,
                 "fields": {"trace_id": ["trace", "document", "conversation", "session", "id", "request"],
                            "kind": ["kind", "feedback", "rating", "reaction", "type"],
                            "note": ["note", "comment", "text"], "ts": ["time", "date", "timestamp", "when"]}},
    "documents": {"label": "Items processed", "model": ingest.DocumentEvent,
                  "fields": {"document_id": ["document", "id", "document id", "item", "file"],
                             "received_at": ["received", "received at", "created", "start", "date"],
                             "completed_at": ["completed", "completed at", "finished", "done", "end"],
                             "document_type": ["type", "document type", "category", "task"],
                             "segment": ["segment", "customer", "client", "region", "team"],
                             "status": ["status"], "page_count": ["pages", "page count"]}},
    "inputs": {"label": "Inputs (what each item was given)", "model": ingest.InputEvent,
               "fields": {"trace_id": ["trace", "document", "id", "request", "conversation"],
                          "input": ["input", "request", "message", "question", "prompt", "text"],
                          "input_ref": ["file", "url", "link", "path", "input ref"]}},
}

# What each field is called on screen.
LABELS = {"document_id": "Item ID", "field": "Which value", "expected": "Correct value", "observed": "Value it gave",
          "actual": "Value it gave", "status": "Pass or fail", "run_id": "Test run", "case_id": "Test case",
          "evaluator": "Checked by", "score": "Score", "reason": "Comment", "attempt": "Attempt number",
          "ts": "When", "reported_at": "When", "reporter": "Reported by", "source": "Where it was found",
          "kind": "Kind of problem", "trace_id": "Item or conversation ID", "note": "Comment", "input": "Input",
          "input_ref": "Link to the input", "received_at": "Received", "completed_at": "Finished",
          "document_type": "Type", "segment": "Customer or group", "page_count": "Pages"}

PASS_WORDS = {"pass", "passed", "ok", "true", "yes", "1", "✓", "success", "correct"}
FAIL_WORDS = {"fail", "failed", "false", "no", "0", "✗", "wrong", "incorrect"}
FEEDBACK_WORDS = {"👎": "thumbs_down", "thumbs down": "thumbs_down", "down": "thumbs_down", "bad": "thumbs_down",
                  "negative": "thumbs_down", "-1": "thumbs_down", "👍": "thumbs_up", "thumbs up": "thumbs_up",
                  "up": "thumbs_up", "good": "thumbs_up", "positive": "thumbs_up", "+1": "thumbs_up",
                  "retry": "retry", "retried": "retry", "escalation": "escalation", "escalated": "escalation",
                  "complaint": "complaint"}


def _norm(h: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", h.strip().lower().replace("_", " "))


def read_table(text: str) -> tuple:
    """Headers and rows from CSV or tab-separated text (as pasted from a spreadsheet)."""
    sample = text[:4096]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel_tab if "\t" in sample else csv.excel
    rows = list(csv.reader(io.StringIO(text.lstrip("﻿")), dialect))
    rows = [r for r in rows if any(c.strip() for c in r)]
    return (rows[0], rows[1:]) if rows else ([], [])


def guess(kind: str, headers: List[str]) -> Dict[str, Optional[str]]:
    """Which column is which field, from common header names."""
    fields = SHEETS[kind]["fields"]
    normed = {h: _norm(h) for h in headers}
    out, used = {}, set()
    for field, names in fields.items():
        exact = next((h for h, n in normed.items() if h not in used and (n == _norm(field) or n in names)), None)
        loose = exact or next((h for h, n in normed.items() if h not in used and
                               any(re.search(rf"\b{re.escape(x)}\b", n) for x in names)), None)
        out[field] = loose
        if loose:
            used.add(loose)
    return out


def detect(headers: List[str]) -> str:
    """The record type a sheet most likely holds."""
    return max(SHEETS, key=lambda k: sum(v is not None for v in guess(k, headers).values())
               - (0 if all(guess(k, headers).get(f) for f in _required(k)) else 5))


def _required(kind: str) -> List[str]:
    m = SHEETS[kind]["model"]
    return [n for n, f in m.model_fields.items() if f.is_required()]


def _value(kind: str, field: str, v: str):
    v = v.strip()
    if v == "":
        return None
    if kind == "eval_results" and field == "status":
        low = v.lower()
        return "pass" if low in PASS_WORDS else "fail" if low in FAIL_WORDS else "error" if "error" in low else low
    if kind == "feedback" and field == "kind":
        return FEEDBACK_WORDS.get(v.lower(), v.lower().replace(" ", "_"))
    if field in ("score",):
        return float(v)
    if field in ("attempt", "page_count"):
        return int(float(v))
    if field in ("received_at", "completed_at", "reported_at", "ts"):
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d", "%d/%m/%Y %H:%M",
                    "%d/%m/%Y", "%m/%d/%Y"):
            try:
                return datetime.strptime(v.replace("Z", ""), fmt)
            except ValueError:
                pass
        return datetime.fromisoformat(v.replace("Z", "+00:00"))
    return v


def parse(kind: str, text: str, mapping: Optional[Dict[str, Optional[str]]] = None, defaults: Optional[dict] = None):
    """Rows as validated records, and the rows that couldn't be read (with why, in plain words)."""
    headers, rows = read_table(text)
    mapping = mapping or guess(kind, headers)
    missing = [f for f in _required(kind) if not mapping.get(f) and not (defaults or {}).get(f)]
    if missing:
        return [], [{"row": None, "problem": "No column for " + ", ".join(missing) + ". Pick one for each."}], mapping
    idx = {h: i for i, h in enumerate(headers)}
    model, good, bad = SHEETS[kind]["model"], [], []
    for n, r in enumerate(rows, start=2):
        rec = dict(defaults or {})
        try:
            for field, col in mapping.items():
                if col and col in idx and idx[col] < len(r):
                    val = _value(kind, field, r[idx[col]])
                    if val is not None:
                        rec[field] = val
            good.append(model(**rec))
        except (ValidationError, ValueError) as e:
            msg = e.errors()[0]["msg"] if isinstance(e, ValidationError) else str(e)
            field = e.errors()[0]["loc"][0] if isinstance(e, ValidationError) and e.errors()[0]["loc"] else None
            bad.append({"row": n, "problem": f"{field}: {msg}" if field else msg})
    return good, bad, mapping


def preview(text: str, kind: Optional[str] = None) -> dict:
    headers, rows = read_table(text)
    kind = kind or (detect(headers) if headers else "errors")
    mapping = guess(kind, headers)
    # A test sheet rarely has a run column: the person names the run instead of being told it's missing.
    ask = kind == "eval_results" and not mapping.get("run_id")
    good, bad, mapping = parse(kind, text, mapping, {"run_id": "upload"} if ask else None)
    return {"kind": kind, "label": SHEETS[kind]["label"], "headers": headers, "rows": len(rows),
            "sample": rows[:5], "mapping": mapping, "fields": list(SHEETS[kind]["fields"]),
            "required": _required(kind), "valid": len(good), "problems": bad[:20], "ask_run_name": ask,
            "labels": {f: ("Reaction" if kind == "feedback" and f == "kind" else LABELS.get(f, f)) for f in
                       SHEETS[kind]["fields"]},
            "kinds": {k: v["label"] for k, v in SHEETS.items()}}


def import_sheet(engine: Engine, tenant: str, kind: str, text: str, mapping: Optional[dict] = None,
                 defaults: Optional[dict] = None) -> dict:
    good, bad, mapping = parse(kind, text, mapping, defaults)
    n = ingest.write(engine, kind, good, tenant) if good else 0
    return {"kind": kind, "imported": n, "skipped": len(bad), "problems": bad[:20], "mapping": mapping}


# ---------- handing off to a developer ----------

def handoff(base_url: str, source: str, key: Optional[str], method: str) -> str:
    """Plain instructions to paste into an email or ticket for whoever will wire it up."""
    tenant = tenant_of(source)
    key_line = f"API key: {key}" if key else "API key: (create one under Connect → Keys, scope: ingest)"
    common = (f"Hi! We're connecting our system to Assay so we can track quality, cost and failures.\n\n"
              f"Assay address: {base_url}\nSource name: {source}\n{key_line}\n\n")
    steps = {
        "otel": ("We already use OpenTelemetry, so no code changes: add this exporter to the collector config.\n\n"
                 "exporters:\n  otlphttp/assay:\n    endpoint: {url}/v1/otlp\n    encoding: json\n"
                 "    headers: {{ Authorization: \"Bearer <API key>\" }}\n"
                 "service:\n  pipelines:\n    traces: {{ exporters: [otlphttp/assay] }}\n\n"
                 "Agents: standard gen_ai tool spans become agent steps. Add assay.answer, and for tests\n"
                 "assay.run_id / assay.case_id, on the root span."),
        "python": ("Install the SDK (standard library only): pip install assay-evals. Then:\n\n"
                   "import assay_sdk as assay\nassay.init(\"{url}\", key=\"<API key>\")\n\n"
                   "with assay.run(\"refund_request\", input=message) as run:     # one run of the system\n"
                   "    run.llm(model=\"claude-sonnet-5\", cost_usd=0.002)\n"
                   "    order = run.call(\"get_order\", get_order, order_id=oid)  # tool calls\n"
                   "    run.answer(reply)\n"
                   "assay.feedback(run.id, \"thumbs_down\")                      # when a user reacts\n\n"
                   "Pipelines: assay.run(task, kind=\"pipeline\") with run.stage(name). Tests: assay.check(...).\n"
                   "Schema: {url}/v1/schema, and docs/event-schema.md in the repo."),
        "http": ("POST events to {url}/v1/ingest with header Authorization: Bearer <API key>.\n"
                 "Each event is JSON in the Assay event schema v1 ({url}/v1/schema): run.start, step,\n"
                 "run.end, feedback, check, correction, expect. Events carry an id, so retries are safe.\n"
                 "Full reference: {url}/docs"),
        "database": ("Give Assay read-only access to the pipeline database: set ASSAY_SOURCE_URL and a\n"
                     "mapping file (see mappings/example.json), then run: python -m assay check-source"),
    }
    return common + steps.get(method, steps["http"]).format(url=base_url.rstrip("/")) + \
        f"\n\nWhen data arrives, the checklist under Connect in {base_url} turns green."
