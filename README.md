# Assay

Evaluation and observability for document-intelligence pipelines: OCR, classification,
splitting and field extraction, with or without LLMs.

Assay answers what a pipeline's own logs can't:

- Is anything broken right now?
- Which customer, document type, stage or model is it broken for?
- Which documents are behind the number?
- What does a document really cost, and where does the money go?
- Is this release safe to ship?

It reports named measures per slice, with alerting, per-document tracing, and release gates
that decide on real signal instead of infrastructure health.

It runs as a separate service. It **reads** your pipeline's data and never writes to it.

## Quick start (demo data, nothing to connect)

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

None of it is real data.

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

If you can't expose a database, send the same records from your code. `assay/client.py` is
one file with no dependencies beyond the standard library, so you can copy it into your project:

```python
from client import Assay   # or: from assay.client import Assay
assay = Assay("https://assay.example.com", tenant="acme", api_key="...")
assay.document(doc_id, received_at=start, document_type="invoice", segment=customer, page_count=2)
with assay.stage(doc_id, "field_extraction"):      # timing, status, and failures
    result = extract(doc)
assay.call(doc_id, stage="field_extraction", model_declared="claude-sonnet-5",
           model_served=resp.model, latency_ms=ms, cost_usd=price, status="success")
assay.review(doc_id, minutes=4.5)                  # people time, for cost
assay.document(doc_id, received_at=start, completed_at=datetime.utcnow())  # only sent fields change
```

It batches, retries, and never raises into your pipeline unless `strict=True`. Or send the
records over HTTP directly:

```
POST /v1/events/documents | /v1/events/stage-runs | /v1/events/calls | /v1/events/indexed | /v1/events/reviews
X-Tenant: acme
```

Then run or schedule the source `events:acme`. Each tenant's data is isolated. Set
`ASSAY_API_KEY` to require an `X-API-Key` header.

### 3. See what you get, and backfill

- The **Connect** tab (or `python -m assay coverage --source …`) checks the last 7 days of your
  data. For every measure it says whether it's *live*, *partial* (works, but a field would make
  it more useful) or *blocked*, and names the exact field that would unlock it.
- **Backfill** (`python -m assay backfill --source … --days 30`, or the button) replays past
  days so every slice has a baseline and anything already wrong is flagged on day one. It
  notifies nobody about history, skips days that already have a run, and never disturbs
  current alerts.

## What you get

| View | Answers |
|---|---|
| **Overview** | What's broken right now? Open anomalies, SLO state, and the slices that moved beyond noise since the last run. Refreshes every minute. |
| **Cost** | Fully loaded cost per document and per page, stacked by component over time; cost by document type, segment or mode; AI spend by model with the fallback share; the rate card. |
| **Measures** | Each measure over time, with the expected range it's judged against, any SLO line, a breakdown of every slice, and an SLO editor. |
| **Trace** | Why was *this* document slow, lost or wrong? A timeline of every stage and model call, with problems flagged. Lists the slowest, stuck and lost documents to start from. |
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

## Deploy

**Docker:** `docker build -t assay . && docker run -p 8400:8400 -v assay-data:/data assay`

**Vercel:** import the repo at vercel.com/new. There are no build settings to change,
because Vercel detects the FastAPI `app` in the root `app.py`.
- **With no environment variables**, results go to SQLite in `/tmp`. Each fresh instance
  loads the demo on its first request, and the data is lost when the instance is recycled.
  That's fine for a showcase.
- **For real use**, set `ASSAY_STORE_URL` to a Postgres URL, for example from Neon in the
  Vercel marketplace. Also set `ASSAY_SOURCE_URL` and `ASSAY_SOURCE_MAPPING`, or use events.
- **Scheduled runs:** serverless has no background process. Set `CRON_SECRET` and
  `ASSAY_SCHEDULE_SOURCES`, then add a `vercel.json` with
  `"crons": [{"path": "/v1/cron", "schedule": "0 6 * * *"}]`.
- Set `ASSAY_API_KEY` before sharing the URL.

All settings are listed in `.env.example`.

## Layout

```
sources/      sql.py (any database, via a mapping), events.py (pushed events)
measures/     operations.py, cost.py, pipeline.py, ground_truth.py; each a class with compute()
cost.py       cost ledger: components, estimates, breakdowns
alerts.py     bands, SLO matching, pending → open → resolved, webhook
trace.py      per-document trace and flags; slowest / stuck / lost finders
coverage.py   what a source can answer, and which field unlocks the rest
client.py     standard-library SDK for pushing events
gates.py      noise floor, paired bootstrap CI, advance / hold / rollback
runner.py     compute + persist a run (one fetch per window), history, what-changed
scheduler.py  in-process periodic runs
api.py        FastAPI; dashboard in static/index.html
```

## Not built yet

- **Ground-truth ingest:** the accuracy measures need a way to load labelled values.
- **Auth:** there is one shared API key, with no per-tenant keys, users or roles.
- **Alert routing:** there is no per-team routing, silencing, acknowledgement or on-call
  paging (PagerDuty).
- **Scale:** measures compute in Python over fetched rows. That is fine for tens of thousands
  of calls per window. Push aggregation into SQL before running at production volume.
- **Timing noise:** p90 completion time on small slices is the noisiest alert. It needs a
  bootstrap error estimate.
- **Migrations:** tables are created on startup. Add Alembic before changing the schema.
