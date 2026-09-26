[Documentation](README.md)

# Which evaluator, if any: triage

Not every failure mode needs an evaluator. Many are preferences nobody wrote down (short answers, a
format, a step), fixed in the prompt in a minute. Code checks are cheap to build and keep. A judge
needs 100 or so labels, calibration, and upkeep every week: worth it only for a subjective failure
that's still there after the prompt was fixed.

## Triage a failure category

A category comes from reading conversations ([learning](learning.md)). Triage it:

```
assay triage --source events:acme --run          # a model call per open category
assay triage --source events:acme --apply        # write the drafted code checks to tests/ai/
```

or the **Triage** button under Failure categories in the Review tab (`POST
/v1/review/categories/{id}/triage`). A model reads the category's notes and quotes with the
system prompts its conversations ran (the latest registered version of each), and names the
cheapest fix:

- **Fix the prompt**: the prompt never asks for what users wanted. The instruction to add is given.
- **A code check**: a rule tells a failing answer from a good one: a word or character limit, a
  pattern the answer must or mustn't match, text it must or mustn't include, valid JSON.
- **A judge**: the quality is subjective and no rule captures it.

## A code check is tried before it's offered

The proposed rule is run on the last answer of every conversation in the category (it should fail
them) and on conversations a reviewer found fine (it should pass them). It's offered only if it
catches at least 60% of the category and flags at most 10% of the fine ones: "It caught 7 of 7 and
flagged 0 of 7 fine ones". An offered check is drafted as a pytest file: the check as a function,
and a test to point at your app, skipped until you do (like `assay connect evals`). Nothing is
written without `--apply`, and a file that's there is left alone.

A rule that doesn't separate them isn't offered; the failure is treated as subjective.

## A judge only for what persisted

Assay knows which prompt versions the category's conversations ran and when each was registered.
It compares the category's share of what was read before and after the latest change:

- **fixed**: the share fell by half or more ("50% of conversations before support@13, 0% after").
  Nothing to build; a regression test keeps it fixed.
- **persisted**: still there after the change. Only then is a judge recommended.
- **too soon**: fewer than 5 conversations read since the change.
- **no change tried**: fix the prompt first, and build a judge only if it persists.

## What each judge costs to keep

```
assay evals audit          # the last 30 days (--days), --format json
```

```
Evaluators in the last 30 days: 41 code checks, 2 judges.

  judge                      labels  calibrated     trust
  helpful                    38/100  19 days ago    ok
  tone                        0/100  never          none

  - helpful: 38 labels; a judge needs 100 or so before its scores mean much
  - helpful: not calibrated in 19 days: a judge needs upkeep every week or so
  - helpful: 4 of its 4 failures are about length or format: a code check would do that without the upkeep
  - tone: never calibrated against people (assay calibrate)
```

For which of them could run in the request path as a guardrail, see [guardrail candidates](guardrails.md).

Code checks and judges are counted apart. For each judge: labels against the 100 or so it needs,
days since its calibration, its trust label ([calibration](calibration.md)), and whether most of its
failures are about something a rule could check.
