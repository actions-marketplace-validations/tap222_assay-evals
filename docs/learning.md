[Assay](../README.md) › [Documentation](README.md)

# Learning from production: failures become regression tests

Most production failures are never reported. **Learn** closes the loop: production traces →
anomalous ones → patterns → draft test cases → your approval → a permanent regression
suite. It also shows when a fixed bug comes back.

**1. Score every trace without labels.** Each signal shows its reason on the trace:

| Signal | Weight |
|---|---|
| a critical path contract broke / a warning one | 3 / 1 |
| someone reported a wrong value on it | 3 |
| a user gave a thumbs down, complained or escalated / retried | 3 / 1.5 |
| a step failed; an agent's tool errored and it never recovered | 2 |
| the same call with identical arguments 3+ times | 2 |
| far more steps, time or cost than the task's usual (robust z ≥ 3.5, and ≥ 1.5× the median) | 1 each |
| a fallback model answered; a path under 1% of the task's traces | 1 each |
| never finished | 1.5 |
| **quiet:** marked resolved, then escalated or complained about | 3 |
| **quiet:** the user edited the answer before using it, or did it themselves (feedback `edited`, `redone`) | 3 |
| **quiet:** the next turn asked nearly the same thing (half or more of the words the same) | 2 |
| **quiet:** the same user asked it again in a new conversation within a day | 2 |

A trace is anomalous at 2 or more.

The quiet signals catch the costly failures that don't announce themselves. The transcript looks
fine and the score is fine, but the user rephrases, asks again later, or does the work by hand.
They're read from what the user did next, not from what the agent said, so they need no
labels. "Asked again" needs `user` on the run (`assay.run(..., user=hashed_id)`), a
pseudonymous id, not an email. Evaluation-run trajectories are left out: tests aren't
production.

**2. Cluster into patterns.** Traces are grouped by their main **cause**, where it happened,
and, for task-relative signals, the task. A cause is a broken contract, a tool error or a
failing step, and it's chosen before a symptom like a thumbs down or an outlier. Each
pattern shows:
- what sets its traces apart from normal ones (lift against normal traces);
- when it started, and whether it came in a burst;
- a kind: **failure**, **infrastructure** (a tool or step that was down; shown but not made
  into tests, since an outage can't be replayed) or **unusual** (outliers and fallbacks,
  worth a look).

**3. Draft test cases.** For a pattern, the most typical trace becomes a draft, then the most
different ones, never near-duplicates. Each expectation says where it came from:

| From | Reliable? | What |
|---|---|---|
| a correction | yes | a person reported the right value |
| what broke | yes | the contract this trace broke, or "at most 2 identical calls" after a loop. These hold whatever the right answer is |
| normal traces | a guess | the tool sequence and step budget that normal traces of the task use |
| the request | a guess | an argument (e.g. `order_id`) found in the input because it looks like what normal traces pass |

A trace can only be replayed if its input was captured: `input` on a trajectory, or
`POST /v1/events/inputs` with the input or an `input_ref`. Inputs are scanned for personal
data (emails, phone and card numbers, IBANs, SSNs) and redacted on approval unless you say
otherwise. Personal data never becomes an expected argument.

**4. Review.** Edit the expected values and approve into a named suite, or reject with a
reason. For agents, the reference is stored, so the next evaluation run checks the case.

A saved agent case keeps the trace it came from in full, not just its input and output:

```json
{"case_id": "prod-3f2a", "input": {"message": "Refund O-17, I'm <email>"}, "expected": {...},
 "trajectory": {"task": "support", "version": {"model": "m1"}, "tags": {"channel": "web"},
   "steps": [{"seq": 0, "kind": "llm", "model": "claude-x", "prompt": "support@3", "tokens_in": 900,
              "tokens_out": 40, "tools": ["search_customer", "refund"], "text": "Looking up the customer"},
             {"seq": 1, "kind": "tool", "name": "search_customer", "args": {"email": "<email>"},
              "result": {"id": "C-1"}},
             {"seq": 2, "kind": "approval", "name": "refund", "decision": "approved", "by": "policy"},
             {"seq": 3, "kind": "answer", "text": "Refunded O-17."}],
   "output": "Refunded O-17.", "outcome": "resolved"}}
```

The steps are in the event schema's own shape, so a case sent back as events comes out the
same. That covers model calls (model, prompt, tokens, the tools offered), tool calls
(arguments, results, errors), approvals, state changes and nesting. The trace is saved when
the case is approved, so it outlives the trace itself. Personal data is redacted in the steps
too, as in the input. `GET /v1/learn/suites/{name}/export?format=json|jsonl|csv` includes it.

**5. Watch the loop.** Each pattern moves open → **protected** (a test guards it) →
**fixed** (a protected pattern stops appearing) → **recurred** (seen again after being
fixed). When a suite case that passed before fails in an evaluation run, Failure causes
say "Production bug back", and the release call holds. The loop reports:
- coverage: the share of failure patterns with a test;
- the median time from first seen to a test;
- how many patterns were fixed, and how many came back.

| Endpoint | Returns |
|---|---|
| `POST /v1/events/inputs`, `POST /v1/events/feedback` | what traces were given, and what users did about them |
| `GET /v1/learn/anomalies?source=…&days=7` | anomalous traces, each with its signals |
| `GET /v1/learn/patterns?source=…` | patterns with their status, plus the loop's coverage and timing |
| `POST /v1/learn/patterns/candidates?source=…&key=…` | draft cases from a pattern |
| `PUT /v1/learn/patterns/status` | dismiss a pattern as not a bug, or reopen it |
| `GET /v1/learn/candidates?source=…&status=proposed` | drafts to review |
| `POST /v1/learn/candidates/{id}/approve`, `…/reject` | decide |
| `GET /v1/learn/suites?source=…`, `GET /v1/learn/suites/{name}?source=…` | suites, and each case's origin and latest result |
| `GET /v1/learn/suites/{name}/export?source=…&format=json` | a suite as a file, each agent case with its trace in full |

In the demo, `events:demo-agent` has a week of live traffic. It includes a pattern only
thumbs-down feedback reveals (policy answers ignoring the knowledge base), and two patterns
already protected by tests. `events:demo` has document-pipeline patterns built from
reported errors, failed steps and contract breaks.
