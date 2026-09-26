[Documentation](README.md)

# Synthetic data: error analysis before there's traffic

Use it to start error analysis before there are users, or to test a failure too rare to find in
real traffic. It can't tell you how common a failure is, and it can miss what matters in a
specialized domain. So synthetic runs are always labeled, never counted as production, and compared
with real traffic as soon as there is some.

"Give me test queries" gets generic, repetitive ones. `assay synth` does it the structured way.

## 1. Dimensions, and tuples by hand

A dimension is one kind of variation in what users send. Pick them from failure hypotheses: what
you expect to go wrong, from using the app yourself.

```toml
[synthetic]
app = "app/bot.py:answer"            # the entry point queries go through
about = "A support assistant for a software subscription"
prompts = ["app/prompts/system.txt"] # optional: default, the system prompts found in the code

[[synthetic.dimensions]]
name = "Issue type"
values = ["billing", "technical", "general"]
hypothesis = "Promises refunds it can't give"

[[synthetic.dimensions]]
name = "Customer mood"
values = ["frustrated", "neutral", "happy"]

[[synthetic.dimensions]]
name = "Prior context"
values = ["new issue", "follow-up", "resolved"]
```

A tuple is one value per dimension. Write 20 by hand first: that's where the problem space is
learned, and generation won't scale before it (`--force` does anyway, with a warning).

```
assay synth tuple "Issue=billing" "mood=frustrated" "Prior=follow-up" --note "the refund promise"
```

## 2. Two steps: tuples, then queries

```
assay synth tuples                  # every combination, and a model drops the ones no user would send
assay synth tuples --direct -n 80   # or: a model writes realistic combinations, yours as examples
assay synth queries                 # each tuple as a message, in a prompt of its own
```

Every combination, then a filter, covers rare cases. Use it when most combinations are valid.
`--direct` gives more realistic combinations but drifts to the typical user; use it when many
combinations make no sense. Dropped combinations stay in `synthetic/tuples.jsonl` with the reason.

Each tuple becomes a query in a separate prompt, shown how the last ones were phrased, and a query
phrased like an earlier one is dropped. The result, `synthetic/queries.jsonl`, is for a person to
read: set `"status": "rejected"` on any no user would send.

## 3. Fix obvious problems first

```
assay synth check
```

It shows how many tuples each value has, and the values the system prompt never mentions: "Customer
mood: frustrated". If the app should handle one, say so in the prompt. That's a fix, not a test to
generate. It's a hint: a prompt can handle a case without naming it.

## 4. Through the real app

```
assay synth run                     # every kept query through [synthetic] app
```

Each query goes through the app's own entry point and is recorded as a run tagged
`origin=synthetic`, with its tuple (`dim.<name>` tags). If the app opens runs of its own
(`@assay.agent`, `assay.run`), those carry the tags; otherwise one is opened around the call. With
no server, they're recorded to `.assay/events.jsonl` (`assay load`, then `assay serve`).

They appear in the Review tab, labeled synthetic with their tuple, and the diverse first sample
spreads over them. A pool of about 100 diverse traces is a good start: read 30 yourself, then let
the model search for more, until saturation ([learning](learning.md)).

## 5. Kept apart from production

A synthetic run is never production. It isn't in a category's share, isn't scored by `assay
learn`, and isn't in the report's usage. A category found only in synthetic runs says so
(`only_synthetic`), and the report asks whether real users hit it.

## 6. Against real traffic

```
assay synth compare --source events:acme     # POST /v1/synthetic/compare
```

Production conversations are placed on the same dimensions by a model (up to `--sample`, each once).
For each dimension:

- values users hit that nothing generated reaches;
- values generated that no user hits;
- the share of conversations that fit no value: a value, or a dimension, is missing.

It also lists categories found only in synthetic runs, and only in production.

## 7. Conversations

```
assay synth personas                # each tuple as a persona: goal, manner, facts given when asked
assay synth run --personas          # each one talks to the app, a simulated user playing it
```

In a test: `for p in assay_sdk.load_personas(): simulate(agent, p, user=Judge(...), run=assay_case)`.
The app is called as `app(message)` or `app(message, history)`.

Generation uses the `[judge]` provider and model.
