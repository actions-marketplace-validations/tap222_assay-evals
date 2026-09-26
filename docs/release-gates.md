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
| got worse | the pass rate dropped beyond chance: one-sided Fisher exact test, with Benjamini–Hochberg across all checks (so 5,000 checks don't produce 250 false alarms) |
| needs reruns | plausibly worse, but too few attempts to tell. Says how many more attempts would settle it |
| flaky | both outcomes seen, and no worse than before |
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
| advance | otherwise. Flaky checks are reported and don't block |
