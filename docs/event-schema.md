# Assay event schema, v1

The one contract between an application and Assay. The SDKs, the HTTP API
(`POST /v1/ingest`) and the JSON Schema (`GET /v1/schema`,
[`event-schema-v1.json`](event-schema-v1.json)) all describe this document.

## What it has to capture

An AI system does something (a **run**) made of **steps**. Afterwards, people and
tests say how it went: its **outcomes**. Everything Assay does is built on those
three things:

| Assay feature | Needs |
|---|---|
| health, cost, alerts, workflow graph | runs and their steps, with timing, status and cost |
| where a wrong value started | step outputs, and **corrections** |
| agent checks, first bad step | tool calls with arguments and results, state changes, the answer, and **expectations** |
| failure causes, flaky tests, release call | **checks** from test runs, with attempts and versions |
| learning from production | the run's **input**, and **feedback** |

## Design decisions

1. **A stream of small events, not one big record.** A run is opened, its steps
   are sent as they happen, and it is closed. A run that crashes or hangs still
   shows every step up to that point. Nothing needs buffering the whole run.
2. **One envelope for every event.** Every event has `v`, `type`, `id` and `ts`.
   One endpoint takes any mix, and adding a type later doesn't change the envelope.
3. **Idempotent by `id`.** Every event carries a client-generated id. Sending a
   batch twice (retries, at-least-once queues) never duplicates anything. Steps
   are keyed by `(run_id, seq)`.
4. **Order is explicit.** Steps carry `seq`, so they can arrive out of order,
   in different batches, or before `run.start`. A step before its run opens the
   run with what it knows, and `run.start` fills in the rest.
5. **A pipeline is an agent with a fixed path.** The same five step kinds cover
   both. A document pipeline uses `stage` steps. An agent uses `llm`, `tool`,
   `state` and `answer`. A run can mix them.
6. **Outcomes arrive whenever they're known**, minutes or weeks later, pointing
   at the run by `run_id`. A test check points at its test case, not at a run,
   and may also point at the run that produced its output.
7. **Versions are first class.** `version` (prompt, model, build, …) on the run
   and on checks is what regressions are attributed to.
8. **Strict about shape, open about content.** Unknown fields are rejected, so a
   typo fails loudly instead of losing data. Arguments, results, outputs and
   `tags` are free-form JSON.
9. **Privacy in the SDK.** Redaction runs before anything leaves the process.
   Sampling is decided per run, so a run is recorded whole or not at all.
   Outcomes are always sent.
10. **Versioned.** `v: 1` is required. A breaking change becomes `v: 2`, and the
    server accepts both for a deprecation period.

## Envelope

| Field | Type | Required | Meaning |
|---|---|---|---|
| `v` | `1` | yes | schema version |
| `type` | string | yes | one of the seven types below |
| `id` | string ≤128 | yes | unique per event; retries reuse it |
| `ts` | RFC 3339 time | yes | when it happened (a step: when it started) |

## Run events

### `run.start`
| Field | Type | Meaning |
|---|---|---|
| `run_id` | string | **required.** One run of the system on one input |
| `kind` | `agent` \| `pipeline` | default `agent`. Decides where `llm` steps go: agent reasoning, or a pipeline's model calls |
| `task` | string | the kind of work, e.g. `refund_request`, `invoice` |
| `segment` | string | what to break results out by: customer, region, team |
| `input` | JSON | what it was given (redacted by the SDK if configured) |
| `input_ref` | string | or where to fetch it: `s3://inbox/a.pdf` |
| `version` | {string: string} | what produced it: `{"prompt": "support@v5", "model": "…", "build": "…"}` |
| `test` | {run, case, attempt} | set when the run is part of a test run |
| `parent_run_id` | string | a sub-agent's parent run |
| `tags` | {string: scalar} | anything else to filter by |

### `step`
| Field | Type | Meaning |
|---|---|---|
| `run_id`, `seq` | string, int ≥ 0 | **required.** Which run, and the step's position in it |
| `kind` | `llm` \| `tool` \| `state` \| `answer` \| `stage` | **required** |
| `name` | string | tool name, stage name, or the object a state change touched (`order:17`) |
| `parent_seq` | int | a step nested inside another (a tool called from inside a stage) |
| `ended_at` | time | when it finished (`ts` is when it started) |
| `status` | `ok` \| `error` | default `ok` |
| `error` | string | what went wrong |

