"""Evaluator inputs, checked against the trace: was the judge given the right data?

A judge that was handed the wrong thing still returns a valid-looking score:

  - {{generation}} filled with the trace's input, so the "output" it grades is
    really the user's request;
  - {{query}} and {{generation}} both filled with the same text;
  - a template variable nobody filled in;
  - the evaluator's own instructions sent as the user's message, so the judge
    confuses the rubric with the request;
  - {{context}} filled with the request, with another run's documents, or with
    nothing, so a faithfulness judge checks the answer against the wrong sources.

A check can say what its evaluator saw (`inputs`, by role). This compares that
with what the run recorded: its input, its answer and its model outputs, and for
the context, what its tools returned. A result
with a finding isn't evidence about the AI, pass or fail: failure causes and
release calls leave it out (flaky.ROLES["evaluator_input"]).

Roles, with the names judges commonly use for them:
  query         the request:         query, question, input, prompt, user_input
  output        what's being graded: output, generation, response, answer, completion
  context       what it may use:     context, contexts, documents, retrieved, tool_results
  expected      the reference:       expected, reference, ground_truth, ideal
  instructions  the rubric:          instructions, rubric, criteria, evaluator_prompt
  messages      the judge call:      messages ([{"role", "content"}])
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from typing import Any, Dict, Iterable, List, Optional

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store

ROLES = {
    "query": ("query", "question", "input", "prompt", "user_input"),
    "output": ("output", "generation", "response", "answer", "completion", "prediction"),
    "context": ("context", "contexts", "documents", "retrieved", "tool_results"),
    "expected": ("expected", "reference", "ground_truth", "ideal"),
    "instructions": ("instructions", "rubric", "criteria", "evaluator_prompt"),
    "messages": ("messages",),
}
TEMPLATE = re.compile(r"\{\{\s*[\w.]+\s*\}\}|\$\{[\w.]+\}")
MIN_TEXT = 12  # shorter texts match each other by chance
SHOW = 80


def _text(v: Any) -> str:
    if v is None:
        return ""
    return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, sort_keys=True, default=str)


def _flat(v: Any) -> str:
    """The text in a value: a structured input's string values, not its JSON punctuation."""
    if isinstance(v, dict):
        return " ".join(_flat(x) for x in v.values())
    if isinstance(v, (list, tuple)):
        return " ".join(_flat(x) for x in v)
    return _text(v)


def _norm(v: Any) -> str:
    return re.sub(r"\s+", " ", _flat(v)).strip().lower()


def _show(v: Any) -> str:
    t = re.sub(r"\s+", " ", _flat(v)).strip()
    return f"“{t[:SHOW]}{'…' if len(t) > SHOW else ''}”"


def roles(inputs: Dict[str, Any]) -> Dict[str, Any]:
    """{role: value} from whatever names the evaluator used; the first name present wins."""
    lower = {str(k).lower(): v for k, v in (inputs or {}).items()}
    out = {}
    for role, names in ROLES.items():
        for n in names:
            if n in lower:
                out[role] = lower[n]
                break
    return out


def _same(a: str, b: str) -> bool:
    """Equal, or one holds the other (a truncated or wrapped copy), for texts long enough to tell."""
    if not a or not b:
        return False
    if a == b:
        return True
    return min(len(a), len(b)) >= MIN_TEXT and (a in b or b in a)


def _passages(v: Any) -> List[str]:
    """The context as the passages it was made of, each normalised; too-short ones can't be told apart."""
    items = v if isinstance(v, (list, tuple)) else [v]
    return [t for t in (_norm(x) for x in items) if len(t) >= MIN_TEXT]


