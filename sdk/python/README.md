# assay-evals

Record what your AI system does, and how it went, in
[Assay](https://github.com/tap222/docai-eval): runs, agent steps, user feedback and test
results. Standard library only, Python 3.9+.

```bash
pip install assay-evals
```

You need an Assay server to send to. See the
[setup guide](https://github.com/tap222/docai-eval#setup-guide).

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
assay.expect("case-17", calls=[{"tool": "get_order", "args": {"order_id": "O-17"}}], answer="27.61")
```

| Call | Records |
|---|---|
| `assay.run(task, kind="agent"\|"pipeline", input=, version=, test={"run", "case", "attempt"})` | one run; an exception ends it as failed |
| `run.llm(...)`, `run.tool(name, args, result)`, `run.call(name, fn, **args)`, `run.state(obj, op, value)`, `run.answer(text)`, `with run.stage(name) as s` | its steps, in order |
| `assay.feedback`, `assay.check`, `assay.correction`, `assay.expect` | outcomes, sent whenever they're known |
| `assay.flush()` | send now (short-lived scripts); also happens every second and at exit |

These options go to `init()`:
- `redact`: a function applied to inputs, arguments, results, text and outputs before they
  leave the process.
- `sample=0.1`: record one run in ten. A run is recorded whole or not at all, and outcomes
  are always sent.
- `strict=True`: raise send errors while developing. Otherwise the SDK never raises into
  your code.
- `enabled=False`: the SDK does nothing, e.g. in unit tests.

Events follow the
[Assay event schema v1](https://github.com/tap222/docai-eval/blob/main/docs/event-schema.md). They stream to
`POST /v1/ingest` in the background, so a run that crashes still shows every step up to
the crash.
