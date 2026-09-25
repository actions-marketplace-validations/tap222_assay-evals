# Assay

Evaluation and observability for AI systems: document-intelligence pipelines (OCR,
classification, splitting, field extraction) and AI agents (reasoning, tool calls, state
changes), with or without LLMs.

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
[setup guide](#setup-guide) for other ways in, including no code at all.

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
[Agents](#agents-evaluating-the-trajectory-not-just-the-answer)). Its failures include one cause of each kind (see
[Failure causes](#failure-causes-many-failures-a-few-causes)).

None of it is real data.

## Test your AI app locally: it's pytest (no server, no account)

Your AI tests are pytest tests: `tests/ai/test_support.py`, `test_tool_selection.py`,
`test_security.py`, `test_document_extraction.py`. Each test records what the agent did, fails
when the run breaks a rule or misses what the test expects, and, with `--assay`, is compared
with its own last passing run:

```bash
pip install assay-server pytest     # assay-server brings the SDK, assay-evals, and its pytest plugin
assay init                          # assay.toml, and tests/ai/test_support.py with an example agent
pytest --assay tests/ai             # exit code 1 when something regressed
```

```
    def test_no_refund_before_delivery(assay_case):
        assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": "O-18"}}], answer="hasn't arrived")
        support_agent(assay_case, "Can I get a refund for O-18?", "O-18")
>       assert_not_called(assay_case, "refund")
E       AssertionError: refund shouldn't have been called, but was: refund(order_id='O-18', amount=12.0) (step 1).

==================================== assay =====================================
2 cases · 1 attempt each · compared with each case's last passing run (2 of 2 cases have one, from 1 run)

✗ tests/ai/test_support.py  1/2

⚠ 1 case regressed (3 checks)

1. tests/ai/test_support.py::test_no_refund_before_delivery  Answer, Your asserts, Tool usage
   Wrong tool: Called refund(order_id='O-18', amount=12.0), which the reference doesn't expect.

Failed.
```

Plain `pytest` works too: red or green, with Assay's checks. `--assay` adds the comparison
with each test's last passing run, and decides the exit code: 0 nothing got worse, 1 a
regression, 6 inconclusive (nothing got worse, but some results couldn't be judged). A test
that failed in its baseline too doesn't fail the session; a failing test that doesn't take
the fixture does, as always. It works with `pytest -n` (xdist) and `-k`: running a subset only
moves the baselines of the tests it ran. `--assay-baseline RUN` compares with one run instead,
and `--assay-upload` sends the run to a server.

`assay test` does the same from outside pytest (or around any command that records with the
SDK), and adds `--repeat N` for flaky cases and `--junit report.xml` for CI. Its exit codes are
0, 1, 2 (setup problem) and 3 (inconclusive).

- **Your tests:** with pytest, take the `assay_case` fixture (it comes with `assay-evals`)
  and set `command = "pytest -q tests/ai"`. Each test is then a case, and pytest's own
  pass/fail is the answer. With `assay-server` installed, a test also fails when its run
  fails Assay's checks: its expectations, and the contracts and PII rules in `assay.toml`.
  So `pytest` alone goes red on an unsafe tool call, with the reason (`[pytest] checks =
  false` turns that off). `assay_sdk.testing` has assertions for the test body:

  ```python
  from assay_sdk.testing import assert_called, assert_not_called, assert_max_steps

  def test_refund(assay_case):
      reply = my_agent("Refund O-17", run=assay_case)   # records steps: run.call, run.answer, ...
      assert_called(assay_case, "get_order", order_id="O-17")
      assert_not_called(assay_case, "delete_order")
      assert_max_steps(assay_case, 6)
      assert "27.61" in reply
  ```

  They fail with what the run did, e.g. "Expected a call to get_order(order_id='O-17');
  get_order was called with get_order(order_id='O-18')". There are also
  `assert_called_before`, `assert_answer_contains` and `assert_no_pii`.

  Without pytest, record each case with `assay.run(..., test="<case>")` and
  `run.expect(...)`. For pipelines, send field results with
  `run.check("invoice_date", "pass" | "fail", expected=..., actual=...)`.
- **Safety rules:** go in `assay.toml` as path contracts, e.g. `never delete_order`, or
  `refund only_after get_order`. Every agent run is checked against them.
- **PII:** personal data (email, card, IBAN, SSN, phone) in a tool's arguments fails the PII
  check. A tool that needs it is allowed it in `assay.toml`, under `[pii]`:
  `allow = { send_receipt = ["email"] }`.
- **Baseline:** kept per case: each case's last passing run. A failing run never becomes
  a baseline, and running a subset (`assay test -- pytest tests/ai/test_security.py`) only
  moves the baselines of the cases it ran. If your suite has known failures, `assay accept`
  makes the latest run their baseline, and later runs then fail only on what got worse.
- **Report:** with pytest, the report opens with each test file (`✗ tests/ai/test_tools.py
  9/10`), then each check. `assay test --junit report.xml` writes JUnit XML for CI: a
  regression is a failure, a known failure is skipped, and a flaky test passes.
- **Flakiness:** `repeat = 3` (or `assay test --repeat 3`) runs each case several times. A
  check that varied the same way before is reported as flaky and doesn't block. A drop that
  could be chance says so.
- **Where things live:** everything goes in `.assay/` (recordings, the store, the baseline),
  which ignores itself in git. `assay test -- pytest -q tests/ai` overrides the command.

To see the recorded runs in the dashboard:
`ASSAY_STORE_URL=sqlite:///.assay/assay.db assay serve`, then open source `events:local`.

To share a run with your team, `assay test --upload` (or `assay upload` for the latest run)
sends it to an Assay server, set with `ASSAY_URL` and `ASSAY_KEY`. The server then checks it
too. That needs a key with the `manage` scope; with an `ingest` key, the dashboard can do the
checking. Sending the same run twice changes nothing.

## Setup guide

Setting up Assay has two stages. Installing it is done once by someone technical and takes
30–60 minutes. Integrating it happens in the dashboard and needs no code.

### Part 1: install (someone technical, once)

**1. Pick where it runs.**

| Option | Steps | Good for |
|---|---|---|
| **Docker** (recommended) | `git clone https://github.com/tap222/docai-eval && cd docai-eval`<br>`docker build -t assay .`<br>`docker run -d -p 8400:8400 -v assay-data:/data assay` | a company server or VM |
| **Vercel** | Import the repo at vercel.com/new (no build settings). Add a Postgres database, e.g. Neon from the Vercel marketplace, and set `ASSAY_STORE_URL` to it | a quick hosted setup |
| **Laptop trial** | `pip install assay-server`<br>`assay demo`<br>`assay serve` → http://127.0.0.1:8400 | trying it with demo data |

On Vercel without a database, data is lost whenever an instance restarts. Use that for
demos only.

**2. Use a real database for anything beyond a trial.** SQLite is fine on one server. For
production, set `ASSAY_STORE_URL=postgresql+psycopg://user:pass@host:5432/assay`. Tables are
created automatically, and a newer version adds its columns when it starts, so there are no
migrations to run.

**3. Switch on access control before sharing the link.** A new server has no login. Creating
the first admin key switches login on:

```bash
python -m assay keys create --tenant acme --scopes admin --name "acme admin"
# with Docker: docker exec <container> assay keys create --tenant acme --scopes admin --name "acme admin"
```

Save the printed key. It isn't shown again.

**4. Optional settings.** All of them are listed in `.env.example`.
- `ASSAY_PUBLIC_URL`: the dashboard's address, so alerts and tickets link back to it.
- `ASSAY_SCHEDULE_MINUTES=60` and `ASSAY_SCHEDULE_SOURCES=events:acme`: recompute every hour.
  On Vercel, use the cron setup under [Deploy](#deploy) instead.
- `ASSAY_ABANDON_MINUTES=30`: an agent run with no events for this long is marked abandoned
  and evaluated. `ASSAY_EVALUATE_SECONDS=60`: how often that's checked (0 turns it off).

**5. Hand over the dashboard address and the admin key** to whoever will set up the
integrations.

### Part 2: integrate (in the dashboard, no code)

Open the dashboard, paste the key when asked, and go to **Connect**. The details are in
[Getting started without code](#getting-started-without-code).

**6. Pick how your data gets in.** Use one choice or several:

| Choice | Who does it | Effort |
|---|---|---|
| **Upload a spreadsheet** | you | minutes |
| **We use OpenTelemetry** | whoever runs the collector | about an hour, no code changes |
| **A developer can add a few lines** (Python, `pip install assay-evals`) | a developer | about an hour |
| **Another system can send web requests** (Zapier, n8n, a script) | whoever owns it | 1–3 hours |
| **Our data is in a database** | whoever runs the Assay server | about a day |

Each choice has a **Create a key for this** button, a snippet to copy, and a ready-to-send
message for the person who needs to act on it.

**7. Watch the checklist.** It shows "N of 8 features ready" and gives the next step for
anything missing:
- items and steps switch on health, cost and alerts;
- corrections show where wrong answers start;
- test results switch on release checks;
- agent runs switch on step-by-step agent checks;
- feedback and inputs let Assay find problems nobody reported and turn them into tests.

**8. Build history (optional).** Under **Connect → Advanced**, **Backfill 30 days** replays
the past month so alerts work from day one.

**9. Send results where your team works.** Under **Send results where your team works**:
- **Slack:** paste an incoming-webhook URL and save. Assay sends a test message.
- **Jira or Linear:** paste the site and token and save. Failure patterns under **Learn** then
  get an **Open a ticket** button.
- **Block bad releases:** give the generated GitHub Actions or GitLab file to whoever runs
  your builds, and add a key with the `manage` scope as the `ASSAY_KEY` secret.

### Part 3: day to day

| Where | What you do |
|---|---|
| **Overview** and **Alerts** | see what's broken right now |
| **Failures** | see a test run's failures grouped into causes, and accept intended changes |
| **Learn** | review draft test cases built from production failures, and approve them into a suite |
| **Agents** and **Trace** | open any run step by step |

### Go-live checklist

- [ ] Postgres, not SQLite in `/tmp`
- [ ] An admin key created and stored safely, and a separate `ingest` key for each system
  that sends data
- [ ] `ASSAY_PUBLIC_URL` set
- [ ] At least one source showing **Receiving data** under Connect
- [ ] Slack connected, plus Jira or Linear if you want tickets
- [ ] Regular recomputing: the built-in schedule, cron, or Vercel cron
- [ ] Assay's database protected: Slack, Jira and Linear tokens are stored in it unencrypted

**Shortest path to a first result:** Docker, create a key, then **Connect → Upload a
spreadsheet** of corrections or test results. You'll see results within 15 minutes, with no
developer involved after the install.

## Getting started without code

Open the dashboard and go to **Connect**. It works as a checklist:

1. **Pick how your data can reach Assay.** There are five choices, in plain words:
   - **Upload a spreadsheet.** No code. Corrections, test results, user feedback, or a list
     of items, as a CSV file or cells copied from Excel or Google Sheets. Assay works out what
     the sheet holds and which column is which, and shows rows it can't read and why. You can
     correct its guesses before importing. Test sheets just need a run name.
   - **We use OpenTelemetry.** One exporter added to the collector's config.
   - **A developer can add a few lines.** `pip install assay-evals`, with no dependencies.
   - **Another system can send web requests.** Zapier, n8n, or any script.
   - **Our data is in a database.** Read-only access and a mapping file.

   Each choice gives you something to copy, with the server's address filled in. It also
   writes a ready-to-send message for whoever looks after that system. If you manage keys,
   one button creates a sending key and puts it in both.
2. **Watch the checklist.** It shows which features your data switches on and the one next
   step for each that's missing, and it refreshes itself as data arrives.
3. **Send results where your team works.** Paste a Slack webhook URL, a Jira site and token,
   or a Linear key, and Assay checks it works. Alerts then appear in Slack, and any failure
   pattern under **Learn** gets an "Open a ticket" button. Saved tokens are never shown
   again. **Block bad releases** gives you a ready-made GitHub Actions, GitLab or plain-script
   job for whoever looks after your builds: it stops a release unless Assay says "advance".

Upgrading Assay is safe. On start it adds any new tables and columns to your existing
database and never removes data.

## Connect your pipeline

There are two ways to connect. Both feed the same measures.

### 1. Point Assay at your database (read-only)

Assay needs four kinds of records, plus a fifth for people cost. Most pipelines already have them:

| Record | What it is | Key fields |
|---|---|---|
| **documents** | one row per document or file | `document_id`, `received_at`, `completed_at`, `segment`, `document_type`, `processing_mode`, `file_hash`, `page_count` |
| **stage_runs** | one row per pipeline stage per document | `document_id`, `stage`, `status`, `started_at`, `finished_at`, `did_work` |
| **calls** | one row per model call | `call_id`, `stage`, `ts`, `model_declared`, `model_served`, `latency_ms`, `cost_usd`, `status`, `resolving_layer`, `gate_reason`, `code_revision` |
| **indexed** | one row per extracted value | `document_id`, `has_positions` |
| **reviews** *(optional)* | time a person spent on a document | `review_id`, `document_id`, `ts`, `kind` (review / rework), `minutes` or `cost_usd`, `reviewer`, `stage` |

`segment` is whatever you want failures broken out by, such as customer, region, business
unit or jurisdiction. `document_type` is your own taxonomy.

Write a mapping that says where each field lives in your schema. The mapping has a `FROM`
clause per record type and a SQL expression per field. See `mappings/example.json`. Fields
you don't record can be set to `"NULL"`. The measures that need them then say *unmeasured*
instead of guessing.

```bash
pip install -e ".[postgres]"
export ASSAY_SOURCE_URL=postgresql+psycopg://readonly:***@your-db:5432/pipeline
export ASSAY_SOURCE_MAPPING=./mappings/mine.json
python -m assay check-source                  # tests every mapped field against the live DB
python -m assay serve --every 60 --source sql # hourly runs over a 1-day window
```

If your tables already follow the reference schema in `assay/sources/sql.py`
(`documents`, `stage_runs`, `model_calls`, `extractions`), you don't need a mapping.
Any database SQLAlchemy supports works. Use a read-only role; Postgres sessions are also
opened `READ ONLY`.

### 2. Push events

If you can't expose a database, send events from your code with the
[`assay-evals`](sdk/python/README.md) SDK (`pip install assay-evals`, standard library only):

```python
import assay_sdk as assay
assay.init("https://assay.example.com", key="ak_...")   # an ingest key; it sets the tenant

with assay.run("invoice", kind="pipeline", input_ref="s3://inbox/inv-9.pdf", segment=customer) as run:
    with run.stage("field_extraction", prompt="extract_fields@v13") as s:   # timing, status, failures
        run.llm(model="claude-sonnet-5", tokens_in=2400, tokens_out=300, cost_usd=0.011)
        s.outputs.update(extract(doc))                                     # so errors can be traced

assay.correction(run.id, "total", expected="1240.00", observed="1204.00")   # a reviewer's fix
```

Events stream in the background. The SDK retries, and never raises into your pipeline
unless `strict=True`. Agents use the same `run` with `run.llm`, `run.call` (tool calls),
`run.state` and `run.answer`; see [Agents](#agents-evaluating-the-trajectory-not-just-the-answer).

Other ways in:
- **Any language:** `POST /v1/ingest` with `Authorization: Bearer <ingest key>`, using the
  [event schema](docs/event-schema.md).
- **OpenTelemetry:** add an exporter and change no code. See
  [API and authentication](#api-and-authentication).
- **Older integrations:** `assay/client.py` (per-record `POST /v1/events`) still works.

### 3. See what you get, and backfill

- The **Connect** tab (or `python -m assay coverage --source …`) checks the last 7 days of your
  data. For every measure it says whether it's *live*, *partial* (works, but a field would make
  it more useful) or *blocked*, and names the exact field that would unlock it.
- **Backfill** (`python -m assay backfill --source … --days 30`, or the button) replays past
  days so every slice has a baseline and anything already wrong is flagged on day one. It
  notifies nobody about history, skips days that already have a run, and never disturbs
  current alerts.

## API and authentication

The full reference, with an **Authorize** button, is at `/docs` (OpenAPI at `/openapi.json`).

### Keys

Send a key as `Authorization: Bearer <key>` (or `X-API-Key: <key>`). A key belongs to one
**tenant** and only ever sees that tenant: its data is the source `events:<tenant>`, and a
different `X-Tenant` header is refused. Scopes:

| Scope | Can |
|---|---|
| `ingest` | send events: give this to a pipeline |
| `read` | dashboards, measures, alerts, traces, cost, coverage |
| `manage` | everything in `read`, plus runs, backfill, SLOs, the rate card and release-gate decisions |
| `admin` | everything, plus create and revoke that tenant's keys |

A key with tenant `*` is a platform key: it sees every tenant and may pass `X-Tenant` to
write on a tenant's behalf. Keys are stored as SHA-256 hashes, shown once at creation, and can
expire (`expires_in_days`) or be revoked instantly. Each key is rate-limited
(`ASSAY_RATE_LIMIT_PER_MIN`, default 1,200 per minute per instance) and answers `429` with
`Retry-After` when over the limit.

```bash
# first key, on the server (switches authentication on)
python -m assay keys create --tenant acme --scopes admin --name "acme admin"
# then, over the API, with that key
curl -X POST $ASSAY/v1/keys -H "Authorization: Bearer $ADMIN" -H "Content-Type: application/json" \
     -d '{"name": "invoice pipeline (prod)", "scopes": ["ingest"]}'
python -m assay keys list
python -m assay keys revoke 3
```

The **Connect** tab lists, creates and revokes keys for admin keys too. `ASSAY_ADMIN_KEY` sets
a break-glass platform key from the environment.

**Open mode:** with no keys and no `ASSAY_ADMIN_KEY`, the server runs without authentication,
for local use and demos. `/v1/whoami` and the dashboard header say so. Open mode never grants
`admin`, so nobody can mint keys over the API on an open server. Set `ASSAY_AUTH=required`
to refuse to run open.

### Sending data

**New integrations should use the event schema.** [`docs/event-schema.md`](docs/event-schema.md)
describes one contract for runs, steps and outcomes (feedback, test checks, corrections,
expectations). It streams to `POST /v1/ingest`, and its JSON Schema is at `GET /v1/schema`.
The Python SDK, [`assay-evals`](https://pypi.org/project/assay-evals/), is the smallest way
to send it: `pip install assay-evals`, with no dependencies (source in
[`sdk/python`](sdk/python/README.md)). The endpoints below keep working.

**Releasing the SDK:** bump `version` in `sdk/python/pyproject.toml` and `__version__` in
`sdk/python/assay_sdk/__init__.py`, then `git tag sdk-v<version> && git push origin
sdk-v<version>`. The `Publish SDK` workflow checks that the tag matches both versions, runs
the SDK tests, publishes to TestPyPI, installs it from there, and then waits for your
approval before publishing to PyPI. The one-time PyPI setup is described at the top of
`.github/workflows/publish-sdk.yml`.

| Endpoint | Use |
|---|---|
| `POST /v1/events` | any mix of `documents`, `stage_runs`, `calls`, `reviews`, `extractions`, `errors`, `eval_results`, `trajectories`, `inputs`, `feedback` in one request (up to 5,000 records) |
| `POST /v1/events/{documents,stage-runs,calls,reviews,extractions,errors,eval-results,trajectories,inputs,feedback}` | one record type per request |
| `POST /v1/otlp/v1/traces` | OpenTelemetry traces (OTLP/HTTP, JSON). Point a collector's `otlphttp` exporter at `/v1/otlp` |

The ingest contract:
- **Idempotent.** Every write is an upsert by `(tenant, id)`. Stage runs without a `run_id`
  get one derived from document, stage and start time; extractions without an `extraction_id`
  get one from document and `field`. Retrying a batch never duplicates anything.
- **Strict.** Unknown fields are refused (`422`, naming the field), so a typo like
  `documentType` doesn't silently drop data. Costs, latencies and page counts can't be negative.
- **Partial updates.** Sending a document again only changes the fields you send, so a
  completion event doesn't erase the document type.
- **Timezones.** Timestamps may carry any offset. They're stored as UTC.

**OpenTelemetry mapping:** one trace is one document, unless a span sets `assay.document_id`.
The root span gives received and completed times, plus `assay.document_type`, `assay.segment`
and `assay.page_count`. Spans with `gen_ai.*` attributes become model calls: requested and
served model, latency, and error status. Their stage comes from `assay.stage` on the span or
its parent. Other spans with `assay.stage` become stage runs. `service.version` becomes the
code revision.

```yaml
exporters:
  otlphttp/assay:
    endpoint: https://assay.example.com/v1/otlp
    encoding: json
    headers: { Authorization: "Bearer ${env:ASSAY_KEY}" }
```

### Alert webhooks

With `ASSAY_WEBHOOK_SECRET` set, each webhook carries `X-Assay-Timestamp` and
`X-Assay-Signature: sha256=<hex>`, the HMAC-SHA256 of `"<timestamp>.<body>"` with the secret.
Check it, and reject stale timestamps, to know a request came from Assay:

```python
expected = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(f"sha256={expected}", signature) and abs(time.time() - int(ts)) < 300
```

## Error analysis: where did it go wrong?

When an output is wrong, Assay traces it back through that document's steps and tells you
which step it started at, and how. This works for a pipeline of 4 steps or 20.

**1. Each step records what it produced.** Stage runs take an `outputs` object with named values,
nested allowed. Long strings, such as OCR text under `_text`, count as evidence of what was
available at that step.

```python
with assay.stage(doc_id, "ocr", sequence=1) as step:
    step.output("_text", text)
with assay.stage(doc_id, "field_extraction", sequence=2) as step:
    step.outputs.update(total=f.total, vendor=f.vendor, date=f.date)
```

In OpenTelemetry, use span attributes `assay.output.<field>` and `assay.sequence`. From a
database, map the `outputs` (a JSON column) and `sequence` fields of `stage_runs`.

**2. Someone reports a wrong value**: a reviewer, a QA check, a customer complaint, or a
correction your review tool already records.

```
POST /v1/errors   {"document_id": "inv-7", "field": "total", "expected": "1240.00", "observed": "1.24"}
```

The response already contains the diagnosis. Reports also arrive in batches
(`errors` in `POST /v1/events`), with `kind` = `wrong`, `missing` or `extra`, and dotted
fields such as `line_items.0.total`.

**3. Assay localizes it.** It walks the steps in order, by `sequence` or else start time, and
compares each step's value with the correct one. Formatting doesn't count: `$1,240.00` =
`1240`, and `01 Sep 2026` = `2026-09-01`.

| Verdict | Meaning | Where to look |
|---|---|---|
| **Introduced** | the first step to produce the field got it wrong, though the right value was in its input | that step's model, prompt or rules |
| **Corrupted** | right after step A, changed to wrong by step B | step B, often a recent release |
| **Dropped** | right at one step, gone at a later one (for missing values) | the step that lost it |
| **Input / upstream** | the right value never appears in any step's text | the source document or OCR |
| **Caused by an earlier error** | an earlier wrong *decision* on the same document (a label such as the document type) made this step fail | the earlier step, not this one |
| **After the pipeline** | every step had it right, yet the output was wrong | delivery, field mapping, or the downstream system |
| **Not localized** | no step records the field | add step outputs |

Labels a step decides (`invoice`, `approved`) are judged at the step that decided them.
Values copied from the page (amounts, dates, ids, names) are also checked against the text,
which is what separates *introduced* from *upstream*. Each diagnosis also lists anything else
odd at the origin step: a failure, a stage that did no work, a fallback model, or a
declared/served mismatch.

**Across documents**, the Errors tab and `GET /v1/errors` show where errors start, in pipeline
order, split by verdict and broken down by field, document type, segment and the model at the
origin step. Two measures make this alertable:
- `reported_error_rate`: the share of documents with a reported wrong value
- `errors_by_origin`: counts per step, verdict and field, with every known step recorded even
  at zero, so a step that suddenly starts corrupting values (a bad release) opens an alert

On a document's trace, **Step by step** shows every step's values side by side, with the wrong
ones marked, plus a form to report another.

## Prompt versions

Every model call and step can say which prompt it ran: `prompt_id` (e.g. `extract_fields`) and
`prompt_version` (e.g. `v13`). If you don't label versions, use `Assay.prompt_version(template)`,
a hash of the prompt text, so the same text always gets the same version.

```python
assay.call(doc_id, stage="field_extraction", prompt_id="extract_fields", prompt_version="v13", ...)
with assay.stage(doc_id, "field_extraction", prompt_id="extract_fields", prompt_version="v13") as step: ...
# from CI, on release: the template (for diffs) and what changed
assay.register_prompt("extract_fields", template=text, version="v13", note="Accept European day-first dates")
```

The same fields work as OpenTelemetry attributes (`assay.prompt_id`, `assay.prompt_version` on
a model-call span or its stage span) and in the SQL mapping (`calls.prompt_id`,
`calls.prompt_version`). `POST /v1/prompts` registers a version over HTTP.

**Registry.** Every version seen in traffic is recorded with when it first and last served;
registering from CI adds the template, note and author. Nothing needs registering up front.
For database sources, the registry is filled in during scheduled runs.

**Per-version results.** The **Prompts** tab and `GET /v1/prompts` show, for every version in
the window: documents handled, error rate, fallback rate, p95 latency and cost per call. Each
version is compared with the one before it:
- **Error rate:** documents with a reported error that started at a step running this version.
  When a wrong earlier decision caused the error, it's charged to that earlier step's prompt.
  The comparison is adjusted to the new version's document-type mix, so a version that got
  harder documents isn't blamed for them. It comes with a 95% interval, and a verdict of
  *worse*, *better*, *no clear difference* or *too few* (under 30 documents per version).
- Fallback and call-error rates are compared as proportions; latency and cost as relative changes.
- **Diff** shows exactly what changed between two registered templates.

**Catching a bad release.** A new version has no history of its own, so an anomaly band can't
judge it. Instead, after every run, each version is compared with its predecessor over the
last 30 days. A *worse* verdict opens a **prompt regression** alert, which reaches Slack or a
webhook like any other. It resolves when the version is shown no worse, or stops serving.
*Too few* keeps an open alert open. In the demo, `extract_fields` v13 was flagged 2 days
after release.

**Everywhere else:**
- Charts mark when a prompt version went live, so a jump lines up with its cause.
- Error diagnoses and the step-by-step view show the prompt each step ran.
- Call error rate, latency, model mismatch and total spend can be broken down by `prompt`.
- `prompt_error_rate` alerts per version.
- Release gates warn when the `prompt` in a decision's lineage (`id@version`) isn't in the
  registry.

## Learning from production: failures become regression tests

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

A trace is anomalous at 2 or more. Evaluation-run trajectories are left out: tests aren't
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

In the demo, `events:demo-agent` has a week of live traffic. It includes a pattern only
thumbs-down feedback reveals (policy answers ignoring the knowledge base), and two patterns
already protected by tests. `events:demo` has document-pipeline patterns built from
reported errors, failed steps and contract breaks.

## Agents: evaluating the trajectory, not just the answer

An agent reasons, calls tools, reads what they return, changes things, and answers. Judging
only the answer misses a refund issued to the wrong order, or a correct answer reached by
deleting something first. Send each run as a **trajectory**:

```json
{"trajectory_id": "agent-0923.case-017.a0", "run_id": "agent-0923", "case_id": "case-017", "attempt": 0,
 "task": "refund_request", "started_at": "2026-09-23T10:00:00Z", "answer": "Refunded $27.61.",
 "lineage": {"prompt": "support_agent@v5", "model": "claude-sonnet-5"},
 "steps": [
   {"kind": "reason", "model": "claude-sonnet-5", "tokens": 812, "cost_usd": 0.0024},
   {"kind": "tool", "name": "get_order", "args": {"order_id": "O-10017"}, "result": {"price": 27.61}},
   {"kind": "tool", "name": "issue_refund", "args": {"order_id": "O-10017", "amount": 27.61}},
   {"kind": "state", "name": "refund:O-10017", "args": {"op": "create"}, "result": {"amount": 27.61}},
   {"kind": "answer", "text": "Refunded $27.61."}]}
```

Also send what each test case expects to `POST /v1/agents/references`:
- the tool calls, in order where it matters. Arguments match partially, and a call can be
  `optional` or `any_order`;
- tools that may be called beyond those (`allow_extra`, e.g. read-only lookups);
- the answer;
- end-state assertions, such as `{"object": "refund:O-10017", "exists": true}` or
  `{"object": "order:*", "field": "qty", "equals": 3}`;
- a step budget.

OpenTelemetry works too. `execute_tool` spans become tool steps and model spans become
reasoning steps (see the mapping at `/docs`).

`POST /v1/agents/runs/{run}/evaluate` checks every trajectory five ways and stores the checks
as evaluation results. Failure causes, flakiness across attempts and the release call then
work on agents unchanged.

| Check | Passes when |
|---|---|
| answer | the final answer has the expected value (whole-word, or written another usual way) |
| end state | the world afterwards matches, folded from the state-change steps |
| tool calls | every required call was made with the right arguments, in order. Retrying a call that errored is fine; so are allowed extras |
| safety | no critical path contract broke. Contracts see tool arguments: `delete_order` never runs `where` `confirmed` isn't true; `issue_refund` only after `get_order` with the `same` `order_id`; at most 2 `identical` `lookup_customer` calls |
| efficiency | within the step budget, and no call repeated 3 times with identical arguments |

For a failing run, **credit assignment** finds the first bad step and how it went wrong:
unsafe action, a tool error it never recovered from (infrastructure when the error says the
tool was unavailable), a loop, the wrong tool, the right tool with the wrong arguments,
stopping before an expected call, ignoring a tool result that held the answer, the wrong end
state, or a wrong answer after correct calls. These are the mechanisms Failure causes groups
by, so you get lines like "Wrong tool: `search_orders` where `get_order` was expected: 21
cases, a regression since `support_agent` v4 → v5".

Tool calls are also recorded as pipeline steps. So the workflow graph draws the agent's tool
graph, path contracts and shifts apply to tool sequences, Trace shows any run step by step
against its reference, and the measures count tool failures and model cost. A document
pipeline is just an agent with a fixed path.

### When a run is evaluated: when it ends, not on a timer

An agent can take ten seconds or five minutes, so Assay doesn't evaluate after a fixed
delay. Each run goes through a lifecycle:

- **running:** `run.start` (or its first step) has arrived, and `run.end` hasn't. It isn't
  judged half-way.
- **ended:** `run.end` says `completed` or `failed`. A run with no events for 30 minutes
  (`ASSAY_ABANDON_MINUTES`) is marked **abandoned**: its process died.
- **evaluated:** in the same request that ended it, once its child runs (`parent_run_id`)
  have ended too.
- **evaluated again:** if more events arrive after that, e.g. a late step.

With OpenTelemetry, a run ends when its root span arrives. Spans are exported as they end,
often over several batches, so the run's steps are added up across batches, and a tool span
that arrives before the root span doesn't end the run early.

Every run gets the checks that need no expectations: it **finished**, it kept the critical
path contracts, it didn't **loop**, and no **tool error** went unrecovered. A test-case run
also gets its case's checks, so an evaluation run fills in as its cases finish. A background
sweep, every 60 seconds (`ASSAY_EVALUATE_SECONDS`) or on each `/v1/cron` call on Vercel,
marks quiet runs abandoned and evaluates anything left over.

| Endpoint | Returns |
|---|---|
| `GET /v1/agents/lifecycle?source=…` | how many runs are running, awaiting evaluation, evaluated, abandoned or failing, and the latest failures |
| `POST /v1/events/trajectories` | ingest trajectories (steps inline) |
| `POST /v1/agents/references`, `GET /v1/agents/references?source=…` | what cases expect |
| `GET /v1/agents/runs?source=…` | agent runs, newest first |
| `POST /v1/agents/runs/{run}/evaluate?source=…` | run the five checks, stored as eval results (runs still going are skipped and counted) |
| `GET /v1/agents/runs/{run}?source=…` | pass rate per check, tool precision and recall, first bad steps, and efficiency vs the baseline |
| `GET /v1/agents/trajectories/{id}?source=…` | one run step by step: divergence, end state, contract breaks, cost, and its latest evaluation |

## One verdict per check: not everything that isn't a pass is a failure

Evaluation infrastructure fails too: a judge times out, returns something unparseable, or its
job never runs for some cases. Counting those as failures blames the AI for the evaluator.
Every check in an evaluation run gets one verdict:

| Verdict | Means |
|---|---|
| `PASS` | judged, and passed (or a failure someone accepted) |
| `FAIL` | judged, and failed: the only verdict that says something about the AI |
| `FLAKY` | passes some attempts and fails others, the way it did before |
| `INCONCLUSIVE` | plausibly worse but too few attempts to tell, or an intended change nobody has accepted |
| `EVALUATOR_ERROR` | couldn't be judged: the evaluator errored, fails values that differ only in format, contradicts itself, or was given the wrong data |
| `INFRA_ERROR` | couldn't be judged: a timeout, rate limit, 5xx or connection error |
| `MISSING` | no result: the evaluator reported on most of the run's cases, but not this one |

Send a check the evaluator couldn't make as `status="error"` with the reason. The failure-cause
analysis decides the rest, so the verdicts agree with the release call. Missing results keep a
release from advancing ("rerun"). `GET /v1/evals/runs/{run}/verdicts?source=…&verdict=FAIL`
lists them, and the Failures page shows the counts.

`assay test` lists what couldn't be judged apart, never as a regression, and exits **3**
(inconclusive) when nothing got worse but some results couldn't be judged: 0 passed, 1 failed,
2 setup problem, 3 inconclusive. An inconclusive run doesn't move any baseline. In
`--junit` output these cases are `<error>`, JUnit's "couldn't run", not `<failure>`.

Agent runs are evaluated by Assay itself when they end (see Agents). A run whose evaluation
throws is recorded with the error and doesn't hold up the others. Ended runs still waiting to
be evaluated after 10 minutes (`ASSAY_BACKLOG_MINUTES`) open an alert, and the Agents page
says so: evaluation that's stuck is noticed, not silent.

## Was the judge given the right data?

An LLM judge given the wrong thing still returns a valid-looking score. Real examples:
`{{generation}}` filled with the trace's input, so the judge grades the user's own question;
`{{query}}` and `{{generation}}` holding the same text; a template variable nobody filled in;
the rubric sent as the user's message, so the judge confuses it with the request.

Send what the evaluator saw with its result, by role, and Assay checks it against the trace:

```python
assay.check("nightly", "case-17", "pass", run_id=run.id, field="helpful", evaluator="helpful@1", score=5,
            inputs={"query": question, "generation": graded, "instructions": rubric, "messages": judge_messages})
```

| Finding | When |
|---|---|
| output is the run's input, not its answer | the graded "output" is what the user asked |
| output isn't what the app produced | it matches none of the run's answer, model outputs or stage outputs |
| query isn't the run's input | the judge was told about a different request |
| query and output are the same text | both variables were filled from the same place |
| a template variable nobody filled in | `{{query}}` or `${input.text}` reached the judge |
| instructions sent as the user's message | the rubric is in a `user` message and in no `system` one |

Roles accept the names judges use: `generation`, `response` or `completion` for the output,
`question` or `input` for the query, `reference` or `ground_truth` for the expected answer,
`rubric` or `criteria` for the instructions.

A result with a finding says nothing about the AI, whether it passed or failed. Release
calls leave it out ("judged on data that doesn't match the trace"). Failure causes group it
as an evaluator problem ("helpful@1 was given the wrong data"), and `assay test` lists it on
its own. `GET /v1/evals/runs/{run}/audit?source=…` shows, per evaluator, how many of its
results were audited and how many were suspect, with examples.

## Failure causes: many failures, a few causes

5,000 failed checks are rarely 5,000 problems. **Failures** groups them into causes and
gives each cause a kind, with the evidence:

| Kind | What it means | Evidence Assay uses |
|---|---|---|
| **AI / pipeline error** | a step got the value wrong. Marked a **regression** when it's new | the step's output was wrong although its input had the right value; no infrastructure signal there. New: passed in the previous run, or a failure rate that jumped at a point in time |
| **Infrastructure** | the output was wrong because something broke | a step failed or timed out on that document; a fallback stood in for an *unavailable* model; right in every step but wrong after the pipeline; the harness couldn't run the check (timeouts, 5xx, rate limits); failures bunched in a short stretch |
| **Evaluator** | the check is wrong, not the output | same value written differently; the output is in the document's text and the expected value isn't; the same evaluator passed the same output on another attempt; the evaluator errored |
| **Intended change** | the output changed on purpose | newly failing with a release, consistently, and still correct once formatting is ignored |
| **Unsure** | the evidence doesn't point anywhere | said plainly rather than forced into a kind |

How it works:

1. Every failure is traced through its document's steps (the same analysis as
   [Error analysis](#error-analysis-where-did-it-go-wrong)) and classified on its own evidence.
2. Failures are grouped by kind, mechanism and step. In an eval run, newly failing and
   already failing are kept apart. AI groups are also split by how the value is wrong, so a
   swapped date and a wrong vendor at the same step are separate causes.
3. Each group is described by what **sets it apart from passing cases**. A feature is listed
   only if it's much more common in the group's failures than in the passes (lift, with a
   significance test). So "they're all invoices" doesn't make the list when most cases are
   invoices.
4. Group-level evidence settles the kind:
   - whether the failures are new since the previous run;
   - which change between the runs they line up with. That's the prompt at their origin step,
     or, for values a code step produced, the build;
   - when they started;
   - whether they came in a burst.

No LLM decides anything here. Every kind comes from rules, and the rules' evidence is on the
card. Confidence reflects how consistently the members point to that kind.

**Evaluation runs.** Send each check, **passes too**, to `POST /v1/events/eval-results`:

```json
{"run_id": "cert-2026-09-23", "case_id": "case-017", "document_id": "cert-2026-09-23/case-017",
 "field": "date", "expected": "2026-03-07", "actual": "2026-07-03", "status": "fail",
 "evaluator": "exact_match@2", "lineage": {"prompt": "extract_fields@v13", "build": "b2e4f60"}}
```

`status` is `pass`, `fail`, or `error` when the check itself couldn't run. Send the pipeline's
stage runs for the same `document_id`, so failures can be traced step by step. Each run is
compared with the one before it, or with `?baseline=<run_id>`.

| Endpoint | Returns |
|---|---|
| `GET /v1/failures?source=…&days=30` | reported errors grouped into causes |
| `GET /v1/evals/runs?source=…` | evaluation runs with pass/fail counts and lineage |
| `GET /v1/evals/runs/{run}/failures?source=…` | one run's failures grouped into causes |
| `PUT /v1/failures/decisions` | record a call on a cause: `accepted_change`, `not_a_problem` or `confirmed` |
| `GET /v1/evals/runs/{run}/expectations?source=…&key=…` | for an accepted change: the new expected values, to update the test set |
| `GET /v1/evals/runs/{run}/stability?source=…` | pass rate per check, flaky and rerun lists, and the release call |
| `POST /v1/evals/runs/{run}/gate` | the same call, recorded as a gate decision |

## Path contracts: which changes are regressions?

Not every change to a document's path is a regression. `search → order → retry → respond`
is usually fine; `search → delete → respond` is not. A diff between two workflows can't tell
them apart, so Assay checks every document's path against rules you agree to:

| Kind | Example | Catches |
|---|---|---|
| `must_include` | every document runs `redaction` | skipped safety steps |
| `never` | `delete_source` never runs, `unless` segment is `admin` | dangerous steps |
| `before` | `classification` runs before `field_extraction` | reordering |
| `only_after` | `human_review` runs only after `validation` | a branch taken without its trigger |
| `max_runs` | `field_extraction` runs at most 3 times | allows retries but not loops |
| `allowed_steps` | nothing outside this list runs | unknown new steps |

Scope a contract with `when` (the document must match every listed attribute) or `unless`
(a matching document is exempt), over `segment`, `document_type` and `processing_mode`.
`must_include` is judged only once a document has finished.

```bash
curl -X POST "$ASSAY/v1/contracts" -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"kind": "never", "step": "delete_source", "unless": {"segment": ["admin"]}}'
```

- **One break is enough.** A broken contract opens an alert on the run that sees it. There's
  no baseline to learn and no sample size to wait for. The alert resolves on the first run
  where documents were checked and none broke it, or when you change or delete the contract.
- **Shifts** (`GET /v1/contracts/shifts`) are changes that break no contract: a new step, a
  new move between steps, or a move whose share of documents changed by more than 3
  standard errors and 2 points. They are listed for a person to look at and never page.
- **Suggestions** (`GET /v1/contracts/suggestions`) are the contracts that the last 30 days of
  paths already keep, learned only from documents that keep your existing contracts. You
  confirm them rather than write them from scratch.
- The **Workflow** view draws a red dashed ring around steps where contracts broke and a red
  dashed edge on the move that broke them. **Trace** marks the step where a document broke
  a contract.

## What you get

| View | Answers |
|---|---|
| **Overview** | What's broken right now? Open anomalies, SLO state, and the slices that moved beyond noise since the last run. Refreshes every minute. |
| **Workflow** | The pipeline as a graph inferred from traffic: each step's health, errors and broken path contracts; the contracts and how each is holding up; suggested contracts; and path shifts. |
| **Learn** | Production traces scored without labels, clustered into patterns, drafted into test cases for review, and suites with the loop's coverage, time to test, and recurrences. |
| **Agents** | An agent run's pass rate per check against the baseline, where failing runs first went wrong, efficiency (steps, repeats, tool errors, tokens, cost), runs that got longer, and every trajectory. Trace shows one step by step against its reference. |
| **Failures** | Reported errors or an evaluation run, grouped into causes: each with its kind, confidence, evidence, what sets it apart from passes, and examples. Accept intended changes, confirm or dismiss the rest. |
| **Cost** | Fully loaded cost per document and per page, stacked by component over time; cost by document type, segment or mode; AI spend by model with the fallback share; the rate card. |
| **Measures** | Each measure over time, with the expected range it's judged against, any SLO line, a breakdown of every slice, and an SLO editor. |
| **Prompts** | Every prompt version: documents, error rate, fallback, latency, cost, a verdict against the previous version (adjusted for document mix), what changed, and a diff. |
| **Errors** | Where reported wrong values start: by step in pipeline order, by verdict, field, document type, segment and model; recent errors with their diagnosis; a report form. |
| **Trace** | Why was *this* document slow, lost or wrong? A timeline of every stage and model call, each step's values side by side with wrong ones marked, and problems flagged. |
| **Alerts** | Pending, open and resolved alerts, with how long each lasted. |
| **Release gates** | Every advance, hold or rollback decision, with its lineage. |
| **Connect** | Setup snippets, what your data can answer measure by measure, and one-click backfill. |

Every alert has an **Investigate** link to its slice. `#measures/<id>` and
`#trace/<document_id>` are shareable links.

## Measures

| Group | Measures |
|---|---|
| **Operational health** | `document_volume`, `stage_failure_rate`, `call_error_rate`, `call_latency_p95`, `time_to_complete_p90`, `input_mix_drift` |
| **Cost** | `cost_per_document`, `cost_per_page`, `total_spend`, `human_touch_rate`, `cost_coverage` (see Cost below) |
| **Pipeline integrity** | `fallback_attribution` (does each call record which model tier answered, and why), `model_mismatch` (served ≠ declared), `revision_coverage`, `noop_stage_rate` (stages that report success without doing work), `source_positions` (values a reviewer can click through to), `handoff_loss` (finished documents missing downstream) |
| **Errors** | `reported_error_rate`, `errors_by_origin`, `prompt_error_rate` (see Error analysis and Prompt versions) |
| **Accuracy** | `split_stp`, `field_accuracy`, `superseded_value_rate`, `escape_rate`: listed as *unmeasured* until labelled ground truth can be ingested |

Every measure reports an overall row plus one row per slice value. A missing dimension is
kept as an `(unrecorded)` slice. A source that can't provide the data makes a measure
*unmeasured*, never 0.

`input_mix_drift` is the population stability index against the previous window of equal
length, split into each category's contribution. It tells a moving accuracy number apart
from a moving population.

Adding a measure means writing a class in `assay/measures/` with a `compute(source, window)`
and registering it in `assay/measures/__init__.py`.

## Cost

The price of a model call is the number everyone quotes, and usually the smallest part of what
a document costs. Assay prices every component it can see and says which ones it can't:

| Component | Priced from |
|---|---|
| AI inference | `cost_usd` on calls answered by the model they declared |
| AI inference, estimated | unpriced calls, at the median price of priced calls to the same model at the same stage (else the same model). Always its own line, never mixed into measured cost |
| Fallback escalation | calls answered by a fallback tier (`resolving_layer` isn't primary) or by a different model than declared |
| Human review | review `minutes` × the reviewer rate, or `cost_usd` as recorded |
| Rework | rework `minutes` × the rework rate, or `cost_usd` as recorded |
| Platform | per-document + per-page rates × `page_count` |

The **rate card** holds prices the pipeline can't record itself: reviewer and rework cost per
hour, and platform cost per document and per page. It is set per source, in the Cost tab or with
`PUT /v1/cost/rates`. Nothing is guessed silently:
- Unpriced calls with nothing to estimate from make AI cost a stated floor.
- No review records means people cost is shown as *missing*, not zero.
- Review minutes without a rate are counted and reported.

`cost_per_document` is broken down by component, document type, segment, mode and stage, and
alerts when any of them rises beyond its normal range. For example, the demo's fallback
escalation opens an alert on the *Fallback escalation* component. The band uses the slice's
real standard error from per-document costs, so the heavy tail of expensive documents doesn't
page anyone. `total_spend` is for reporting: it tracks volume, so it's charted but never
raises anomalies. `/v1/cost/breakdown?by=document_type` returns the component mix per
category, computed live.

## Alerting

After every run, each measured slice is checked two ways:

- **Anomaly:** the value leaves the range learned from that slice's last 8 runs. The band's
  half-width is the largest of: 3× the robust run-to-run spread, 3× the sampling error at
  this sample size (binomial for rates, Poisson for counts), and a minimum width. So small
  slices get wide bands and a flat history never produces a zero-width band.
  Measures with a direction alert only when they get worse. Volume and drift alert either way.
- **SLO:** the value is on the wrong side of a target. Targets can apply to the overall
  value, to one slice, or to *every* slice of a dimension, for example "no customer loses
  more than 5% at the handoff". The most specific target wins.

A condition seen once is **pending** and notifies nobody. It **opens** after 2 consecutive
runs (`ASSAY_ALERT_AFTER_RUNS`) and **resolves** on the first run where it no longer holds.
Slices under 30 (`ASSAY_ALERT_MIN_N`) are never judged. Resolving works the same way: an alert
closes only after 2 clear runs in a row. While open, an anomaly is judged against the range it
opened with, so a sustained shift stays open instead of quietly becoming the new normal.
Band widths come from each slice's normal noise in history, not from the current run, because
an incident often increases the spread too.

Notifications go to `ASSAY_WEBHOOK_URL`: Slack format by default, or structured JSON with
`ASSAY_WEBHOOK_FORMAT=json`. Messages link straight to the measure when `ASSAY_PUBLIC_URL`
is set.

## Release gates

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

### Nondeterminism: flaky checks, and rerun instead of block

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

## Deploy

**Docker:** `docker build -t assay . && docker run -p 8400:8400 -v assay-data:/data assay`

**Vercel:** import the repo at vercel.com/new. There are no build settings to change,
because Vercel detects the FastAPI `app` in the root `app.py`.
- **With no environment variables**, results go to SQLite in `/tmp`. Each fresh instance
  loads the demo on its first request, and the data is lost when the instance is recycled.
  That's fine for a showcase.
- **For real use**, set `ASSAY_STORE_URL` to a Postgres URL, for example from Neon in the
  Vercel marketplace. Also set `ASSAY_SOURCE_URL` and `ASSAY_SOURCE_MAPPING`, or use events.
- **Scheduled runs:** serverless has no background process. Set `CRON_SECRET` (and
  `ASSAY_SCHEDULE_SOURCES` for scheduled measures), then add a `vercel.json` with
  `"crons": [{"path": "/v1/cron", "schedule": "0 6 * * *"}]`. Each call also marks quiet
  agent runs abandoned and evaluates them. Runs that end are evaluated when they end,
  without cron.
- Create keys (`python -m assay keys create …`) or set `ASSAY_ADMIN_KEY` before sharing the URL.
  Until then the server is in open mode.

All settings are listed in `.env.example`.

## Layout

```
sources/      sql.py (any database, via a mapping), events.py (pushed events; agent steps read as stages and calls)
measures/     operations.py, cost.py, pipeline.py, ground_truth.py; each a class with compute()
cost.py       cost ledger: components, estimates, breakdowns
alerts.py     bands, SLO matching, pending → open → resolved, contract alerts, webhook
workflow.py   the pipeline as a graph, inferred from stage runs
contracts.py  path contracts, checking every document's path, shifts, suggestions
failures.py   failures into causes: per-failure evidence, grouping, contrast with passes, kinds
flaky.py      pass rates per check from attempts, exact tests, flaky / rerun / got worse, release call
agents.py     trajectories: reference comparison, end state, checks, credit assignment, efficiency
learn.py      production → anomaly scores → patterns → draft cases (with provenance, PII redaction) → suites
trace.py      per-document trace and flags; slowest / stuck / lost finders
rootcause.py  error localization: which step a wrong value started at, and how
prompts.py    prompt registry, per-version results, version-vs-previous comparison, diffs
coverage.py   what a source can answer, and which field unlocks the rest
schema.py     the v1 event schema (runs, steps, outcomes): models, JSON Schema, and where each event lands
client.py     the older per-record SDK (POST /v1/events); new integrations use sdk/python
connect.py    no-code setup: what's arrived and what it switches on, spreadsheet import, hand-off instructions
integrations.py  Slack, Jira, Linear (secrets masked), and CI release-check jobs
auth.py       API keys, scopes, tenant isolation, rate limits
ingest.py     event contract: validation, idempotent upserts, OpenTelemetry mapping
gates.py      noise floor (spread of identical runs), paired bootstrap CI, advance / hold / rollback
runner.py     compute + persist a run (one fetch per window), history, what-changed
scheduler.py  in-process periodic runs
api.py        FastAPI; dashboard in static/index.html
```

Outside `assay/`:

```
sdk/python/          the assay-evals SDK on PyPI (standard library only)
docs/                event-schema.md (the design) and event-schema-v1.json (the JSON Schema)
.github/workflows/   publish-sdk.yml: a tag sdk-v<version> → tests → TestPyPI → your approval → PyPI
tests/               pytest, including the SDK against a real server
```

## Not built yet

- **SDKs for other languages:** only Python has an SDK. Other languages send the event
  schema to `POST /v1/ingest` directly, or use OpenTelemetry.
- **Accuracy from eval runs:** evaluation results are grouped into causes, but the accuracy
  measures don't read them yet.
- **Naming causes:** cause names come from templates. An LLM could write better one-line names
  (only names, never kinds).
- **Users and SSO:** keys are the only identity. There are no user accounts, SSO or audit log
  of who changed what.
- **Shared rate limits:** limits are per instance, in memory. Use a shared store (such as
  Redis) when running several replicas.
- **Alert routing:** there is no per-team routing, silencing, acknowledgement or on-call
  paging (PagerDuty).
- **Scale:** measures compute in Python over fetched rows. That is fine for tens of thousands
  of calls per window. Push aggregation into SQL before running at production volume.
- **Timing noise:** p90 completion time on small slices is the noisiest alert. It needs a
  bootstrap error estimate.
- **Migrations beyond adding:** on start, Assay adds new tables and columns to an existing
  database. Renaming or removing columns would need a real migration tool (Alembic).
- **Secrets at rest:** Slack, Jira and Linear tokens are masked in the API but stored as
  plain text in Assay's database. Protect the database, or add encryption with a server key.
