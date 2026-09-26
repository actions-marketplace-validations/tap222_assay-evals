[Assay](../README.md) › [Documentation](README.md)

# Not built yet

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
