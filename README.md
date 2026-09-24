# Assay

Evaluation and observability for document-AI pipelines. It is the product form of the
DocAI Core Evaluation Roadmap: named measures, reported per slice, with alerting,
per-document tracing, and release gates that decide on real signal instead of
infrastructure health.

It runs as a separate service. It **reads** a pipeline's data and never writes to it.

## Quick start (demo data, no database needed)

```bash
pip install -e ".[dev]"
python -m assay demo      # synthetic tenant, 7 weeks of daily runs, staged incidents
python -m assay serve     # http://127.0.0.1:8400  (API docs at /docs)
pytest                    # 50 tests
```

The demo tenant is generated data shaped after the roadmap's findings. It includes four
staged incidents: an indexing failure spike that resolves, slow classification calls, a new
county flooding the input mix, and Cook IL documents going missing downstream. It is not
real pipeline data.

## What you get

| View | Answers |
|---|---|
| **Overview** | What's broken right now? Open anomalies, SLO state, and the slices that moved beyond noise since the last run. Refreshes every minute. |
| **Measures** | Each measure over time, with the expected range it's judged against, any SLO line, a breakdown of every slice, and an SLO editor. |
| **Trace** | Why was *this* document slow, lost or wrong? A timeline of every stage and model call, with problems flagged. Lists the slowest, stuck and lost documents to start from. |
| **Alerts** | Pending, open and resolved alerts, with how long each lasted. |
| **Release gates** | Every advance, hold or rollback decision, with its lineage. |

The overview and measure pages link to each other. Every alert has an **Investigate** link to
its slice, and handoff or latency measures link to the documents behind the number.
`#measures/<id>` and `#trace/<document_id>` are shareable links.

## Measures

| Group | id | Roadmap |
|---|---|---|
| Operational health | `document_volume`, `stage_failure_rate`, `call_error_rate`, `call_latency_p95`, `time_to_complete_p90`, `input_mix_drift` | Rate / errors / duration, Measure 13, V2-4 |
| Pipeline integrity | `fallback_attribution`, `model_mismatch`, `cost_coverage`, `revision_coverage`, `noop_stage_rate`, `source_positions`, `handoff_loss` | Measures 2, 5, 7, 8, 9, 12 |
| Accuracy | `split_stp`, `field_accuracy`, `superseded_value_rate`, `escape_rate` | DEV-NEW-2/1/7/8; *unmeasured* with the reason until ground truth exists |

Every measure reports an overall row plus one row per slice value (county, instrument type,
stage, model, mode). A missing dimension is kept as an `(unrecorded)` slice. A source that
can't provide the data makes a measure *unmeasured*, never 0.

`input_mix_drift` is the population stability index against the previous window of equal
length, split into each category's contribution. It exists so a moving accuracy number can
be told apart from a moving population.

## Alerting

After every run, each measured slice is checked two ways:

- **Anomaly:** the value leaves the range learned from that slice's last 8 runs. The band's
  half-width is the largest of: 3× the robust run-to-run spread, 3× the sampling error at
  this sample size (binomial for rates, Poisson for counts), and a minimum width. So small
  slices get wide bands and a flat history never produces a zero-width band.
  Measures with a direction alert only when they get worse. Volume and drift alert either way.
- **SLO:** the value is on the wrong side of a target. Targets can apply to the overall
  value, to one slice, or to *every* slice of a dimension, for example "no county loses more
  than 5% at the handoff". The most specific target wins.

A condition seen once is **pending** and notifies nobody. It **opens** after 2 consecutive
runs (`ASSAY_ALERT_AFTER_RUNS`) and **resolves** on the first run where it no longer holds.
Slices under 30 (`ASSAY_ALERT_MIN_N`) are never judged. On the demo's 7 weeks, this produced
20 anomaly alerts and caught all four staged incidents. Before persistence and sampling error
were added, the same data produced 118.

Notifications go to `ASSAY_WEBHOOK_URL`: Slack format by default, or structured JSON with
`ASSAY_WEBHOOK_FORMAT=json`. Messages link straight to the measure when `ASSAY_PUBLIC_URL`
is set.

## Point it at DocAI Core

