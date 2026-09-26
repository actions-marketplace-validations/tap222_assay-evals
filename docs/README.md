[Assay](../README.md)

# Documentation

| | |
|---|---|
| [Testing your AI app](testing.md) | Your AI tests are pytest tests: `pytest --assay`, the assay_case fixture, assertions, baselines, flakiness |
| [Behavior diff](diff.md) | `assay diff`: what changed between two versions, with the flow before and after and a severity |
| [CI and pull requests](ci.md) | The GitHub Action, the PR comment, rerunning what failed, timeouts |
| [Security](security.md) | What the checks catch in the agent, and what a pull request can and can't do to the evaluation |
| [Agents](agents.md) | Evaluating the trajectory: lifecycle, plan adherence, the LLM judge, conversations, MCP, behavior |
| [Results you can trust](verdicts.md) | One verdict per check, and whether the judge was given the right data |
| [Failure analysis](failures.md) | Where a wrong answer started, failure causes, path contracts |
| [Learning from production](learning.md) | Production failures become regression tests, with the whole trace |
| [Prompt versions](prompts.md) | The prompt registry, diffs, and results per version |
| [Release gates](release-gates.md) | Advance, hold or roll back, and flaky checks that rerun instead of block |
| [Measures, cost and alerting](measures.md) | What you get, the measures, cost per document, alerts |
| [Setup](setup.md) | Install, connect your pipeline or push events, go live, deploy |
| [API and authentication](api.md) | Keys, sending data, alert webhooks |
| [Event schema](event-schema.md) | The v1 events: runs, steps, checks, expectations |
| [Code layout](layout.md) | Where things are in this repository |
| [Not built yet](roadmap.md) | What's missing |

The Python SDK has its own guide: [sdk/python](../sdk/python/README.md).
