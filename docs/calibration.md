[Assay](../README.md) › [Documentation](README.md)

# Judge calibration: does the judge agree with a person?

A judge can return valid JSON, in range, every time, and still be wrong. It can rank a great
answer below a poor one, call everything a 3, or score the same answer 2 and then 4. The checks
elsewhere catch a judge that breaks (an answer that isn't a verdict, the wrong inputs, a judge
that disagrees with itself). Only a golden set catches one that's miscalibrated: outputs a
person scored, spanning terrible to great.

Most of the value is in the golden set, not in the metric. So Assay helps build the set, and
then checks the judge against it on every prompt or model change.

## The golden set

`golden.jsonl`, in the repository, one item a line:

```json
{"id": "q17", "input": "Tell me about a launch that failed", "output": "...", "score": 4, "by": "sam", "tags": ["behavioral"]}
{"id": "q18", "input": "...", "output": "...", "labels": [{"by": "sam", "score": 2}, {"by": "ana", "score": 3}]}
```

An item's label is the median of its labels. `tags` group items (a question type, say), so each
group's calibration is checked on its own.

```
assay golden add tests/ai/test_coach.py::test_star_answer --score 4    # a recorded output, labeled
assay golden add q17 --score 3 --by ana                                # a second person's label
assay golden suggest --field helpful                                   # what to label next
assay golden stats
```

`golden add` takes the input and output from the latest recorded run of that case (or
`--input`/`--output`). `golden suggest` picks recorded outputs spread over what the judge scored
them, so the set spans poor to great instead of piling up typical answers. `golden stats` shows
labels per score (`nothing labeled 1: the judge is untested there`), who labeled, and, for items
two people labeled, how much they agree. That agreement is the ceiling: no judge will match a
person much better than two people match each other.

How big? The report answers it for your set. The Spearman interval narrows as items are added:
at 30 items it's wide, and at 100 it can tell 0.8 from 0.9. Label every score level, and have a
second person label 20 or so items.

## Calibrating

```toml
[calibrate]
judge = "evals/judges.py:helpfulness"   # or "evals.judges:helpfulness": called as judge(input, output)
repeat = 5                              # judgements per item: how much it swings
score_range = [1, 5]                    # the judge's scale
label_range = [1, 5]                    # the labels' (default: the same)
threshold = 3                           # pass at or above, for pass/fail flips
min_drop = 0.05                         # smaller drops in rank correlation are reported, not failed
field = "helpful"                       # the judge's check in your tests, for `golden suggest`
```

The judge is anything `evaluate()` takes: a function returning a score, a dict, JSON text, or an
`assay_sdk.Judge` answer. It runs through `EvalRuntime`, so concurrency, retries and cost apply.

```
$ assay calibrate
Judge calibration  c-20260926-125520-210
────────────────────────────────────────────
evals/judges.py:helpfulness · 60 items · 5 judgements each

Golden set   60 items, labeled by sam (60), ana (20)
             1: 10  2: 12  3: 14  4: 14  5: 10
             people agree: exact 70%, within one 95% (20 items labeled twice): the ceiling for any judge

Ranking      Spearman 0.82 (95% interval 0.71–0.89)
             pairs in the right order: 94% (1,334 of 1,420)
             2 ordering violations two or more apart:
               q17 labeled 5, judged 2.0  <  q04 labeled 3, judged 3.0
Agreement    exact 58%, within one 92%
Bias         +0.40 (lenient) · labeled 1 → judged 2.1 on average
Consistency  mean spread 0.40 across 5 judgements · 4 flip pass/fail: q09, q22, q31, q40
Validity     60 of 60 items judged
By tag       behavioral 0.88 (n=30)   product_sense 0.71 (n=30)

Compared with c-20260919-101204-551 (60 items in both)
  overall        Spearman 0.86 → 0.82 (-0.04, within chance)
  behavioral     Spearman 0.84 → 0.88 (+0.04)
✗ product_sense  Spearman 0.87 → 0.71 (-0.16, beyond chance)

Regressed.
```

- **Ranking:** Spearman rank correlation with the labels, with its 95% interval (a bootstrap over
  the items), the share of pairs in the right order, and the ordering violations themselves.
- **Agreement:** exact and within one, and a label × judge table.
- **Bias:** lenient or harsh on average, the label it's furthest off on, and whether it squashes
  every answer toward the middle.
- **Consistency:** how far each item's score swings across the repeats, and which items flip
  between pass and fail.
- **Validity:** answers that weren't verdicts, and timeouts, are counted apart. They never
  become scores.

## As a regression test

Each calibration is stored and compared with the last one that passed, over the items both
judged: overall and per tag. Improving one kind of item often breaks another. It fails (exit 1)
when:

- a rank correlation dropped beyond chance (a paired bootstrap over the same items) and by at
  least `min_drop`, overall or for a tag with 8 items or more;
- a new ordering violation is two or more label points apart: a 5 now judged below a 2.

Violations one point apart, a lean, and a wider spread are reported, not failed. Run it where
the judge's prompt or model changes:

```yaml
- run: assay calibrate
```

When the judge's model or prompt changes, `assay test` doesn't compare the new judge's results with
the old one's baseline as if only the AI had changed ([When the judge or the model
changes](testing.md#when-the-judge-or-the-model-changes)). Calibrating the new judge is how to
know whether to trust it.

`assay calibrate --baseline none` starts over, `--baseline ID` compares with a given one, and
`--format json` gives it all as data.
