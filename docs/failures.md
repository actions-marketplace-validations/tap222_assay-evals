[Assay](../README.md) › [Documentation](README.md)

# Failure analysis: where it went wrong, and why

## Error analysis: where did it go wrong?

When an output is wrong, Assay traces it back through that document's steps and tells you
which step it started at, and how. This works for a pipeline of 4 steps or 20.

**1. Each step records what it produced.** Stage runs take an `outputs` object with named values,
nested allowed. Long strings, such as OCR text under `_text`, count as evidence of what was
available at that step.

```python
with assay.stage(doc_id, "ocr", sequence=1) as step:
    step.output("_text", text)
with assay.stage(doc_id, "field_extraction", sequence=2) as step:
    step.outputs.update(total=f.total, vendor=f.vendor, date=f.date)
```

In OpenTelemetry, use span attributes `assay.output.<field>` and `assay.sequence`. From a
database, map the `outputs` (a JSON column) and `sequence` fields of `stage_runs`.

**2. Someone reports a wrong value**: a reviewer, a QA check, a customer complaint, or a
correction your review tool already records.

```
POST /v1/errors   {"document_id": "inv-7", "field": "total", "expected": "1240.00", "observed": "1.24"}
```

The response already contains the diagnosis. Reports also arrive in batches
(`errors` in `POST /v1/events`), with `kind` = `wrong`, `missing` or `extra`, and dotted
fields such as `line_items.0.total`.

**3. Assay localizes it.** It walks the steps in order, by `sequence` or else start time, and
compares each step's value with the correct one. Formatting doesn't count: `$1,240.00` =
`1240`, and `01 Sep 2026` = `2026-09-01`.

| Verdict | Meaning | Where to look |
|---|---|---|
| **Introduced** | the first step to produce the field got it wrong, though the right value was in its input | that step's model, prompt or rules |
| **Corrupted** | right after step A, changed to wrong by step B | step B, often a recent release |
| **Dropped** | right at one step, gone at a later one (for missing values) | the step that lost it |
| **Input / upstream** | the right value never appears in any step's text | the source document or OCR |
| **Caused by an earlier error** | an earlier wrong *decision* on the same document (a label such as the document type) made this step fail | the earlier step, not this one |
| **After the pipeline** | every step had it right, yet the output was wrong | delivery, field mapping, or the downstream system |
| **Not localized** | no step records the field | add step outputs |

Labels a step decides (`invoice`, `approved`) are judged at the step that decided them.
Values copied from the page (amounts, dates, ids, names) are also checked against the text,
which is what separates *introduced* from *upstream*. Each diagnosis also lists anything else
odd at the origin step: a failure, a stage that did no work, a fallback model, or a
declared/served mismatch.

**Across documents**, the Errors tab and `GET /v1/errors` show where errors start, in pipeline
order, split by verdict and broken down by field, document type, segment and the model at the
origin step. Two measures make this alertable:
- `reported_error_rate`: the share of documents with a reported wrong value
- `errors_by_origin`: counts per step, verdict and field, with every known step recorded even
  at zero, so a step that suddenly starts corrupting values (a bad release) opens an alert

On a document's trace, **Step by step** shows every step's values side by side, with the wrong
ones marked, plus a form to report another.

## Failure causes: many failures, a few causes

5,000 failed checks are rarely 5,000 problems. **Failures** groups them into causes and
gives each cause a kind, with the evidence:

| Kind | What it means | Evidence Assay uses |
|---|---|---|
| **AI / pipeline error** | a step got the value wrong. Marked a **regression** when it's new | the step's output was wrong although its input had the right value; no infrastructure signal there. New: passed in the previous run, or a failure rate that jumped at a point in time |
| **Infrastructure** | the output was wrong because something broke | a step failed or timed out on that document; a fallback stood in for an *unavailable* model; right in every step but wrong after the pipeline; the harness couldn't run the check (timeouts, 5xx, rate limits); failures bunched in a short stretch |
| **Evaluator** | the check is wrong, not the output | same value written differently; the output is in the document's text and the expected value isn't; the same evaluator passed the same output on another attempt; the evaluator errored |
| **Intended change** | the output changed on purpose | newly failing with a release, consistently, and still correct once formatting is ignored |
| **Unsure** | the evidence doesn't point anywhere | said plainly rather than forced into a kind |

How it works:

