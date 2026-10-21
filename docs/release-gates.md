[Assay](../README.md) › [Documentation](README.md)

# Release gates

`POST /v1/gates/evaluate` takes the following:
- per-unit outcomes for baseline and candidate, per slice;
- repeated baseline runs, for the noise floor;
- the lineage (prompt, model, build and corpus are required).

A slice holds if it is unmeasured, below `min_n`, or has no known noise floor. It rolls back
if even the optimistic end of the confidence interval is worse than max(tolerance, noise
floor). It holds if the interval straddles that limit, and advances otherwise.
`severity: "high"` halves the tolerance. The worst slice decides.

**Percentiles.** A rule compares means by default. For latency, a slow tail can grow while the
average holds: most calls get a little faster and one in twelve now takes four seconds. Set
`statistic: "p95"` (any percentile from `p1` to `p99.9`) and the rule compares that percentile
instead, with the tolerance in the measure's units:

```json
{"measure_id": "latency_ms", "higher_is_better": false, "tolerance": 200, "min_n": 200, "statistic": "p95"}
```

The interval comes from resampling the cases (the same ones on both sides when they're paired),
and the noise floor from each baseline run's own p95. A percentile rests on the values beyond
it, so a slice holds until at least 5 lie there: 100 cases for p95, 500 for p99. One measure can
have a mean rule and a p95 rule at the same time; each gets its own noise floor.

**Absolute limits.** Tolerance only limits how much worse the candidate is than the baseline,
so a baseline that's already slow lets a slow candidate through. Set `absolute_limit` and the
candidate also has to keep that value, whatever the baseline did: a maximum when lower is
better ("p95 under 20 s"), a minimum when higher is better ("accuracy at least 0.9"):

```json
{"measure_id": "latency_ms", "higher_is_better": false, "tolerance": 200, "min_n": 200, "statistic": "p95", "absolute_limit": 20000}
```

The candidate's statistic gets its own interval, from resampling its cases. The slice rolls back
if even the optimistic end is past the limit, holds if the interval reaches past it, and
otherwise keeps the decision against the baseline: the worse of the two decides. A candidate
past the limit rolls back even before there's a baseline or a noise floor; one within it still
needs both to advance. Each slice reports `candidate_value` and `candidate_ci`.

**A margin per slice.** `tolerance`, `severity`, `min_n` and `absolute_limit` apply to every
slice of a measure. A high-risk slice (high-value invoices, one regulated document type) can
have its own, agreed with the customer before the runs:

```json
{"measure_id": "field_accuracy", "tolerance": 0.05, "min_n": 100,
 "slices": {"high_value": {"tolerance": 0.01, "severity": "high"}}}
```

Here a 3-point drop advances on routine documents and rolls back on high-value ones, and the
worst slice decides, so that slice blocks the release on its own. A slice named in `slices`
has to be measured: a missing one holds the release, as if it were in `required_slices`. A
slice can set only those four fields; a misspelt one is rejected. Each slice reports the
`tolerance` it got, after severity, and `own_margin`.

**The noise floor** is how far apart two runs of the same version on the same corpus usually
land. With two baseline runs, it's their difference. With three or more, it's 1.96 × √2 × the
standard deviation of the run means, which is the 95% range of the gap between two runs. It
used to be the largest gap between any two runs. That grows with the number of runs, so more
evidence made the gate looser.

## Nondeterminism: flaky checks, and rerun instead of block

Run the same case five times and get PASS PASS FAIL PASS PASS. Is that a regression? Four of
five fits any true pass rate from 28% to 99%, so it depends on how reliably the case passed
before. Send each attempt as its own eval result (`attempt`: 0, 1, 2…). Each **check** (a case,
field and evaluator) then gets a pass rate with an exact interval, compared with the baseline
run:

| State | Meaning |
|---|---|
| got worse | the pass rate dropped beyond chance: one-sided Fisher exact test, with Benjamini–Hochberg at 5% across all checks that could have moved (so 5,000 checks don't produce 250 false alarms). Or the check collapsed: every attempt passed before and every one fails now, three or more each |
| needs reruns | plausibly worse, but too few attempts to tell: the same correction at 25%, or a collapse with fewer than three attempts. Says how many more attempts would settle it |
| flaky | both outcomes seen, and no worse than chance |
| improved, stable pass, stable fail, errored | as named |

A flaky check says what varies:
- the output changes between attempts (the model or pipeline is nondeterministic);
- only the verdict on the same output changes (the evaluator);
- attempts errored (infrastructure).

In Failure causes, flaky failures are kept apart from new ones. A cause is called a regression
only when its checks' attempts, pooled, show a drop beyond chance. Three attempts per case
can't prove much alone, but 55 cases that all went from 3/3 to 0/3 can.

**The release call** (`GET /v1/evals/runs/{run}/stability`, recorded with
`POST /v1/evals/runs/{run}/gate`) counts each check by its pass rate, so a flaky check is 0.8,
not a pass one run and a fail the next. The noise comes from the attempts themselves, so no
separate identical runs are needed. It uses the failure causes:
- failures that are the evaluator's, or that someone accepted, don't count;
- infrastructure failures are listed for rerunning;
- an intended change nobody has accepted holds the release.

The decision is one of:

| Call | When |
|---|---|
| roll back | the counted pass rate is lower than the tolerance allows, even at the optimistic end of its interval |
| hold | checks got worse beyond chance, or an intended change is waiting for a decision |
| rerun | nothing proven worse, but some checks can't be judged yet. Returns the list, with attempts per check |
| advance | otherwise. Flaky checks are reported and don't block. If the interval still reaches past the tolerance, the reasons say so: not proven worse, not proven fine |

Two gates, because they answer different questions. The run-level interval asks whether the
suite got broadly worse; it treats each check as one unit (a paired t interval over the checks'
pass-rate changes), since 50 attempts of one task say a lot about that task and nothing about
the others. The per-check states ask whether something that worked stopped working. A change
can pass the first and fail the second: one task going 8/8 → 0/8 among seven moves the average
by 14 points, within a small suite's interval, and holds the release as a check that got worse.

Measured on simulated unchanged changes (8 attempts per check, per-check pass rates between 60%
and 100%), 96–100% advance for suites of 5, 10 and 50 checks. The same checks with one task
collapsed from 8/8 to 0/8 hold every time.

## Unified release decision across both gates

`POST /v1/gates/release-decision` provides a unified release decision table across quality per slice,
stability runs, and operational limits in a single request.

Instead of running and reconciling quality gates (`/v1/gates/evaluate`) and stability runs
(`/v1/evals/runs/{run}/stability`) in separate scripts, the unified decision returns a structured
table with one overall outcome:
- `advance`: all slice quality rules, stability checks, and operational limits pass.
- `advance_with_conditions`: non-critical warnings or canary watch conditions apply (e.g. p95 latency increased slightly but remains comfortably below the operational ceiling).
- `hold`: unmeasured slices, unresolved intended changes, or stability regressions under investigation.
- `rollback`: any critical slice or ceiling violated beyond noise margins.

```json
{
  "lineage": {
    "prompt": "summarize@v2.1",
    "model": "gpt-5-turbo",
    "build": "sha-49e4324",
    "corpus": "golden-v3"
  },
  "rules": [
    {"measure_id": "accuracy", "tolerance": 0.05, "min_n": 50},
    {"measure_id": "latency_ms", "higher_is_better": false, "tolerance": 200, "statistic": "p95", "absolute_limit": 1000.0}
  ],
  "samples": { ... },
  "identical_runs": { ... },
  "stability": { ... },
  "operations": {
    "timeouts": {"count": 0, "limit": 5},
    "fallbacks": {"count": 1, "limit": 10}
  }
}
```

The response includes:
- `outcome`: the single rollout verdict (`advance`, `advance_with_conditions`, `hold`, `rollback`).
- `conditions`: actionable watch conditions for deployment canaries.
- `rows`: tabular breakdown of every quality slice, operational limit, and stability metric.

## Small high-risk slices

Slices with high business severity (e.g. security checks, VIP customers, regulated document types) often
have small sample counts (`n < min_n`, where default `min_n = 30`). Previously, rules on these slices held
automatically due to low sample volume.

Setting `severity: "high"` on a rule allows high-risk slices under `min_n` to fall back to per-case
stability verification:
- If all cases in the slice passed reliably in identical baseline runs, the slice evaluates each case individually.
- No reliable case is allowed to fail in the candidate run.
- Slices passing per-case stability advance with an annotated reason (`"passed stability per-case check (n=20 < min_n=30)"`), preventing low-volume critical cases from artificially blocking releases.
