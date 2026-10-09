[Assay](../README.md) › [Documentation](README.md)

# Getting started

Add Assay to the AI app you already have in four steps. You need Python 3.10 or newer. You don't
need an API key, a server or an account.

The steps below are for a Python app. For TypeScript or JavaScript (Jest or Vitest), see
[sdk/js](../sdk/js/README.md): `npm install --save-dev assay-evals`, `assayCase()` in a test, and
`assay test`. Everything from step 4 on (baselines, `assay diff`, CI, the dashboard) is the same.

Assay remembers what each test did the last time it passed. When a prompt, model or code change
makes your app behave differently, the test fails and tells you what changed.

## 1. Install

```bash
pip install assay-server pytest
```

## 2. Add two lines to your app

In the file where your AI calls its tools:

```python
import assay_sdk as assay

assay.instrument()          # line 1: records your OpenAI, Anthropic, Gemini, Ollama or LiteLLM calls


@assay.tool                 # line 2: put this on each tool the AI can call
def get_order(order_id):
    ...
```

That's the only change to your app. It behaves exactly as before. Outside a test, nothing is
recorded.

## 3. Write a test

Make a file `tests/test_ai.py`. It's a normal pytest test that takes one extra argument,
`assay_case`:

```python
from assay_sdk.testing import assert_called
from myapp.agent import answer_customer      # your app's function


def test_order_status(assay_case):
    reply = answer_customer("Where is my order O-17?")

    assert_called(assay_case, "get_order", order_id="O-17")   # it used the right tool
    assert "Friday" in reply                                    # and gave the right answer
```

If the test can't import your app, add this to `pyproject.toml`:

```toml
[tool.pytest.ini_options]
pythonpath = ["."]
```

## 4. Run it

```bash
pytest --assay tests
```

The first time it passes, Assay saves the result as this test's **baseline**.

After any later change, run the same command. If your app now does something different, the test
fails and says what:

```
E   AssertionError: Expected a call to get_order(order_id='O-17'); get_order was called with
    get_order(order_id='O-71').
```

That's it. You're using Assay.

## Three commands to remember

| Command | What it does |
|---|---|
| `pytest --assay tests` | Runs your tests and compares each one with its last passing run. |
| `assay diff` | Shows what changed, test by test. |
| `assay accept` | The change was on purpose: save it as the new baseline. |

## Let Claude Code run it for you (optional)

If you use Claude Code, add a skill that has Claude run these tests whenever it changes a prompt,
the model, a tool or the agent's code:

```bash
assay init --claude-code
```

This writes `.claude/skills/assay/SKILL.md`. Commit it, so everyone on your team gets it. After
an edit, Claude runs `pytest --assay`, reads `assay diff`, and tells you what regressed and why,
with a proposed fix. It never accepts a new baseline or loosens a test on its own; that stays
your call.

## All your repositories in one dashboard (optional)

If you work across several repositories, set this up once and every repository's runs land in
one dashboard, each under its own name:

```bash
pipx install assay-server                       # the `assay` command, in its own environment
assay init --claude-code --global               # the Claude Code skill, for every project
mkdir -p ~/.assay-server
ASSAY_STORE_URL=sqlite:///$HOME/.assay-server/assay.db assay serve   # the dashboard, at http://localhost:8400
```

`assay serve` is one Python process with a SQLite file, so it needs no Docker and little memory.
Give it a fixed `ASSAY_STORE_URL`: by default it keeps its data in `./assay.db`, wherever it was
started. Each project still needs the pytest plugin in its own environment
(`pip install assay-evals pytest`), since `pytest --assay` runs there.

To keep the server running across restarts, start it as a user service.

On macOS, save this as `~/Library/LaunchAgents/dev.assay.server.plist` (with your home folder
in place of `/Users/you`, and the path `which assay` prints), then run
`launchctl load ~/Library/LaunchAgents/dev.assay.server.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>dev.assay.server</string>
  <key>ProgramArguments</key><array>
    <string>/Users/you/.local/bin/assay</string><string>serve</string><string>--port</string><string>8400</string>
  </array>
  <key>EnvironmentVariables</key><dict>
    <key>ASSAY_STORE_URL</key><string>sqlite:////Users/you/.assay-server/assay.db</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardErrorPath</key><string>/Users/you/.assay-server/server.log</string>
</dict></plist>
```

