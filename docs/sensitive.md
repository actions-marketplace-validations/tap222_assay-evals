[Documentation](README.md)

# Evals when traces hold sensitive data

Nothing replaces looking at real interactions. When you can't look at all of them, these are the
options, best first. Synthetic data ([synthetic](synthetic.md)) is the last resort: it finds
early problems, and says little about how real users behave.

## 1. Traces you're allowed to see

A customer may share some traces; test users may agree to let you read theirs. Mark those runs:

```python
with assay.run("support", input=q, tags={"consent": "shared"}): ...
```

or afterwards, in bulk: `POST /v1/consent?source=events:acme {"trace_ids": [...], "conversations": [...]}`
(`"shared": false` withdraws it). With `ASSAY_REVIEW_CONSENTED_ONLY=true`, the Review tab shows
only consented conversations (and synthetic ones) to anyone without sensitive access, and a model
only ever reads consented ones: the review run, the search for more of a category, and the
synthetic comparison.

## 2. Experts who are allowed to see the data

A clinician may see what you can't. Give them the `sensitive` scope: an API key with it, or, with
SSO, a group in `ASSAY_OIDC_SENSITIVE`. It's never implied by another scope, `admin` included.

With it, the Review tab shows conversations unredacted, every conversation instead of only
consented ones, and each view goes in the audit log (`viewed raw conversations`, which
conversations, who). Everyone else sees them redacted. The notes an expert writes are stored
redacted, like everything kept: the same limits apply to their decisions.

## 3. Let experts check small pieces while they work

"Was this helpful?" says little about what went wrong. Show the expert the evidence behind each
claim, and flag sources that disagree. Then record what they decide, claim by claim:

```python
assay.claim_review(run_id, "No dose change needed", "wrong",
                   evidence=[{"id": "label-b"}], correction="Halve the dose of B", by="clinician-7")
assay.claim_review(run_id, "Onset in two days", "conflict_resolved", note="The label over the review")
```

The verdict is `supported`, `wrong`, `conflict_resolved` or `unsure`. The decisions become:

- **labels**: `assay golden claims` adds supported (1) and wrong (0) claims to `golden.jsonl`, the
  correction as the critique, to calibrate the faithfulness judge against the experts (`score_range
  = [0, 1]`, see [calibration](calibration.md)). `GET /v1/claims/golden` has them as JSON lines;
- **signals**: a claim marked wrong makes its trace anomalous in `assay learn`, like a reported error;
- **findings**: the report counts them ("Experts checked 40 claims in 12 answers: 31 supported, 6
  wrong, 3 conflicts between sources resolved"), and `GET /v1/claims` has the counts.

They're redacted like every other event (`assay.init(redact=...)`) before they're sent.

## 4. Redact, and check the redaction

Redact before it's recorded: `assay.init(redact=...)`. Assay's own redactor
(`assay.learn.redact`) finds emails, card numbers (checksum-valid), IBANs, US SSNs, IP addresses,
phone numbers, street addresses, and names and birth dates after a cue ("my name is", "born on").
Every redactor misses things, so check its output:

```
assay redact check                         # .assay/events.jsonl, or --file
assay redact check --url $ASSAY_URL --source events:acme --days 7   # what the server stored
```

It lists what got through, by kind and field ("email in tool.args: 12, e.g. ana…om"), masked, and
exits 1 if anything did. It also names a few runs to read yourself: a redactor misses what it has
no pattern for.

## 5. Check that edited traces still behave like the real ones

A placeholder like `<email>` can change what the app does: a validation fails, a lookup finds
nothing. Stand-ins keep the shape: `assay.learn.pseudonymize()` replaces each person, number or
address with a realistic fake, the same one every time it appears (person1@example.com, a published
test card number, a 555 phone number, an address on Example Street). Approving a production trace
as a test case can use them: `{"stand_ins": true}` on `POST /v1/learn/candidates/{id}/approve`.

Then check the edit didn't change the behavior:

```
assay redact replay --app app/bot.py:answer                  # stand-ins
assay redact replay --app app/bot.py:answer --mode placeholder
```

Each recorded input with personal data is run through the app with it replaced, and compared with
the recording: the tools called, in order, how the run ended, and the answer (stand-ins mapped
back). The original is run again too, so a run that varies without the edit is reported as the
app's own variation, not the edit's. It exits 1 when an edit changed what the app did. Nothing it
runs is sent anywhere.
