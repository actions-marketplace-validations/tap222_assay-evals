"""Checking redaction, and checking that edited traces still behave like the real ones.

Redaction tools miss things. `check` scans what was recorded (a local events file, or what the server
stored) for personal data that got through: by kind and by field, with samples masked, and a few
traces picked for a person to look over. What's stored should already be redacted
(assay_sdk.init(redact=...)), so anything found here is a miss.

An edited trace is only useful if the app does the same thing with it. `replay` runs each recorded
input through the app twice: as it was, and with personal data replaced (realistic stand-ins by
default, or placeholders), and compares the two runs with what was recorded: the tools called, in
order, how the run ended, and the answer (stand-ins mapped back). A difference the original input
also shows when run again is the app being nondeterministic, not the edit.
"""
from __future__ import annotations

import json
import random
import re
from collections import defaultdict
from datetime import datetime
from typing import Any, Callable, Dict, Iterable, List, Optional

from assay import learn

SAMPLE = 5
SAME_ANSWER = 0.6  # word overlap at which two answers say the same thing


# ---------- what got through ----------

def _fields(e: dict) -> Iterable[tuple]:
    """(field, value) of an event that can hold personal data."""
    t = e.get("type")
    if t == "run.start":
        yield "input", e.get("input")
    elif t == "step":
        k = e.get("kind") or "step"
        for f in ("args", "result", "text", "error"):
            if e.get(f) is not None:
                yield f"{k}.{f}", e.get(f)
    elif t == "run.end":
        yield "answer", e.get("answer")
    elif t == "feedback":
        yield "feedback.note", e.get("note")
    elif t == "correction":
        yield "correction", [e.get("expected"), e.get("observed")]
    elif t == "claim_review":
        yield "claim_review", [e.get("claim"), e.get("correction"), e.get("note"), e.get("evidence")]


def scan(events: Iterable[dict], sample: int = SAMPLE, seed: str = "redaction") -> dict:
    """Personal data in recorded events: {"events", "runs", "runs_with", "found": [{"kind", "field",
    "count", "samples"}], "sample": [run ids to look over]}. Samples are masked."""
    found: Dict[tuple, dict] = {}
    runs, hit, n = set(), set(), 0
    for e in events:
        n += 1
        rid = e.get("run_id")
        if rid:
            runs.add(rid)
        for f, v in _fields(e):
            for kind, value in learn.pii_matches(v):
                x = found.setdefault((kind, f), {"kind": kind, "field": f, "count": 0, "samples": []})
                x["count"] += 1
                s = learn.pii_sample(value)
                if s not in x["samples"] and len(x["samples"]) < 3:
                    x["samples"].append(s)
                if rid:
                    hit.add(rid)
    rnd = random.Random(seed)
    first = sorted(hit)
    rnd.shuffle(first)
    rest = sorted(runs - hit)
    rnd.shuffle(rest)
    return {"events": n, "runs": len(runs), "runs_with": len(hit),
            "found": sorted(found.values(), key=lambda x: -x["count"]),
            "sample": (first + rest)[:sample]}


def store_events(engine, tenant: str, since: datetime, until: datetime) -> List[dict]:
    """What the server stored for the tenant's runs in the window, as events."""
    from sqlalchemy import and_, select
    from assay import store
    t, st, ti = store.agent_trajectories, store.agent_steps, store.trace_inputs
    out = []
    with engine.connect() as conn:
        heads = [dict(r._mapping) for r in conn.execute(select(t).where(and_(
            t.c.tenant == tenant, t.c.started_at >= since, t.c.started_at < until)))]
        ids = [h["trajectory_id"] for h in heads]
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(select(ti).where(and_(ti.c.tenant == tenant, ti.c.trace_id.in_(chunk)))):
                out.append({"type": "run.start", "run_id": r.trace_id, "input": r.input})
            for r in conn.execute(select(st).where(and_(st.c.tenant == tenant, st.c.trajectory_id.in_(chunk)))):
                out.append({"type": "step", "run_id": r.trajectory_id, "kind": r.kind, "args": r.args,
                            "result": r.result, "text": r.text, "error": r.error})
        for h in heads:
            if h.get("answer"):
                out.append({"type": "run.end", "run_id": h["trajectory_id"], "answer": h["answer"]})
        c = store.claim_reviews
        for r in conn.execute(select(c).where(and_(c.c.tenant == tenant, c.c.ts >= since, c.c.ts < until))):
            out.append({"type": "claim_review", "run_id": r.run_id, "claim": r.claim, "correction": r.correction,
                        "note": r.note, "evidence": r.evidence})
    return out