```bash
export ASSAY_DOCAI_URL=postgresql+psycopg://assay_ro:***@host:5432/docai
pip install -e ".[postgres]"
python -m assay check-source                      # which mapped columns exist
python -m assay serve --every 60 --source docai_core   # hourly runs over a 1-day window
```

Table names come from the architecture reference (`documents`, `document_stage_executions`,
`ai_api_calls`, `indexed_data` in the `docai` schema). **Several column names are guesses**
(`model_requested`, `estimated_cost`, `duration_ms`, `status`, `completed_at`, `county`,
`file_hash`, …). `check-source` lists every one it can't find. Fix them in a JSON override
(`ASSAY_DOCAI_MAPPING`, see `mapping.example.json`), or set them to `"NULL"` so the measure
reports *unmeasured*. Use a read-only role; the adapter also opens every transaction
`READ ONLY`.

Scheduling: the built-in scheduler suits a single instance. With several replicas, run it on
one only, or schedule `python -m assay run --source docai_core --days 1` from Celery Beat or
cron. Keep the window and the interval steady, because the baseline compares like with like.

## Deploy on Vercel

The repo is ready to import at **vercel.com/new**. There are no build settings to change:
`vercel.json` routes every path to `api/index.py`, which serves the API and dashboard.

- **With no environment variables**, results go to SQLite in `/tmp`. Each fresh instance
  loads the demo tenant on its first request (about 3–6 s), and the data is lost when the
  instance is recycled. That's fine for showing the product, but it isn't a real store.
- **For real use**, set `ASSAY_STORE_URL` to a Postgres URL, for example
  `postgresql+psycopg://…` from Neon in the Vercel marketplace. Then load the demo once
  from your machine (`ASSAY_STORE_URL=… python -m assay demo`), or point it at DocAI Core
  with `ASSAY_DOCAI_URL`.
- **Scheduled runs:** serverless has no background process. Set `CRON_SECRET` and
  `ASSAY_SCHEDULE_SOURCES`, then add a cron in `vercel.json`:
  `"crons": [{"path": "/v1/cron", "schedule": "0 6 * * *"}]`. The Hobby plan allows daily
  crons only.
- Set `ASSAY_API_KEY` before sharing the URL outside the team.

## Other teams (multi-tenant path)

Teams without a DocAI-shaped database push events instead:
`POST /v1/events/{calls,documents,stage-runs,indexed}` with an `X-Tenant` header, then
schedule `events:<tenant>`. Set `ASSAY_API_KEY` to require `X-API-Key`. The dashboard asks
for the key once.

## Release gates

`POST /v1/gates/evaluate` takes the following:
- per-unit outcomes for baseline and candidate, per slice;
- repeated baseline runs, for the noise floor;
- the lineage (prompt, model, build and corpus are required).

A slice holds if it is unmeasured, below `min_n`, or has no known noise floor. It rolls back
if even the optimistic end of the confidence interval is worse than max(tolerance, noise
floor). It holds if the interval straddles that limit, and advances otherwise.
`severity: "high"` halves the tolerance. The worst slice decides.

## Layout

```
sources/      adapters → canonical records; docai_core.py (read-only SQL), events.py (ingest)
measures/     operations.py, pipeline.py, ground_truth.py; each a class with compute()
alerts.py     bands, SLO matching, pending → open → resolved, webhook
trace.py      per-document trace and flags; slowest / stuck / lost finders
gates.py      noise floor, paired bootstrap CI, advance / hold / rollback
runner.py     compute + persist a run (one fetch per window), history, what-changed
scheduler.py  in-process periodic runs
api.py        FastAPI; dashboard in static/index.html
```

## Not built yet

- **Auth:** there is one shared API key, with no per-tenant keys, users or roles.
- **Alert routing:** there is no per-team routing, silencing, acknowledgement or on-call
  paging (PagerDuty). Everything goes to one webhook.
- **Scale:** measures compute in Python over fetched rows. That is fine for tens of thousands
  of calls per window. Push aggregation into SQL before running hourly at production volume.
- **Timing noise:** p90 time-to-complete on small county slices is still the noisiest alert
  in the demo. It needs a proper error estimate for quantiles, such as a bootstrap.
- **Migrations:** tables are created on startup. Add Alembic before changing the schema.
- **Ground truth:** accuracy measures stay *unmeasured* until DEV-NEW-1 and DEV-NEW-2
  deliver labels.
