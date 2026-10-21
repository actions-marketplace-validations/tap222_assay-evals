---
name: assay
description: Run this project's AI regression tests with Assay after changing anything the AI's behavior depends on (a prompt, the model or its settings, a tool, retrieval, agent or pipeline code), or when the user asks whether a change broke the AI. Reports which cases regressed, how their behavior changed, and what caused it.
---

# Assay: check the AI's behavior after a change

This project tests its AI app with Assay. Each test is compared with its own last passing run
(its baseline), so a change that makes the app behave differently fails even when the answers
still look right.

## When to run it

After you change a prompt or prompt file, the model or its settings, a tool's code or
description, retrieval, or the agent or pipeline code. Also when the user asks whether a change
broke something. Skip it for changes that can't reach the AI, such as docs or styling.

## How

1. Read `assay.toml` first rather than guessing the folder. When its `[test] command` is pytest,
   run it with `--assay` added: `pytest -q tests/ai` becomes `pytest -q --assay tests/ai`. Any
   other command (Jest, Vitest, `node --test`: a JavaScript or TypeScript project), run
   `assay test`, which runs that command and compares the cases it records. Without a
   `[test] command`, run pytest on the tests that take the `assay_case` fixture.
2. Read the exit code:
   - `0`: nothing got worse. Say so in one line.
   - `1`: a case regressed. Go on to step 3.
   - `6`: inconclusive. Nothing got worse, but some results couldn't be judged or need more
     attempts. Say which, and suggest `assay test --repeat 3`.
   - anything else: the tests didn't run. Report the error.
3. Run `assay diff`. For each regression, tell the user the case, how its path changed
   (Expected and Actual), the check that failed, and what changed next to it, such as a prompt
   line. Tie it to the edit you just made where you can.
4. Propose a fix to your change, and run the tests again after it.

## Rules

- Never run `assay accept` or `assay ack` yourself. They make the new behavior the baseline.
  Suggest them only when the user says the change in behavior is intentional.
- Never make a test pass by weakening it: don't remove or loosen checks, asserts, `expect(...)`
  or the contracts in `assay.toml`, and don't skip or delete a test.
- Don't delete `.assay/`: it holds the baselines.
- If `assay` or `pytest` isn't found, tell the user to run `pip install assay-server pytest`. In a
  JavaScript or TypeScript project, `assay` comes from `pipx install assay-server`, and the tests
  need `npm install --save-dev assay-evals`.
- If the repository has no Assay tests (no `assay.toml`, no test taking the `assay_case` fixture or
  calling `assayCase()`),
  say so once, and offer to set them up with `assay init` and a first test for the code you
  changed. Don't set them up without the user's OK.
- With `ASSAY_URL` and `ASSAY_UPLOAD=1` set, each run also goes to the Assay dashboard, filed
  under this repository's name. Nothing to do for that.
