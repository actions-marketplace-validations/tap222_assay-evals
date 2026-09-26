[Assay](../README.md) › [Documentation](README.md)

# Layout

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
