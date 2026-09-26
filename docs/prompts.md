[Assay](../README.md) › [Documentation](README.md)

# Prompt versions

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
`calls.prompt_version`). `POST /v1/prompts` registers a version over HTTP. In tests, with the
SDK's runs: `run.llm(prompt=assay.prompt("support", "13", template=text, note="..."))`, and a
regression shows what changed in the prompt next to it ([assay diff](diff.md)).

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
