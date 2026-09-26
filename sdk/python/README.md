# assay-evals

Record what your AI system does, and how it went, in
[Assay](https://github.com/tap222/docai-eval): runs, agent steps, user feedback and test
results. Standard library only, Python 3.9+.

```bash
pip install assay-evals
```

With an Assay server, events go there (see the
[setup guide](https://github.com/tap222/docai-eval/blob/main/docs/setup.md)). Without one, they're
recorded to a local file, so you can start with no account and no server (see
"No server" below).

```python
import assay_sdk as assay

assay.init("https://assay.example.com", key="ak_...")   # or set ASSAY_URL / ASSAY_KEY

# An agent
with assay.run("refund_request", input=message, version={"prompt": "support@v5", "model": "claude-sonnet-5"}) as run:
    run.llm(model="claude-sonnet-5", tokens_in=620, tokens_out=180, cost_usd=0.0024)
    order = run.call("get_order", get_order, order_id="O-17")    # runs it; records the result or the error
    run.state("refund:O-17", "create", {"amount": order["price"]})
    run.answer(f"Refunded ${order['price']}.")

# A pipeline
with assay.run("invoice", kind="pipeline", input_ref="s3://inbox/inv-9.pdf") as run:
    with run.stage("extract", prompt="extract_fields@v13") as s:
        run.llm(model="claude-sonnet-5", cost_usd=0.01)          # nested under the stage
        s.outputs.update(fields)

# Outcomes, whenever they're known
assay.feedback(run.id, "thumbs_down")
assay.correction(run.id, "total", expected="1240.00", observed="1204.00")
assay.check("nightly-0924", "case-17", "fail", run_id=run.id, field="total", expected="1240.00", actual="1204.00")
assay.check("nightly-0924", "case-17", "pass", run_id=run.id, field="helpful", evaluator="helpful@1",
            inputs={"query": question, "generation": graded})   # what the judge saw, checked against the trace
assay.expect("case-17", calls=[{"tool": "get_order", "args": {"order_id": "O-17"}}], answer="27.61")
```

| Call | Records |
|---|---|
| `assay.run(task, kind="agent"\|"pipeline", input=, version=, test="case-17")` | one run; an exception ends it as failed. `test` can also be `{"run", "case", "attempt"}`: under `assay test` the run and attempt are filled in |
| `run.llm(...)`, `run.tool(name, args, result)`, `run.call(name, fn, **args)`, `run.state(obj, op, value)`, `run.answer(text)`, `with run.stage(name) as s` | its steps, in order |
| `run.llm(..., tools=[...])`, `run.approval(action, decision, by=)`, `run.outcome("resolved")` | what the model was offered, decisions to allow an action, and whether the request was resolved |
| `assay.feedback`, `assay.check`, `assay.correction`, `assay.expect` | outcomes, sent whenever they're known |
| `run.expect(...)`, `run.check(field, status, expected=, actual=)` | the same, for a test-case run's own case |
| `assay.flush()` | send now (short-lived scripts); also happens every second and at exit |

These options go to `init()`:
- `redact`: a function applied to inputs, arguments, results, text and outputs before they
  leave the process.
- `sample=0.1`: record one run in ten. A run is recorded whole or not at all, and outcomes
  are always sent.
- `strict=True`: raise send errors while developing. Otherwise the SDK never raises into
  your code.
- `enabled=False`: the SDK does nothing, e.g. in unit tests.
- `path`: where to record when there's no server (see below).

Events follow the
[Assay event schema v1](https://github.com/tap222/docai-eval/blob/main/docs/event-schema.md). They stream to
`POST /v1/ingest` in the background, so a run that crashes still shows every step up to
the crash.

## No server: record locally

Call `assay.init()` with no URL, and with `ASSAY_URL` unset. Events are then appended to
`.assay/events.jsonl`, one per line, in the same form the server takes. Set `path=` or
`ASSAY_PATH` to use another file. Several processes can record to the same file, e.g.
`pytest -n 4`.

When the SDK creates the `.assay` folder, it adds a `.gitignore` there, so recorded inputs
don't end up in git.

To look at a recording, load it into a local Assay server
([`assay-server`](https://pypi.org/project/assay-server/)) and open the dashboard:

```bash
pip install assay-server
assay load            # reads .assay/events.jsonl into the tenant "local"
assay serve           # http://127.0.0.1:8400, source events:local
```

Loading the same file twice changes nothing, because every event has an id.

## Attach with a few lines: `@assay.step`, `@assay.tool`, `assay.instrument()`

```python
import assay_sdk as assay

assay.init()
assay.instrument()                     # Anthropic and OpenAI calls are recorded, in the step they're in

@assay.step("classification")          # a step of the pipeline; a dict it returns is its outputs
def classify(doc): ...

@assay.tool                            # a tool the agent calls: arguments, result or error, timing
def get_order(order_id): ...

@assay.pipeline("invoice", id_from="document_id")   # one run per call; @assay.agent for an agent
def handle(document_id, pdf):
    return graph.invoke({"pdf": pdf})
```

The steps, tools and model calls made inside `@assay.pipeline`, `@assay.agent` or `with
assay.run(...)` are recorded into that run. A step called outside any run starts one of its
own. With no run at all, a tool just runs. `assay.instrument()` never changes what a call
returns, and recording never breaks your code. Everything works on async functions too.
`assay connect code` proposes these lines for your code, as a diff.

## Any model provider, one shape: `Judge` and `normalize()`

```python
from assay_sdk import Judge, normalize

judge = Judge(provider="openai", model="gpt-5")        # anthropic, gemini, ollama, openai-compatible
r = judge.ask("Rate this answer…", system=RUBRIC, schema=VERDICT, temperature=0)
r.text, r.structured, r.tool_calls, r.usage, r.reasoning, r.finish_reason, r.error

r = normalize(response)   # a response you already have, from any of them (or LiteLLM)
```

Whatever answered, the fields are the same:
- `tool_calls`: `[{"name", "arguments", "id"}]`, with `arguments` always a dict. A JSON string
  (OpenAI's) is parsed; anything else is kept as `{"_raw": value}`. Nothing is dropped.
- `usage`: tokens `input`, `output`, `cached`, `reasoning`.
- `finish_reason`: `stop`, `length`, `tool_call`, `refusal`, `content_filter` or `error`.
- `structured`: the answer parsed as JSON, and checked against `schema`. When it doesn't fit,
  it's `None` with `error_kind="invalid"`, never an empty stand-in.
- `error` and `error_kind` (`timeout`, `rate_limited`, `unavailable`, `invalid`, `error`), when
  there's no answer. `ask()` never raises for a provider's failure.

Every other parameter goes to the provider unchanged: nothing is filtered or renamed.
Credentials are the provider SDK's own (its key variables, or a CLI login it supports).
Ollama and OpenAI-compatible servers (vLLM, LM Studio, a LiteLLM proxy) need no SDK:
`base_url`, and `api_key` if the server wants one. `evaluate()` takes a Judge's answer as it
is: `evaluate(judge, prompt, schema=VERDICT, judge_kwargs={"schema": VERDICT})`.
`judge_kwargs` is how arguments reach your judge when their names are also `evaluate()`'s own
(`schema`, `field`, `run`, ...); `evaluate()` warns when one looks misrouted.

`assay.instrument()` records Anthropic, OpenAI, Gemini, Ollama and LiteLLM calls through the
same reading, with the tool calls asked for and why each call stopped.

## Your own evaluators: results whose validity is explicit

An LLM judge that answers with something that isn't a verdict, or a metric that divides by
zero, shouldn't become a score of 0: that reads as a real failure of your AI. `evaluate()`
calls your evaluator and says whether what came back is a verdict at all:

```python
from assay_sdk import evaluate

VERDICT = {"type": "object", "required": ["score", "reason"],
           "properties": {"score": {"type": "number", "minimum": 0, "maximum": 1}, "reason": {"type": "string"}}}

result = evaluate(my_judge, question, answer, schema=VERDICT, threshold=0.7, retries=2,
                  run=assay_case, field="helpful", evaluator="helpful@2")
result.status             # PASS, FAIL, INVALID, ERROR, TIMEOUT or RATE_LIMITED
result.score              # only for PASS and FAIL: an invalid result has none
result.reason, result.error, result.attempts, result.raw_judge_output
```

- **INVALID:** it answered, but not with a verdict: not JSON (a fenced JSON block is fine),
  not the schema, a score that's `None`, `NaN`, infinite or outside `score_range` (0 to 1 by
  default), or a score with no `threshold` to decide by.
- **TIMEOUT, RATE_LIMITED, ERROR:** it raised. The kind comes from the exception's type, its
  `status_code` (429, 5xx) or its message.
- **Retries:** INVALID, timeouts, rate limits and an unavailable service (connection error,
  5xx) are tried again, `retries` times, with a pause that doubles (`backoff` seconds first).
  Any other exception is an ERROR at once: asking a bug again doesn't fix it.
- **Recorded:** with `run=`, the result becomes a check. PASS and FAIL are passes and fails;
  the rest are errors with their kind, what the judge said, and how many tries it took. So
  they're `INVALID`, `TIMEOUT`, `RATE_LIMITED`, `INFRA_ERROR` or `EVALUATOR_ERROR` in Assay,
  and never count against the AI.

The judge may return a bool, a number, a dict (`score`, `passed` or `pass`, `reason`), JSON
text, or an object with those attributes. `aevaluate()` is the same for an async judge.

## With pytest

Take the `assay_case` fixture. It's a pytest plugin that comes with this package, so there's
nothing to configure:

```python
def test_refund(assay_case):
    assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": "O-17"}}], answer="27.61")
    reply = my_agent("Refund O-17", run=assay_case)   # record steps on it
    assert "27.61" in reply
```

The fixture wraps the test in `assay.run(<test name>, test=<test id>)`. It also records the
test's own outcome as a check on the field `pytest`, so failing asserts count too. Tests
that don't take the fixture are left alone.

With [`assay-server`](https://pypi.org/project/assay-server/) installed, the test also fails
when its run fails Assay's checks: its `expect(...)`, and the contracts and PII rules in
`assay.toml`. So plain `pytest` goes red on an unsafe tool call:

```
The run failed Assay's checks:
  Safety: Unsafe action: Broke “delete_order never runs”: ran delete_order (step 2, order_id='O-2').
```

`expect(run)` declares everything a run should do, checked together when the test ends:
`expect(run).must_call("get_order").must_not_call("delete_order").max_steps(8)
.must_get_approval_before("refund").max_cost(0.05).max_latency(8).max_tools_exposed(10)
.max_context_tokens(8000).must_resolve()`.

For the test body, `assay_sdk.testing` has `assert_called(run, tool, **args)`,
`assert_not_called`, `assert_called_before(run, first, then)`, `assert_max_steps(run, n)`,
`assert_answer_contains` and `assert_no_pii`. Each fails with what the run actually did.

`pytest --assay` (with `assay-server` installed) also compares each test with its last passing
run, prints Assay's report in pytest's summary, and exits 1 only when something got worse.

To test with it, `assay test` (in `assay-server`) runs your code with the SDK recording,
checks each run against its `assay.expect(...)`, and compares with the last run that passed.
See [Test your AI app locally](https://github.com/tap222/docai-eval/blob/main/docs/testing.md).