def _context_findings(ctx: Any, run: dict, q: str, o: str) -> List[str]:
    """Was the judge's context what the run retrieved? Only asked when the run retrieved something:
    a run with no tool results may have had its context from somewhere Assay doesn't see."""
    given_in = _norm(run.get("input"))
    got = _passages(ctx)
    if not got:
        return []
    joined = " ".join(got)
    if given_in and joined in given_in:  # not the other way: a document may well quote the question
        return ["context is the run's input, not what it retrieved"]
    retrieved = [t for t in (_norm(x) for x in run.get("retrieved") or []) if t]
    if not retrieved or (o and joined == o):
        return []
    everything = " ".join(retrieved)
    found = [p for p in got if p in everything or any(_same(p, r) for r in retrieved)]
    if not found:
        return [f"context isn't what the run retrieved (its tools returned {_show(run['retrieved'][0])})"]
    return []


def findings(inputs: Dict[str, Any], run: Optional[dict]) -> List[str]:
    """What's wrong with what the evaluator saw.
    run: {"input", "outputs": [texts], "retrieved": [tool results]} or None."""
    got = roles(inputs)
    out = []
    ctx_name = next((n for n in ROLES["context"] if n in {str(k).lower() for k in inputs or {}}), None)
    for name, v in (inputs or {}).items():
        if isinstance(v, str) and TEMPLATE.search(v):
            out.append(f"{name} still holds a template variable nobody filled in: {TEMPLATE.search(v).group(0)}")
        elif v is None or (isinstance(v, (str, list, dict)) and not v):
            n = len((run or {}).get("retrieved") or [])
            out.append(f"{name} is empty" + (f", but the run's tools returned {n} result{'s' * (n != 1)}"
                                             if n and str(name).lower() == ctx_name else ""))
    # A template placeholder is already reported: comparing it with the trace would say so twice.
    blank = lambda role: "" if isinstance(got.get(role), str) and TEMPLATE.search(got[role]) else _norm(got.get(role))
    q, o = blank("query"), blank("output")
    if q and o and q == o:
        out.append("query and output are the same text, so the output graded isn't a response to anything")
    ctx = _norm(got.get("context"))
    if ctx and o and ctx == o:
        out.append("context and output are the same text")

    if run:
        given_in, outputs = _norm(run.get("input")), [_norm(x) for x in run.get("outputs") or [] if _norm(x)]
        if o and outputs and not any(_same(o, x) for x in outputs):
            if given_in and _same(o, given_in):
                out.append(f"output is the run's input, not its answer (the app answered {_show(run['outputs'][-1])})")
            else:
                out.append(f"output isn't what the app produced (it answered {_show(run['outputs'][-1])})")
        elif o and not outputs and given_in and o == given_in:
            out.append("output is the run's input, not its answer")
        if q and given_in and not _same(q, given_in) and q not in given_in:
            out.append(f"query isn't the run's input (the run was asked {_show(run['input'])})")
        if ctx and not (isinstance(got.get("context"), str) and TEMPLATE.search(got["context"])):
            out += _context_findings(got.get("context"), run, q, o)

    rubric = _norm(got.get("instructions"))
    if len(rubric) >= MIN_TEXT:
        head = rubric[:SHOW]
        if q and head in q:
            out.append("the evaluator's instructions are inside the query, mixed with the user's request")
        msgs = got.get("messages") if isinstance(got.get("messages"), list) else []
        said = lambda m, who: isinstance(m, dict) and m.get("role") in who and head in _norm(m.get("content"))
        if any(said(m, ("user",)) for m in msgs) and not any(said(m, ("system", "developer")) for m in msgs):
            out.append("the evaluator's instructions were sent as the user's message, not as the system prompt")
    return out


