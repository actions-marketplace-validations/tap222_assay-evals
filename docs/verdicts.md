[Assay](../README.md) › [Documentation](README.md)

# Results you can trust

## One verdict per check: not everything that isn't a pass is a failure

Evaluation infrastructure fails too: a judge times out, returns something unparseable, or its
job never runs for some cases. Counting those as failures blames the AI for the evaluator.
Every check in an evaluation run gets one verdict:

| Verdict | Means |
|---|---|
| `PASS` | judged, and passed (or a failure someone accepted) |
| `FAIL` | judged, and failed: the only verdict that says something about the AI |
| `FLAKY` | passes some attempts and fails others, the way it did before |
| `INCONCLUSIVE` | plausibly worse but too few attempts to tell, or an intended change nobody has accepted |
| `EVALUATOR_ERROR` | couldn't be judged: the evaluator errored, fails values that differ only in format, contradicts itself, or was given the wrong data |
| `INFRA_ERROR` | couldn't be judged: a timeout, rate limit, 5xx or connection error |
| `MISSING` | no result: the evaluator reported on this case in the baseline, or on most of the run's cases, but not on this one now |

Send a check the evaluator couldn't make as `status="error"` with the reason. The failure-cause
analysis decides the rest, so the verdicts agree with the release call. Missing results keep a
release from advancing ("rerun"). An evaluator job that didn't trigger at all is caught against
the baseline: "faithful@2 reported on 48 of these cases in the baseline, none in this run". A new
version of an evaluator (`faithful@3`) replaces the old one, so it isn't reported missing.

A retried or twice-delivered evaluator job sends the same judgement again under a new id. Assay
keeps the first, so it counts as one attempt, and `/v1/ingest` answers with `duplicate_checks`:
how many it dropped. That needs the check to name its trace (`run_id`) or its attempt, to tell a
repeat from a real second attempt. Two different judgements of the same output are both kept,
and the check is an `EVALUATOR_ERROR`: the evaluator contradicts itself. `GET /v1/evals/runs/{run}/verdicts?source=…&verdict=FAIL`
lists them, and the Failures page shows the counts.

`assay test` lists what couldn't be judged apart, never as a regression, and exits **3**
(inconclusive) when nothing got worse but some results couldn't be judged: 0 passed, 1 failed,
2 setup problem, 3 inconclusive. An inconclusive run doesn't move any baseline. In
`--junit` output these cases are `<error>`, JUnit's "couldn't run", not `<failure>`.

Agent runs are evaluated by Assay itself when they end (see Agents). A run whose evaluation
throws is recorded with the error and doesn't hold up the others. Ended runs still waiting to
be evaluated after 10 minutes (`ASSAY_BACKLOG_MINUTES`) open an alert, and the Agents page
says so: evaluation that's stuck is noticed, not silent.

## Was the judge given the right data?

An LLM judge given the wrong thing still returns a valid-looking score. Real examples:
`{{generation}}` filled with the trace's input, so the judge grades the user's own question;
`{{query}}` and `{{generation}}` holding the same text; a template variable nobody filled in;
the rubric sent as the user's message, so the judge confuses it with the request;
`{{context}}` filled with the request or another run's documents, so a faithfulness judge
checks the answer against the wrong sources.

Send what the evaluator saw with its result, by role, and Assay checks it against the trace:

```python
assay.check("nightly", "case-17", "pass", run_id=run.id, field="helpful", evaluator="helpful@1", score=5,
            inputs={"query": question, "generation": graded, "instructions": rubric, "messages": judge_messages})
```

| Finding | When |
|---|---|
| output is the run's input, not its answer | the graded "output" is what the user asked |
| output isn't what the app produced | it matches none of the run's answer, model outputs or stage outputs |
| query isn't the run's input | the judge was told about a different request |
| query and output are the same text | both variables were filled from the same place |
| a template variable nobody filled in | `{{query}}` or `${input.text}` reached the judge |
| instructions sent as the user's message | the rubric is in a `user` message and in no `system` one |
| context isn't what the run retrieved | the run's tools returned documents, and none of the context's passages is among them |
| context is the run's input | the context is the user's request, not retrieved documents |
| context is empty | the judge got no context, though the run's tools returned results |

Roles accept the names judges use: `generation`, `response` or `completion` for the output,
`question` or `input` for the query, `context`, `documents` or `retrieved` for the context
(a list of passages, or one text), `reference` or `ground_truth` for the expected answer,
`rubric` or `criteria` for the instructions. The context is only compared with the trace
when the run recorded tool results: if it didn't, the context may have come from somewhere
Assay doesn't see.

A result with a finding says nothing about the AI, whether it passed or failed. Release
calls leave it out ("judged on data that doesn't match the trace"). Failure causes group it
as an evaluator problem ("helpful@1 was given the wrong data"), and `assay test` lists it on
its own. `GET /v1/evals/runs/{run}/audit?source=…` shows, per evaluator, how many of its
results were audited and how many were suspect, with examples.
