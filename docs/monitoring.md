[Documentation](README.md)

# CI and production: two jobs

**CI** protects against known regressions before a deploy: a small, curated suite (core features,
past bugs, edge cases), run on every change, so deterministic checks first ([testing](testing.md),
[CI](ci.md), [triage](triage.md)). **Production monitoring** finds failures in live traffic and how
often they happen. There are no reference answers there, so it leans on reference-free checks and a
sampled judge, and on intervals rather than single numbers.

## A sampled judge on production traffic

```
ASSAY_PRODUCTION_JUDGE_SAMPLE=0.05       # the share of ended production runs the judge reads
ASSAY_PRODUCTION_JUDGE_BUDGET_USD=5      # dollars a day
```

With the scheduler on, each scheduled run judges the production runs that ended since the last: 5%
of each task's, and at least one, so a rare task isn't drowned out by a common one. The sample is
the same every time (a run's place is a hash of its id), and no run is judged twice. Test-case runs
and synthetic ones are never in it; with `ASSAY_REVIEW_CONSENTED_ONLY`, only consented runs are.
Personal data is redacted first. `POST /v1/production/judge?source=…&hours=24` runs it now.

It's the built-in judge: plan quality, consistency, context retention, and faithfulness for runs
that retrieved. The results are kept apart from test results and never become a baseline.

## Quality, with intervals

`GET /v1/production/quality?source=…&days=7`, and **Production quality** in the Learn tab: per
metric, the value over the window and per day, each with a 95% interval.

- `check.*`: the reference-free checks every ended run gets (it finished, kept the critical
  contracts, didn't loop, recovered from tool errors, ...);
- `judge.*`: the sampled judge's pass rates;
- `category.*`: each failure category's share of the conversations read.

Give a metric a target:

```
PUT /v1/production/targets?source=events:acme   {"metric": "judge.consistency", "target": 0.95}
PUT /v1/production/targets?source=events:acme   {"metric": "category.Answers the policy, not the question", "target": 0.05}
```

A pass rate's target is a floor; a failure share's, a ceiling. When the interval's lower bound
crosses it, the alert says investigate: it could be noise, or the start of something. When the
whole interval is past it, it's a breach. Both are alerts (Production quality, in the Alerts tab),
and resolve once the interval clears.

## Connecting the two

What monitoring finds goes into CI. A sampled judge's FAIL is a failure signal for `assay learn`,
like a reported error: it groups into patterns, and a pattern's most typical and most different
traces become draft test cases to approve into a regression suite ([learning](learning.md)). So do
review categories. The report says when a failure a test guards against comes back.

## What the CI suite costs

`assay evals audit` ends with the suite: its size, how many cases a judge reads, the time in
evaluators, the model cost per run (the app's own calls and the judges'), and the costliest cases.
When most cases need a judge, it says so: CI runs often, so check deterministically where you can.