On Linux, save this as `~/.config/systemd/user/assay.service`, then run
`systemctl --user enable --now assay`:

```ini
[Unit]
Description=Assay dashboard

[Service]
Environment=ASSAY_STORE_URL=sqlite:///%h/.assay-server/assay.db
ExecStart=%h/.local/bin/assay serve --port 8400
Restart=on-failure

[Install]
WantedBy=default.target
```

Or with Docker, if you already run it:
`docker run -d --restart unless-stopped -p 127.0.0.1:8400:8400 -v assay-data:/data --name assay ghcr.io/tap222/assay-server`.

Then send every run there. Set these in your shell profile, or for Claude Code in
`~/.claude/settings.json` under `"env"`:

```bash
export ASSAY_URL=http://localhost:8400
export ASSAY_UPLOAD=1
```

Every `pytest --assay` run, yours or Claude's, then goes to the dashboard, filed under the
repository's name from its git remote (`ASSAY_PROJECT` overrides it). **All projects** shows
each repository's latest run against the one before it, with what regressed first. A repository
needs Assay tests to show up; in one without them, Claude offers to set them up.

Each run also records where it came from: the repository's git remote (with any credentials
removed) and its folder, with your home folder as `~`. The dashboard shows them under **Latest
test run** and in **All projects**, so a project name can always be traced back to its
repository.

Only `pytest --assay` sends runs. A plain `pytest` run, even with `ASSAY_URL` set, records
locally to `.assay/events.jsonl` and sends nothing, so day-to-day test runs don't fill the
dashboard.

If the server isn't running, the tests still run and their result stands; the run is kept in
`.assay/` and the output says so. Send it once the server is up with `assay upload`.

Each run's page lists every case with the checks the server works out from the trajectory
(answer, end state, tool calls, safety, efficiency) and **Recorded checks**: the ones sent with
the run, such as the test's own `expect(...)` checks and judges. A case that failed one of those
shows as failing, with the check's name and reason.

## Run it on every pull request (optional)

Copy this file into your repository as `.github/workflows/ai-tests.yml`. Each pull request then
gets a comment with what changed, and fails if a test got worse.

```yaml
on: { push: { branches: [main] }, pull_request: {} }
permissions: { contents: read, pull-requests: write }
jobs:
  ai-tests:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: "3.12" }
      - run: pip install -r requirements.txt      # your app's dependencies
      - uses: tap222/assay-evals@v1
        with:
          command: pytest --assay tests
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}   # if your app calls a real model
```

Also add `.assay/` to your `.gitignore`. Assay keeps baselines there, and CI keeps its own.

## See your results in the dashboard (optional)

Everything above works on your machine alone. To see runs in the Assay dashboard too, get two
things from whoever runs your Assay server: its address and an API key. Then:

```bash
export ASSAY_URL=https://your-assay-server.example.com
export ASSAY_KEY=ak_...
pytest --assay --assay-upload tests
```

The output ends with a link to the run in the dashboard. In CI, add `ASSAY_URL` and `ASSAY_KEY`
as repository secrets, and add `--assay-upload` to the command.

Running the server yourself: [Setup](setup.md#deploy).

## If something goes wrong

| Problem | Fix |
|---|---|
| `assay: command not found` | Activate your virtual environment and run the install again. |
| `No module named 'myapp'` | Add `pythonpath = ["."]` to `pyproject.toml` (step 3). |
| The first run fails | A failing run never becomes the baseline. Fix the test, or run `assay accept` to start from where you are. |
| You want to start over | Delete the `.assay/` folder. |

## Want more?

- [One-minute demo](../examples/prompt-regression): a one-line prompt change caught.
- [Safety rules](testing.md): rules every run must follow, such as "never call `delete_order`"
  (`assay init` makes a starter `assay.toml`).
- [All assertions and flaky tests](testing.md), [agents](agents.md),
  [document extraction](documents.md), [CI options](ci.md).
