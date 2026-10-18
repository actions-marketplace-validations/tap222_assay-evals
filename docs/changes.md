[Assay](../README.md) › [Documentation](README.md)

# What nothing checked: `assay changes`

A pull request can pass every test and still ship code no test ran, or a change to a
requirements file, a workflow or a Dockerfile that no CI job used. `assay changes` lists both,
against the base branch. It works on any repository, with or without Assay tests.

```bash
pytest --cov=myapp --cov-report=json     # or: npx jest --coverage, or anything that writes lcov
assay changes --base main
```

```
Against main: 6 changed files.

CHANGED, NOT RUN BY ANY TEST (9 lines, 3 places; 41 of 50 changed lines ran)
  1. app/billing.py: Billing.refund, line 88 (money)
  2. app/client.py: upload_run, lines 120-126 (outside calls)
  3. app/demo.py: no test loaded this file, line 12

DEPLOY AND DEPENDENCY CHANGES NO CI JOB CHECKED
  - .github/workflows/release.yml: doesn't run on pull requests, only on push, tags: its first run is after the merge
  - requirements.txt: no job on pull requests installs or builds from it
```

Nothing here fails the run: it's what to look at before merging.

## Changed lines no test ran

The changed lines come from `git diff` against the merge base with the base branch, uncommitted
changes and new files included. Whether a test ran them comes from the coverage your test run
left, the first of these that's found (or `--coverage FILE`):

| File | Written by |
|---|---|
| `coverage.json` | coverage.py: `pytest --cov --cov-report=json`, or `coverage json` |
| `.coverage` | coverage.py's data file (needs `coverage` installed) |
| `coverage/coverage-final.json` | Jest or Vitest with `--coverage` (istanbul) |
| `coverage/lcov.info`, `lcov.info` | lcov, which Go, Rust, Ruby, Java and most others can write |

Lines coverage doesn't count, like comments and blank lines, are left out, and so are test
files. Unrun lines are grouped by the function they're in. A file no test ran at all is one item,
and so is a changed file of a measured language that coverage never saw.

Each item is ranked by what its unrun lines do: **money** (charges, refunds, prices) first, then
**messages** (email, SMS, webhooks), **logins** (passwords, access tokens, permissions),
**database writes** and **outside calls** (HTTP), then the rest. At most three items are shown;
`--all` lists every one.

Why lines and not functions: replaying this repository's history, a changed function no test ran
would have flagged 1 of 9 bugs later fixed by hand. The upload bug sat in a function tests did run,
just not on the line that sent the request ([P0 findings](p0-findings.md)).

## Deploy and dependency changes no CI job checked

Four of those nine bugs were in files coverage can't see: a requirements file, an ignore file, a
workflow and a version pin. A change to one of these is listed only when no job in a workflow that
runs on pull requests used it:

| Changed | Counts as checked when a pull-request job |
|---|---|
| A workflow | is that workflow (a release workflow that runs on tags isn't) |
| `action.yml` | uses the action (`uses: ./`) |
| `requirements*.txt`, `pyproject.toml`, `setup.py`, lock files | installs or builds from it (`pip install -r`, `pip install .`, `uv sync`, `poetry install`, `python -m build`) |
| `package.json`, lock files, `.npmrc`, `.nvmrc` | installs or runs in its directory (`npm ci`, `yarn`, `pnpm install`) |
| `Dockerfile`, `.dockerignore`, compose files | builds an image |
| `vercel.json`, `.vercelignore`, `netlify.toml`, `fly.toml`, `Procfile`, `.python-version` | deploys with Vercel, Netlify, Fly or Render |

A manifest whose changed lines are only its version or description isn't listed, and neither is
an `action.yml` whose inputs changed but not its steps. Files under `examples/`, `docs/` and
`tests/` aren't deployed, so they're skipped. A preview deploy run by a hosting provider's own
GitHub app, outside the workflows, isn't seen.

Replayed over this repository's history, the rule lists all four of those bugs. Of the 52 changes
that touched such a file, it lists 26. Most are from before pull requests ran CI here, when nothing
checked these files. Since then it lists 7 changes, 2 of them the bugs; most of the others change
release workflows, which only run after a merge.

## In the pull request

With the GitHub Action, `changes: true` adds this section to the PR comment and the job summary.
Run your tests under coverage in `command`, and check out with `fetch-depth: 0` so the merge
base is known:

```yaml
- uses: actions/checkout@v4
  with:
    fetch-depth: 0
- uses: tap222/assay-evals@v1
  with:
    command: pytest --assay --cov=myapp --cov-report=json
    install: pip install assay-server pytest pytest-cov
    changes: true
```

`--format markdown` prints the same section for any other CI, and `--format json` gives everything
for a coding agent to act on.
