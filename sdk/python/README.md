# assay-evals

Record what your AI system does, and how it went, in
[Assay](https://github.com/tap222/docai-eval): runs, agent steps, user feedback and test
results. Standard library only, Python 3.9+.

```bash
pip install assay-evals
```

With an Assay server, events go there (see the
[setup guide](https://github.com/tap222/docai-eval#setup-guide)). Without one, they're
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

For the test body, `assay_sdk.testing` has `assert_called(run, tool, **args)`,
`assert_not_called`, `assert_called_before(run, first, then)`, `assert_max_steps(run, n)`,
`assert_answer_contains` and `assert_no_pii`. Each fails with what the run actually did.

`pytest --assay` (with `assay-server` installed) also compares each test with its last passing
run, prints Assay's report in pytest's summary, and exits 1 only when something got worse.

To test with it, `assay test` (in `assay-server`) runs your code with the SDK recording,
checks each run against its `assay.expect(...)`, and compares with the last run that passed.
See [Test your AI app locally](https://github.com/tap222/docai-eval#test-your-ai-app-locally-its-pytest-no-server-no-account).
