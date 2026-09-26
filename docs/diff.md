[Assay](../README.md) › [Documentation](README.md)

# What changed: `assay diff`

```bash
assay diff                      # the latest run against each case's last passing run
assay diff v1.8.2 v1.9.0        # two versions: runs recorded with version={"version": "v1.9.0"}
assay diff RUN_A RUN_B          # or two run ids
assay diff --format markdown    # for a PR or job summary; --format json for tools
```

It lists what regressed, what failed for the first time, what changed but still passes,
what's flaky, what couldn't be judged, and what improved. Each regression shows its flow
before and after, what differs ("an approval moved", "new: cancel_order"), the failing
check's reason, and a severity:
- **HIGH:** a security check failed (safety contracts, PII, prompt injection, approvals), an
  approval moved, or the agent called a tool its baseline never called.
- **MEDIUM:** another check failed, or accuracy on one of your fields dropped by 5 points or
  more.
- **LOW:** only cost, latency, steps or context grew.

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