def read_file(path) -> List[dict]:
    from pathlib import Path
    p = Path(path)
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines() if x.strip()] if p.exists() else []


def text(r: dict, path: str) -> str:
    """A scan's report, for the terminal."""
    out = [f"{path}: {r['events']:,} events in {r['runs']:,} runs."]
    if not r["found"]:
        out.append("No personal data found that the redactor knows how to find. It finds emails, card numbers, "
                   "IBANs, US SSNs, IP addresses, phone numbers, street addresses, and names and birth dates after a "
                   "cue (\"my name is\"); read the sample below for what it doesn't.")
    else:
        out.append(f"Personal data got through in {r['runs_with']:,} run{'s' * (r['runs_with'] != 1)}:")
        for x in r["found"]:
            out.append(f"  {x['kind']:<11} in {x['field']:<18} {x['count']:>5}   e.g. {', '.join(x['samples'])}")
        out.append("Redact before it's recorded: assay_sdk.init(redact=...), e.g. redact=assay.learn.redact.")
    if r["sample"]:
        out.append("\nRead these yourself, since a redactor misses what it has no pattern for: " + ", ".join(r["sample"]))
    return "\n".join(out)


# ---------- do edited traces behave the same? ----------

def runs_in(events: List[dict]) -> List[dict]:
    """Recorded runs: {"run_id", "task", "input", "tools", "status", "answer"}. Test-case runs and
    synthetic ones are left out: only real inputs are worth checking."""
    by: Dict[str, dict] = {}
    for e in events:
        rid = e.get("run_id")
        if not rid:
            continue
        r = by.setdefault(rid, {"run_id": rid, "task": None, "input": None, "tools": [], "status": None,
                                "answer": None, "skip": False})
        t = e.get("type")
        if t == "run.start":
            r["task"], r["input"] = e.get("task"), e.get("input")
            r["skip"] = bool(e.get("test")) or (e.get("tags") or {}).get("origin") == "synthetic"
        elif t == "step" and e.get("kind") == "tool":
            r["tools"].append(e.get("name"))
        elif t == "step" and e.get("kind") == "answer" and e.get("text"):
            r["answer"] = e["text"]
        elif t == "run.end":
            r["status"] = e.get("status")
            r["answer"] = e.get("answer") or r["answer"]
    return [r for r in by.values() if not r["skip"] and r["input"] is not None]


def _words(v: Any) -> set:
    return set(re.findall(r"[a-z0-9]+", str(v or "").lower()))


def _same_answer(a: Any, b: Any) -> bool:
    x, y = _words(a), _words(b)
    return not (x or y) or (bool(x and y) and len(x & y) / len(x | y) >= SAME_ANSWER)


def differences(recorded: dict, got: dict) -> List[str]:
    out = []
    if recorded["tools"] != got["tools"]:
        out.append(f"tools {' → '.join(recorded['tools']) or '(none)'} became {' → '.join(got['tools']) or '(none)'}")
    if (recorded["status"] or "completed") != (got["status"] or "completed"):
        out.append(f"it {recorded['status'] or 'completed'}, now {got['status'] or 'completed'}")
    if not _same_answer(recorded["answer"], got["answer"]):
        out.append("the answer changed")
    return out


def _restore(v: Any, mapping: Dict[str, str]) -> Any:
    """An answer with the stand-ins put back as the originals, to compare with the recorded one."""
    if not isinstance(v, str):
        return v
    back = {fake: orig.split(":", 1)[1] for orig, fake in mapping.items()}
    for fake in sorted(back, key=len, reverse=True):
        v = v.replace(fake, back[fake])
    return v


def call(app: Callable, value: Any, task: Optional[str]) -> dict:
    """The app run on one input, what it did recorded in memory, never sent anywhere."""
    import asyncio
    import inspect
    import assay_sdk as assay
    sent: List[dict] = []
    assay.init(transport=lambda batch: sent.extend(batch), flush_interval=3600)
    status, answer = "completed", None
    with assay.tagged(origin="replay") as started:
        try:
            with assay.run(task or "replay", input=value):  # so tools are recorded even if the app opens no run
                out = app(value)
                if inspect.isawaitable(out):
                    out = asyncio.run(out)
            answer = out if isinstance(out, str) else json.dumps(out, default=str)
        except Exception as exc:
            status, answer = "failed", f"{type(exc).__name__}: {exc}"
    assay.flush()
    tools = [e.get("name") for e in sent if e.get("type") == "step" and e.get("kind") == "tool"]
    return {"tools": tools, "status": status, "answer": answer, "runs": list(started)}


