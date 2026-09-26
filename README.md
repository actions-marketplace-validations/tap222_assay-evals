# Assay

**Behavioral regression testing for AI apps.** You changed a prompt, a model or the code:
Assay tells you exactly what your AI app now does differently, whether the change is real,
and whether to trust the result.

```
$ assay diff v1.8.2 v1.9.0

AI BEHAVIOR DIFF
────────────────────────────────

Baseline: v1.8.2
Current:  v1.9.0

47 scenarios

✓ 39 unchanged
↑ 4 improved
✗ 3 regressed
⚠ 1 flaky

REGRESSIONS

1. refund_flow
   Expected: approval(refund) → refund
   Actual:   refund → approval(refund)
   An approval moved
   Safety: Broke “refund runs only once it's approved”
   Severity: HIGH

2. support_agent
   Expected: search_order
   Actual:   search_order → cancel_order
   New: cancel_order
   Severity: HIGH

3. invoice_number accuracy
   98% → 91%
   Severity: MEDIUM
```

- **What changed:** each scenario is compared with its own last passing run: its checks, and
  its flow (the tools it called, its approvals, what it read, in order). A scenario that now
  takes another path is shown even when nothing failed.
- **Is it real:** each scenario can run several times. A check that varied the same way before
  is flaky and doesn't block, and a drop that could be chance says so.
- **Can you trust it:** a result that couldn't be judged (a judge timed out, refused, or was
  given the wrong data) is kept apart, never counted as a regression. A pull request can't
  loosen the checks that judge it.

Your AI tests are pytest tests (`pytest --assay`), and a GitHub Action puts the same diff on
every pull request. See [Test your AI app locally](docs/testing.md#test-your-ai-app-locally-its-pytest-no-server-no-account).

Assay also runs as a service, for evaluation and observability of AI systems in production:
document-intelligence pipelines (OCR, classification, splitting, field extraction) and AI
agents (reasoning, tool calls, state changes), with or without LLMs.

Assay answers what a system's own logs can't:

- Is anything broken right now?
- Which customer, document type, step, tool or model is it broken for?
- Where did a wrong answer, or an agent's run, first go wrong?
- Which of 5,000 test failures are one cause, which are flaky, and is this release safe to ship?
- Which production failures should become regression tests?
- What does a document or an agent run really cost, and where does the money go?

It reports named measures per slice, with alerting, step-by-step traces, failure causes,
and release gates that decide on real signal instead of infrastructure health.

It runs as a separate service. It **reads** your system's data and never writes to it. To
send data from Python:

```bash
pip install assay-evals
```

```python
import assay_sdk as assay
assay.init("https://assay.example.com", key="ak_...")

with assay.run("refund_request", input=message) as run:
    run.llm(model="claude-sonnet-5", cost_usd=0.002)
    order = run.call("get_order", get_order, order_id="O-17")
    run.answer(reply)
```

See the [SDK](sdk/python/README.md), the [event schema](docs/event-schema.md), or the
[setup guide](docs/setup.md#setup-guide) for other ways in, including no code at all.

## Test your AI app in a minute

```bash
pip install assay-server pytest   # the SDK, its pytest plugin, and the assay command
assay init                        # assay.toml, and tests/ai/test_support.py with an example agent
pytest --assay tests/ai           # each test compared with its last passing run; exit 1 on a regression
assay diff                        # what behavior changed, scenario by scenario
```

Then add [the GitHub Action](docs/ci.md) to get the diff on every pull request. The
[testing guide](docs/testing.md) covers writing the tests.

## Quick start (demo data, nothing to connect)

```bash
pip install assay-server
assay demo                # synthetic tenant, 7 weeks of daily runs, staged incidents
assay serve               # http://127.0.0.1:8400  (API docs at /docs)
```

From a clone of this repo, to run the tests too:

```bash
pip install -e ".[dev]"
python -m assay demo      # synthetic tenant, 7 weeks of daily runs, staged incidents
python -m assay serve     # http://127.0.0.1:8400  (API docs at /docs)
pytest
```

The demo is a generated pipeline serving four customers. It has four staged incidents:
- a field-extraction failure spike that resolves
- slow classification calls
- a new customer shifting the input mix
- one customer's documents going missing downstream
- one customer's field extraction escalating to a pricier fallback model
- two prompt releases: `classify_document` v8 (better) and `extract_fields` v13 (misreads dates;
  flagged as a regression)
- a bad release of the validation step that corrupts correct totals, plus everyday errors
  (OCR losing totals, misclassification, misread dates) reported by reviewers and customers
- two path-contract breaks: documents skipping redaction, and a `delete_source` step

A second source, `events:demo-eval`, holds two runs of a 400-case certification set, before
and after a release. Each case runs three times, and some outputs vary between attempts.
A third, `events:demo-agent`, is a customer-support agent with nine tools, run on 150 cases
before and after a prompt release, with one of each agent failure mode built in (see
[Agents](docs/agents.md#agents-evaluating-the-trajectory-not-just-the-answer)). Its failures include one cause of each kind (see
[Failure causes](docs/failures.md#failure-causes-many-failures-a-few-causes)).

None of it is real data.

## Documentation

| | |
|---|---|
| [Testing your AI app](docs/testing.md) | Your AI tests are pytest tests: `pytest --assay`, the assay_case fixture, assertions, baselines, flakiness |
| [Behavior diff](docs/diff.md) | `assay diff`: what changed between two versions, with the flow before and after and a severity |
| [CI and pull requests](docs/ci.md) | The GitHub Action, the PR comment, rerunning what failed, timeouts |
| [Security](docs/security.md) | What the checks catch in the agent, and what a pull request can and can't do to the evaluation |
| [Agents](docs/agents.md) | Evaluating the trajectory: lifecycle, plan adherence, the LLM judge, conversations, MCP, behavior |
| [Results you can trust](docs/verdicts.md) | One verdict per check, and whether the judge was given the right data |
| [Failure analysis](docs/failures.md) | Where a wrong answer started, failure causes, path contracts |
| [Learning from production](docs/learning.md) | Production failures become regression tests, with the whole trace |
| [Prompt versions](docs/prompts.md) | The prompt registry, diffs, and results per version |
| [Release gates](docs/release-gates.md) | Advance, hold or roll back, and flaky checks that rerun instead of block |
| [Measures, cost and alerting](docs/measures.md) | What you get, the measures, cost per document, alerts |
| [Setup](docs/setup.md) | Install, connect your pipeline or push events, go live, deploy |
| [API and authentication](docs/api.md) | Keys, sending data, alert webhooks |
| [Event schema](docs/event-schema.md) | The v1 events: runs, steps, checks, expectations |
| [Code layout](docs/layout.md) | Where things are in this repository |
| [Not built yet](docs/roadmap.md) | What's missing |

The Python SDK has its own guide: [sdk/python](sdk/python/README.md).