1. Every failure is traced through its document's steps (the same analysis as
   [Error analysis](#error-analysis-where-did-it-go-wrong)) and classified on its own evidence.
2. Failures are grouped by kind, mechanism and step. In an eval run, newly failing and
   already failing are kept apart. AI groups are also split by how the value is wrong, so a
   swapped date and a wrong vendor at the same step are separate causes.
3. Each group is described by what **sets it apart from passing cases**. A feature is listed
   only if it's much more common in the group's failures than in the passes (lift, with a
   significance test). So "they're all invoices" doesn't make the list when most cases are
   invoices.
4. Group-level evidence settles the kind:
   - whether the failures are new since the previous run;
   - which change between the runs they line up with. That's the prompt at their origin step,
     or, for values a code step produced, the build;
   - when they started;
   - whether they came in a burst.

No LLM decides anything here. Every kind comes from rules, and the rules' evidence is on the
card. Confidence reflects how consistently the members point to that kind.

**Evaluation runs.** Send each check, **passes too**, to `POST /v1/events/eval-results`:

```json
{"run_id": "cert-2026-09-23", "case_id": "case-017", "document_id": "cert-2026-09-23/case-017",
 "field": "date", "expected": "2026-03-07", "actual": "2026-07-03", "status": "fail",
 "evaluator": "exact_match@2", "lineage": {"prompt": "extract_fields@v13", "build": "b2e4f60"}}
```

`status` is `pass`, `fail`, or `error` when the check itself couldn't run. Send the pipeline's
stage runs for the same `document_id`, so failures can be traced step by step. Each run is
compared with the one before it, or with `?baseline=<run_id>`.

| Endpoint | Returns |
|---|---|
| `GET /v1/failures?source=…&days=30` | reported errors grouped into causes |
| `GET /v1/evals/runs?source=…` | evaluation runs with pass/fail counts and lineage |
| `GET /v1/evals/runs/{run}/failures?source=…` | one run's failures grouped into causes |
| `PUT /v1/failures/decisions` | record a call on a cause: `accepted_change`, `not_a_problem` or `confirmed` |
| `GET /v1/evals/runs/{run}/expectations?source=…&key=…` | for an accepted change: the new expected values, to update the test set |
| `GET /v1/evals/runs/{run}/stability?source=…` | pass rate per check, flaky and rerun lists, and the release call |
| `POST /v1/evals/runs/{run}/gate` | the same call, recorded as a gate decision |

## Path contracts: which changes are regressions?

Not every change to a document's path is a regression. `search → order → retry → respond`
is usually fine; `search → delete → respond` is not. A diff between two workflows can't tell
them apart, so Assay checks every document's path against rules you agree to:

| Kind | Example | Catches |
|---|---|---|
| `must_include` | every document runs `redaction` | skipped safety steps |
| `never` | `delete_source` never runs, `unless` segment is `admin` | dangerous steps |
| `before` | `classification` runs before `field_extraction` | reordering |
| `only_after` | `human_review` runs only after `validation` | a branch taken without its trigger |
| `max_runs` | `field_extraction` runs at most 3 times | allows retries but not loops |
| `allowed_steps` | nothing outside this list runs | unknown new steps |
| `requires_approval` | a step runs only once it's approved (`run.approval(step, "approved")`; a later rejection takes it back) | a refund nobody approved |
| `claim` | an answer that claims "refunded" needs a successful `refund` call, and `order:*` status `refunded` in the recorded state (agent runs) | an agent saying it did what it didn't |

A claim contract makes the run's own record the authority, not a model's opinion of it: the
model proposes, the recorded tool results and state verify. `claim` is a regular expression
matched in the answer (`claim_in = "any"` also covers the model's own text, such as a
self-review). `needs` is the tool that must have succeeded, and `state` is `{name, field, is}`
on the run's state changes (`run.state("order:17", "update", {"status": "refunded"})`); give
either or both:

```toml
[[contracts]]
kind = "claim"
claim = "refunded|money is on its way"
needs = "refund"
state = { name = "order:*", field = "status", is = "refunded" }
```

A broken claim reads `claimed “Refunded” in the answer, but refund was called but failed (402
card declined) and order:17 status is 'delivered', not 'refunded'`. It fails the safety check in
tests, and in production it's a contract signal, so the trace becomes a candidate case.

Scope a contract with `when` (the document must match every listed attribute) or `unless`
(a matching document is exempt), over `segment`, `document_type` and `processing_mode`.
`must_include` is judged only once a document has finished.

```bash
curl -X POST "$ASSAY/v1/contracts" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"kind": "never", "step": "delete_source", "unless": {"segment": ["admin"]}}'
```

- **One break is enough.** A broken contract opens an alert on the run that sees it. There's
  no baseline to learn and no sample size to wait for. The alert resolves on the first run
  where documents were checked and none broke it, or when you change or delete the contract.
- **Shifts** (`GET /v1/contracts/shifts`) are changes that break no contract: a new step, a
  new move between steps, or a move whose share of documents changed by more than 3
  standard errors and 2 points. They are listed for a person to look at and never page.
- **Suggestions** (`GET /v1/contracts/suggestions`) are the contracts that the last 30 days of
  paths already keep, learned only from documents that keep your existing contracts. You
  confirm them rather than write them from scratch.
- The **Workflow** view draws a red dashed ring around steps where contracts broke and a red
  dashed edge on the move that broke them. **Trace** marks the step where a document broke
  a contract.
