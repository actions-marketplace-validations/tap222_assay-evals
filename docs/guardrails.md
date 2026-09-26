[Documentation](README.md)

# Could an evaluator be a guardrail?

An evaluator runs after a response, and measures. A guardrail runs in the request path, before the
user sees the output, and blocks or changes it. Assay is the first kind: it never blocks or fixes
anything in production. It can tell you which of your evaluators could be the second kind.

```
assay evals guardrails                     # the last 30 days (--days), --format json
assay evals guardrails --export guardrails.json
```

```
Guardrail candidates, from the last 30 days: at most 50 ms p95 and 5% false positives.

Could run in the request path:
  has_order_id: 0.2 ms p95, 0% false positives on 25 good

Can't tell yet:
  tone: 0 good outputs labeled; 20 needed to tell its false positives (assay golden add)

Keep them after the fact:
  helpful: an LLM judge: slow, and not the same verdict every time ([guardrails] judges = true to consider it); 2.4 s p95, over 50 ms
  short_enough: 12% false positives, over 5%
```

## What it measures

- **Latency and cost**: every evaluator's time is recorded with its result (`duration_ms`, and
  `cost_usd` for a judge's model call): `evaluate()`, `expect(run)`, the trajectory checks and the
  built-in judge record them; your own `assay.check(..., duration_ms=)` can too. p50 and p95.
- **False positives** (good outputs it fails) and **false negatives** (bad outputs it passes),
  against people's labels. A golden-set item labeled from a recorded run (`assay golden add CASE`
  remembers the run) is matched with what each evaluator said of that same output. A judge uses its
  latest calibration's catch rate instead.
- **Deterministic or not**: an LLM judge is never a candidate unless you say so.

## The thresholds are yours

The trade-off depends on the stakes. Blocking a good output frustrates a user; letting a bad one
through can do harm. In medical advice false negatives cost more; in a creative tool, false
positives do.

```toml
[guardrails]
max_ms = 50                 # p95 in the request path
max_false_positive = 0.01   # good outputs it may fail
max_false_negative = 0.05   # bad outputs it may pass (unset: not required)
min_labeled = 20            # labeled outputs before a rate means anything
judges = false              # consider LLM judges at all
```

An evaluator with too few labels is "can't tell yet", not a candidate.

## After

`--export` writes the candidates (field, evaluator, latency, cost, error rates, an example case) for
whatever guardrail layer you run. Assay keeps testing them like any other check, so a change that
makes one fail good outputs shows up as a regression before it reaches the guardrail.
