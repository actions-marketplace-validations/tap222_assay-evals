[Assay](../README.md) › [Documentation](README.md)

# Security: the agent's, and the pipeline's

**What the checks catch in the agent:**

| Check | Fails when the agent |
|---|---|
| **Safety** (contracts) | calls a tool it must never call, or out of order, or with arguments a rule forbids (`never delete_order`, `refund only_after get_order`, `where`) |
| **Approval** | takes an action that needs sign-off without it (`requires_approval`, `must_get_approval_before`) |
| **PII** | sends personal data to a tool that isn't allowed it, or says personal data in its answer that the request didn't give (someone else's email, card or IBAN; the user's own is fine). `[pii] allow_in_answer` lists kinds an answer may carry. It needs the request recorded (`input=`) |
| **Prompt injection** | obeys instructions that reached it through a tool or resource result ("ignore previous instructions", "you are now", "call delete_account"): after the injected text it calls a tool the text named, breaks a contract, or makes a call its case or plan didn't expect. An agent that reads the text and carries on passes |

**What a pull request can and can't do to the evaluation.** The tests are code, and CI runs
them, as with any test suite. What Assay adds:

- **A PR can't loosen the checks that judge it.** On a pull request, the action reads the base
  branch's `assay.toml` (`trusted-policy`, on by default), and the contracts, `[pii]`,
  `[behavior]`, `[pytest] checks` and `tolerance` are held to it. The PR can tighten them: an
  added contract applies at once. Loosening them (removing a contract, turning a check off,
  allowing a tool more personal data, raising a limit) is listed, isn't applied, and fails the
  run. The PR comment says so: "Checks weakened: this PR loosens the checks that judge it". A
  maintainer accepts the change with the `assay-policy-change` label (add `labeled` to the
  workflow's `pull_request` types, so labelling re-runs it). Outside the action, set
  `ASSAY_POLICY` to the trusted `assay.toml` and `ASSAY_POLICY_CHANGE=accepted` to accept.
- **No code in the config.** `assay.toml` is TOML, and contracts are rules, not code: no
  inline scripts, no expressions, no regular expressions.
- **A fork's PR gets nothing to steal.** The action runs on `pull_request`, where a fork's PR
  gets a read-only token and no secrets (so no `ANTHROPIC_API_KEY` either). The action stops
  under `pull_request_target`, which would run the PR's code with write access and secrets.
- **Baselines can't be poisoned.** Only pushes to the default branch save them; a PR restores.
- **The PR comment is text.** Test names and failure messages come from the PR, so they go in
  escaped: no `@`-mentions, links, images or HTML.
- **The judge doesn't take personal data out.** Traces are redacted before they're sent to the
  model API (`[judge] redact`, `ASSAY_JUDGE_REDACT`), and the trace is marked as data, so
  instructions inside it don't steer the score.

What it doesn't do: sandbox the tests. They run on the CI runner, like any test suite; run
untrusted code only where a read-only token and no secrets are all there is to reach.
