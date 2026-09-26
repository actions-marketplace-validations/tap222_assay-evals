[Assay](../README.md) › [Documentation](README.md)

# The report: what the evaluation found

Don't sell a team on "evals"; show them what was found. `assay report` tells it as findings, for
a week (`--days 7`) or a month:

```
# What we found: Sep 19 – Sep 26

**47 issues caught by tests before users saw them** (41 fixed, 6 still open)

- **HIGH** `refund_flow` safety: refund before approval — fixed Sep 24 by prompt support@13 → support@14
- **MEDIUM** `shipping_eta` answer: wrong delivery date — still failing

## Failure modes in production
| Failure mode | Found by | Share now | Before |
|---|---|---|---|
| Answers the policy, not the question | reading conversations | 9% | 12% |

## Fixed, and staying fixed
- “Loops on get_order”: no longer seen in production since Sep 21

## Surprising usage
- A new kind of task: warranty_claim

## The log
- Sep 22: Approval prompts need an explicit wait step (sam)
```

- **Caught by tests:** a check that passed, then failed in a run (a regression the tests caught),
  and the run where it passed again. What fixed it is what changed between the two: the commit,
  the prompt or model version, the input or settings ([What changed around it](diff.md)). A
  security check is HIGH.
- **In production** (the server's report): the top failure modes from reading conversations
  ([Reading conversations](learning.md#reading-conversations-what-nobody-wrote-a-test-for)) and
  from traces, each with its share now and before; patterns that stopped showing up once a test
  guarded them, and ones that came back; new kinds of task and new failure modes.
- **The log** keeps every finding as it happened, once, across reports, and the notes people add:
  `assay log add "what we learned"`, or `POST /v1/report/log`.

```
assay report                    # this week, from the tests run here (--format json, --out FILE)
GET  /v1/report?source=events:acme&days=7        the server's, with production
POST /v1/report/send?source=events:acme          post it to the alert webhook (Slack)
```

"Before users saw them" means fixed before a later test run passed: Assay knows the tests, not
your releases. Run the tests on every change and the count means what it says.