Fields for each kind:

| Kind | Fields |
|---|---|
| `llm` | `model`, `tokens_in`, `tokens_out`, `cost_usd`, `prompt` (`id@version`), `text` (the output, or a summary) |
| `tool` | `args` (object), `result` (JSON) |
| `state` | `op` (`create` \| `update` \| `delete`), `value` (the object afterwards) |
| `answer` | `text` |
| `stage` | `outputs` (object of named values; long strings count as text evidence), `did_work` (bool) |

### `run.end`
| Field | Type | Meaning |
|---|---|---|
| `run_id` | string | **required** |
| `status` | `completed` \| `failed` \| `abandoned` | default `completed` |
| `answer` | string | the final answer, if there was no `answer` step |
| `error` | string | why it failed |

## Outcome events

### `feedback`: what a user did
`run_id` (required), `kind` (`thumbs_up` \| `thumbs_down` \| `retry` \| `escalation` \| `complaint`), `note`.

### `check`: one result from a test run
| Field | Type | Meaning |
|---|---|---|
| `test` | {run, case, attempt} | **required** (`run` and `case`). Repeat attempts share `run` and `case` |
| `status` | `pass` \| `fail` \| `error` | **required.** `error`: the check itself couldn't run |
| `run_id` | string | the run that produced the output, so failures can be traced |
| `field`, `expected`, `actual` | string | what was checked |
| `evaluator` | string | `name@version`, e.g. `exact_match@2`, `llm_judge@1` |
| `score`, `reason` | number, string | |
| `version` | {string: string} | what produced the output, when there's no linked run |

Send passes too: they're what failures are compared against.

### `correction`: a wrong value someone found
`run_id`, `field` (required; dotted paths like `line_items.0.total`), `expected`,
`observed`, `kind` (`wrong` \| `missing` \| `extra`), `reporter`.

### `expect`: what a test case should do
`case` (required), `calls` (`[{"tool", "args", "optional", "any_order"}]`),
`allow_extra` (tools), `answer`, `answer_match` (`contains` \| `equals`),
`state` (`[{"object", "exists"} | {"object", "field", "equals"}]`), `max_steps`.

## Example: an agent run, streamed

```json
{"v":1,"type":"run.start","id":"e1","ts":"2026-09-24T10:00:00Z","run_id":"r-42","task":"refund_request",
 "input":"Please refund order O-17","version":{"prompt":"support@v5","model":"claude-sonnet-5"}}
{"v":1,"type":"step","id":"e2","ts":"2026-09-24T10:00:01Z","run_id":"r-42","seq":0,"kind":"llm",
 "model":"claude-sonnet-5","tokens_in":620,"tokens_out":180,"cost_usd":0.0024}
{"v":1,"type":"step","id":"e3","ts":"2026-09-24T10:00:02Z","run_id":"r-42","seq":1,"kind":"tool",
 "name":"get_order","args":{"order_id":"O-17"},"result":{"price":27.61},"ended_at":"2026-09-24T10:00:02.4Z"}
{"v":1,"type":"step","id":"e4","ts":"2026-09-24T10:00:03Z","run_id":"r-42","seq":2,"kind":"state",
 "name":"refund:O-17","op":"create","value":{"amount":27.61}}
{"v":1,"type":"step","id":"e5","ts":"2026-09-24T10:00:04Z","run_id":"r-42","seq":3,"kind":"answer",
 "text":"Refunded $27.61."}
{"v":1,"type":"run.end","id":"e6","ts":"2026-09-24T10:00:04Z","run_id":"r-42","status":"completed"}
{"v":1,"type":"feedback","id":"e7","ts":"2026-09-24T10:03:00Z","run_id":"r-42","kind":"thumbs_up"}
```

## How it maps onto Assay

| Event | Stored as |
|---|---|
| `run.start` | an agent trajectory (when the run has agent steps) and a document; its input |
| `step` `llm` / `tool` / `state` / `answer` | agent steps; in a `pipeline` run an `llm` step is a model call instead |
| `step` `stage` | a pipeline stage run |
| `run.end` | the run's end time, status and answer |
| `feedback`, `check`, `correction`, `expect` | feedback, an evaluation result, a reported error, an agent reference |

The older per-record endpoints (`/v1/events`, `/v1/events/trajectories`, …) keep
working. New integrations should use this schema.
