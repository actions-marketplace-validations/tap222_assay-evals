[Assay](../README.md) › [Documentation](README.md)

# API and authentication

The full reference, with an **Authorize** button, is at `/docs` (OpenAPI at `/openapi.json`).

## Keys

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

## Single sign-on, people and roles

People sign in to the dashboard with the company's identity provider instead of pasting a key:
Okta, Microsoft Entra ID, Google Workspace, Auth0, Keycloak, or anything that speaks OpenID
Connect. Keys keep working alongside, for pipelines and CI.

```bash
pip install "assay-server[sso]"
ASSAY_OIDC_ISSUER=https://acme.okta.com          # the provider's issuer URL
ASSAY_OIDC_CLIENT_ID=0oa1...  ASSAY_OIDC_CLIENT_SECRET=...
ASSAY_PUBLIC_URL=https://assay.acme.internal     # the provider sends people back to /auth/callback here
ASSAY_SESSION_SECRET=$(openssl rand -hex 32)     # signs sessions; keep it secret, and the same on every replica
ASSAY_OIDC_ADMINS=assay-admins                   # groups (or emails) that are admins
ASSAY_OIDC_MANAGERS=ml-team,platform             # ... managers; everyone else who may sign in reads
ASSAY_OIDC_ALLOWED_DOMAINS=acme.com              # optional: who may sign in, by email domain
```

Register Assay with the provider as a web application, with the redirect URI
`https://assay.acme.internal/auth/callback`, and have the ID token carry the groups claim
(`ASSAY_OIDC_ROLE_CLAIM` if yours is named otherwise). People belong to `ASSAY_OIDC_TENANT`
(`default`), or to the tenant a claim names (`ASSAY_OIDC_TENANT_CLAIM`).

- **Verification:** the authorization code flow with PKCE, a state and a nonce. The ID token's
  signature is checked against the provider's published keys (rotated keys are refetched), and so
  are its issuer, audience, expiry and nonce. Unsigned or HMAC-signed tokens are refused.
- **Roles** are the scopes above: `read`, `manage`, `admin`. They come from the groups claim at
  every sign-in, so removing someone from a group takes effect at their next one. An admin can
  set a person's role over their claims, or disable them (`GET /v1/users`, `PUT /v1/users/{id}`
  with `{"role": "manage"}`, `{"role": null}` to go back to the claims, or `{"disabled": true}`,
  which ends their sessions at once). Nobody can take away their own admin role.
- **Sessions** are signed, HttpOnly cookies, `Secure` over HTTPS, and expire after
  `ASSAY_SESSION_HOURS` (12). A change made with a session also needs the `X-CSRF-Token` header
  to match the `assay_csrf` cookie, so another site can't make one on a signed-in person's
  behalf. The dashboard sends it.
- **With SSO on, the server is never in open mode:** every request needs a session or a key.

## The audit log

Every change is recorded: who (a person, `key:<id> (<name>)`, or `ASSAY_ADMIN_KEY`), what
(method and path), when, and the result, refusals included. So are sign-ins, refused sign-ins
and why, and sign-outs. `GET /v1/audit` (admin) lists them, newest first, for your tenant;
`?actor=ana@acme.com` narrows it. Event ingestion is left out: it's volume, not a change of
anything.

## Sending data

**New integrations should use the event schema.** [`docs/event-schema.md`](event-schema.md)
describes one contract for runs, steps and outcomes (feedback, test checks, corrections,
expectations). It streams to `POST /v1/ingest`, and its JSON Schema is at `GET /v1/schema`.
The Python SDK, [`assay-evals`](https://pypi.org/project/assay-evals/), is the smallest way
to send it: `pip install assay-evals`, with no dependencies (source in
[`sdk/python`](../sdk/python/README.md)). The endpoints below keep working.

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

**OpenInference spans** are read too, so traces from Arize Phoenix and other instrumentors that
follow OpenInference need no change: send the same spans to Assay (a second exporter in the
collector) and use it as the regression gate on top of the tracing you already have. By
`openinference.span.kind`:

| Span | Becomes |
|---|---|
| `LLM` | a model call: `llm.model_name`, `llm.token_count.prompt` / `completion`, `llm.cost.total`, `llm.prompt_template.version`, and `llm.input_messages` for the input by part (system prompt, history, user) |
| `TOOL` | a tool call: `tool.name`, with `input.value` its arguments and `output.value` its result |
| `RETRIEVER` | a retrieval step: `retrieval.documents.N.document.{id, content, score}`, the query from `input.value` |
| `AGENT` or `CHAIN` at the root | the run: `input.value` what it was asked, `output.value` its answer, its name the task |

`session.id` is the conversation and `user.id` the user. A trace with a tool or retriever span, or
an `AGENT` or `CHAIN` root, becomes an agent run, checked like any other. A span's own `gen_ai.*`
or `assay.*` attributes always win over what's read from its OpenInference ones.

```yaml
exporters:
  otlphttp/assay:
    endpoint: https://assay.example.com/v1/otlp
    encoding: json
    headers: { Authorization: "Bearer ${env:ASSAY_KEY}" }
```

## Alert webhooks

With `ASSAY_WEBHOOK_SECRET` set, each webhook carries `X-Assay-Timestamp` and
`X-Assay-Signature: sha256=<hex>`, the HMAC-SHA256 of `"<timestamp>.<body>"` with the secret.
Check it, and reject stale timestamps, to know a request came from Assay:

```python
expected = hmac.new(secret, f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
assert hmac.compare_digest(f"sha256={expected}", signature) and abs(time.time() - int(ts)) < 300
```