def replay(app: Callable, runs: List[dict], mode: str = "pseudonym", control: bool = True) -> List[dict]:
    """[{"run_id", "kinds", "edited_input", "result": same | changed | unstable, "differences"}] for
    every recorded run whose input holds personal data."""
    out = []
    for r in runs:
        kinds = sorted({k for k, _ in learn.pii_matches(r["input"])})
        if not kinds:
            continue
        if mode == "placeholder":
            edited, mapping = learn.redact(r["input"]), {}
        else:
            edited, mapping = learn.pseudonymize(r["input"])
        got = call(app, edited, r["task"])
        got["answer"] = _restore(got["answer"], mapping)
        diffs = differences(r, got)
        result = "same" if not diffs else "changed"
        if diffs and control:  # the original, run again: is it the edit, or the app?
            again = differences(r, call(app, r["input"], r["task"]))
            if again and set(d.split(" ")[0] for d in again) >= set(d.split(" ")[0] for d in diffs):
                result = "unstable"
        out.append({"run_id": r["run_id"], "kinds": kinds, "edited_input": edited, "result": result,
                    "differences": diffs})
    return out


def replay_text(rows: List[dict], mode: str) -> str:
    if not rows:
        return "No recorded input holds personal data the redactor finds: nothing to replay."
    changed = [x for x in rows if x["result"] == "changed"]
    unstable = [x for x in rows if x["result"] == "unstable"]
    out = [f"{len(rows)} run{'s' * (len(rows) != 1)} with personal data, replayed with "
           f"{'stand-ins' if mode == 'pseudonym' else 'placeholders'}: {len(rows) - len(changed) - len(unstable)} "
           f"behaved the same, {len(changed)} changed, {len(unstable)} vary even unedited."]
    for x in changed:
        out.append(f"  {x['run_id']} ({', '.join(x['kinds'])}): " + "; ".join(x["differences"]))
    if changed:
        out.append("An edited copy of these doesn't test what the real one did: keep more of the input, "
                   + ("or try stand-ins (the default) instead of placeholders." if mode == "placeholder" else
                      "or edit the value the behavior depends on by hand."))
    for x in unstable:
        out.append(f"  {x['run_id']}: differs from the recording even unedited; the check can't tell")
    return "\n".join(out)


# ---------- the command line: assay redact ----------

def cli(root, args) -> int:
    import os
    import sys
    from assay import local, synth
    if args.redact_cmd == "check":
        if args.url or (os.environ.get("ASSAY_URL") and not args.file):
            url = (args.url or os.environ.get("ASSAY_URL")).rstrip("/")
            key = os.environ.get("ASSAY_KEY")
            code, body = local._http("GET", f"{url}/v1/redaction/check?source={args.source}&days={args.days}", None,
                                     {"Authorization": f"Bearer {key}"} if key else {})
            if code != 200:
                print(f"{url} refused ({code}): {body}", file=sys.stderr)
                return 2
            print(text(body, f"{url} ({args.source}, last {args.days:g} days)"))
            return 1 if body["found"] else 0
        path = root / (args.file or os.path.join(".assay", "events.jsonl"))
        if not path.exists():
            print(f"No {path.relative_to(root) if path.is_relative_to(root) else path}: record something first, "
                  "or pass --file or --url.", file=sys.stderr)
            return 2
        r = scan(read_file(path))
        print(text(r, str(path.relative_to(root)) if path.is_relative_to(root) else str(path)))
        return 1 if r["found"] else 0
    if args.redact_cmd == "replay":
        spec = args.app
        if not spec:
            try:
                spec = synth.config(synth.local_toml(root)).app
            except synth.SynthError:
                pass
        try:
            app = synth.load_app(spec, root)
        except synth.SynthError as exc:
            print(str(exc).replace("[synthetic] app", "--app"), file=sys.stderr)
            return 2
        path = root / (args.file or os.path.join(".assay", "events.jsonl"))
        runs = runs_in(read_file(path))
        if args.limit:
            runs = [r for r in runs if learn.pii_matches(r["input"])][:args.limit]
        rows = replay(app, runs, args.mode, control=not args.no_control)
        print(replay_text(rows, args.mode))
        return 1 if any(x["result"] == "changed" for x in rows) else 0
    return 2
