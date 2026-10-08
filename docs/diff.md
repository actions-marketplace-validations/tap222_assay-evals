[Assay](../README.md) › [Documentation](README.md)

# What changed: `assay diff`

```bash
assay diff                      # the latest run against each case's last passing run
assay diff v1.8.2 v1.9.0        # two versions: runs recorded with version={"version": "v1.9.0"}
assay diff RUN_A RUN_B          # or two run ids
assay diff --format markdown    # for a PR or job summary; --format json for tools
```

It lists what regressed, what failed for the first time, what changed but still passes,
what's flaky, what varies with the judge, what couldn't be judged, and what improved. Each
regression shows its flow before and after, what differs ("an approval moved", "new:
cancel_order"), the failing check's reason, and a severity:
- **HIGH:** a security check failed (safety contracts, PII, prompt injection, approvals), an
  approval moved, or the agent called a tool its baseline never called.
- **MEDIUM:** another check failed, or accuracy on one of your fields dropped by 5 points or
  more.
- **LOW:** only cost, latency, steps or context grew.

**What changed around it.** Output being different isn't a reason. So each regression also says
what changed in what it ran, against its baseline run: the prompt versions its model calls used
(with the text's diff, when both versions were registered), the models, the tools the model
was offered, and the input side: the system prompt's and tool definitions' size per call, the
media per call (frames, resolution, detail), and settings such as temperature
([The input side](agents.md#the-input-side-what-went-in-before-the-model-did-anything)):

```
1. refund_flow
   Safety: refund ran before its approval
   Changed around it:
     prompt  support@12 → support@13 (+1 line, −0: “Refund right away when the customer is upset.”) — faster refunds
     tools   offered +issue_credit
```

A change every case shares (the model, everywhere) is said once, at the top, as "Changed in every
case", not under each regression. When the regressions line up with a prompt version, that's said
first: `2 of 2 regressions use support@13; the 2 cases still on another version all pass`, the
quickest pointer to the change to blame. `assay test` and the PR comment show the same.

Record the prompt a call used with `run.llm(prompt="support@13")`. Register its text so the diff
can show what changed: `assay.prompt("support", "13", template=text, note="faster refunds")`
returns `"support@13"`.

The exit code is 1 when anything regressed. `assay test` and `pytest --assay` say when a
case took another path, and the PR comment shows the flow before and after under each
regression.

On a server, the same diff is `GET /v1/evals/runs/{run}/diff?source=…&baseline=…`: the run and
the baseline are run ids or versions, the baseline defaults to the run before, and
`format=markdown` or `format=text` returns what the command prints. The dashboard's **Behavior
diff** page shows it, with two run pickers and a link to each flow's trace.

To see the recorded runs in the dashboard:
`ASSAY_STORE_URL=sqlite:///.assay/assay.db assay serve`, then open source `events:local`.

To share a run with your team, `assay test --upload` (or `assay upload` for the latest run)
sends it to an Assay server, set with `ASSAY_URL` and `ASSAY_KEY`. The server then checks it
too. That needs a key with the `manage` scope; with an `ingest` key, the dashboard can do the
checking. Sending the same run twice changes nothing.