def run_context(engine: Engine, tenant: str, ids: Iterable[str]) -> Dict[str, dict]:
    """Per run: its input, every output it produced (answer, model texts, stage outputs), and what
    its tools returned."""
    ids = list({i for i in ids if i})
    ctx: Dict[str, dict] = {i: {"input": None, "outputs": [], "retrieved": []} for i in ids}
    if not ids:
        return {}
    ti, t, st, runs, stages = (store.trace_inputs, store.agent_trajectories, store.agent_steps, store.runs,
                               store.event_stage_runs)
    with engine.connect() as conn:
        for chunk in (ids[i:i + 500] for i in range(0, len(ids), 500)):
            for r in conn.execute(select(ti.c.trace_id, ti.c.input).where(and_(ti.c.tenant == tenant,
                                                                               ti.c.trace_id.in_(chunk)))):
                ctx[r.trace_id]["input"] = r.input
            for r in conn.execute(select(st.c.trajectory_id, st.c.text).where(and_(
                    st.c.tenant == tenant, st.c.trajectory_id.in_(chunk), st.c.kind.in_(("reason", "answer")),
                    st.c.text.is_not(None))).order_by(st.c.trajectory_id, st.c.seq)):
                ctx[r.trajectory_id]["outputs"].append(r.text)
            for r in conn.execute(select(st.c.trajectory_id, st.c.result).where(and_(  # what the judge's context
                    st.c.tenant == tenant, st.c.trajectory_id.in_(chunk),  # should come from: tools, MCP resources
                    st.c.kind.in_(("tool", "resource")),
                    st.c.error.is_(None), st.c.result.is_not(None))).order_by(st.c.trajectory_id, st.c.seq)):
                ctx[r.trajectory_id]["retrieved"].append(r.result)
            for tbl, key in ((t, t.c.trajectory_id), (runs, runs.c.run_id)):
                for r in conn.execute(select(key.label("id"), tbl.c.answer).where(and_(
                        tbl.c.tenant == tenant, key.in_(chunk), tbl.c.answer.is_not(None)))):
                    ctx[r.id]["outputs"].append(r.answer)
            for r in conn.execute(select(stages.c.document_id, stages.c.outputs).where(and_(
                    stages.c.tenant == tenant, stages.c.document_id.in_(chunk), stages.c.outputs.is_not(None)))):
                ctx[r.document_id]["outputs"] += [v for v in (r.outputs or {}).values() if v not in (None, "")]
    return ctx


def audit_rows(engine: Engine, tenant: str, rows: List) -> Dict[str, List[str]]:
    """{result_id: findings} for the results that recorded their inputs and have any."""
    with_inputs = [r for r in rows if getattr(r, "inputs", None)]
    ctx = run_context(engine, tenant, [r.document_id for r in with_inputs])
    out = {}
    for r in with_inputs:
        f = findings(r.inputs, ctx.get(r.document_id))
        if f:
            out[r.result_id] = f
    return out


def summary(rows: List, found: Dict[str, List[str]], examples: int = 20) -> dict:
    """How often each evaluator was given the wrong data, and examples."""
    audited = Counter(r.evaluator or "(unnamed)" for r in rows if getattr(r, "inputs", None))
    bad = Counter(r.evaluator or "(unnamed)" for r in rows if r.result_id in found)
    kinds = defaultdict(Counter)
    for r in rows:
        for f in found.get(r.result_id, []):
            kinds[r.evaluator or "(unnamed)"][f.split(" (")[0].split(":")[0]] += 1
    by_row = {r.result_id: r for r in rows}
    return {"audited": sum(audited.values()), "suspect": len(found),
            "not_audited": sum(1 for r in rows if not getattr(r, "inputs", None)),
            "evaluators": [{"evaluator": e, "audited": n, "suspect": bad[e], "share": bad[e] / n,
                            "problems": dict(kinds[e].most_common())} for e, n in audited.most_common()],
            "examples": [{"result_id": rid, "case_id": by_row[rid].case_id, "field": by_row[rid].field,
                          "evaluator": by_row[rid].evaluator, "status": by_row[rid].status,
                          "score": by_row[rid].score, "findings": fs}
                         for rid, fs in list(found.items())[:examples]]}


def evaluation_run(engine: Engine, tenant: str, run_id: str) -> Optional[dict]:
    t = store.eval_results
    with engine.connect() as conn:
        rows = conn.execute(select(t).where(and_(t.c.tenant == tenant, t.c.run_id == run_id))).all()
    if not rows:
        return None
    return {"run_id": run_id, **summary(rows, audit_rows(engine, tenant, rows))}
