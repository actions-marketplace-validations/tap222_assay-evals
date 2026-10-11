[Assay](../README.md) › [Documentation](README.md) › [Roadmap](roadmap.md)

# User stories

Who Assay is for, what each person needs, and how we'll know it's done. Each story belongs to a
phase of the [roadmap](roadmap.md) and moves one number a team cares about: quality,
reliability, cost or delivery time.

| Person | Who they are |
|---|---|
| Developer | Builds features, often with a coding agent; wants to merge safely without reading every line |
| Reviewer | Reviews pull requests from teammates and agents; wants to know where to look |
| Lead | Owns a team's delivery; wants quality, cost and speed trends, with evidence |
| Company | Platform or engineering leadership; wants it across teams, with controls |
| Coding agent | Claude Code or another agent; reads the report and fixes items before a person reviews |

| ID | Person | Story | Value | Phase |
|---|---|---|---|---|
| [US-01](#us-01) | Developer | One verdict and at most 3 items per PR | Delivery time | P2 |
| [US-02](#us-02) | Developer | Know when a test was loosened | Quality | P1 |
| [US-03](#us-03) | Developer | See what the code does differently, even when tests pass | Quality | P2 |
| [US-04](#us-04) | Developer | The likely cause of each change | Delivery time | P2 |
| [US-05](#us-05) | Developer | Changed code no test ran, riskiest first | Quality | P2 |
| [US-06](#us-06) | Developer | Flaky or real | Delivery time | P2 |
| [US-07](#us-07) | Developer | Mark a change intended once | Delivery time | P2 |
| [US-08](#us-08) | Developer | Rules the app already follows, confirmed once | Reliability | P3 |
| [US-09](#us-09) | Developer | Told when a PR breaks a rule | Quality | P3 |
| [US-10](#us-10) | Developer | The coding agent's claims checked | Quality | P3 |
| [US-11](#us-11) | Coding agent | A report it can act on | Delivery time | P1, P2 |
| [US-12](#us-12) | Developer | Know when an endpoint does more work | Cost | P4 |
| [US-13](#us-13) | Lead | What each change costs to build and run | Cost | P4 |
| [US-14](#us-14) | Reviewer | The report on the PR itself | Delivery time | P2 |
| [US-15](#us-15) | Lead | One monthly page with the four numbers | All four | P5 |
| [US-16](#us-16) | Company | The same view across teams and repositories | All four | P6 |
| [US-17](#us-17) | Company | Self-hosted, with SSO, roles and an audit log | Trust | P6 |
| [US-18](#us-18) | Developer | A production bug becomes a test | Reliability | P4 |

### US-01

As a developer, I want one verdict and at most 3 items for every pull request, so I can decide
to merge in under a minute.

- The verdict is "Safe to merge" or "Check N things".
- At most 3 items; everything else grouped into causes and collapsed.
- Each item has its evidence and a next action.

### US-02

As a developer, I want to know when a test was loosened in the same pull request as the code it
checks, so a weakened test can't hide a bug.

- Catches removed or loosened assertions, rewritten expected values, new skips and updated
  snapshots, in pytest, Jest and Vitest.
- Shows the lines before and after.
- Fails the run until a maintainer accepts it with the `assay-policy-change` label, like the
  existing checks that can't be loosened.

### US-03

As a developer, I want to see what the code now does differently, even when every test passes,
so I catch behavior changes my tests don't check.

- Responses, outside calls, database writes and AI calls, before and after.
- Values that change on every run (times, IDs) are left out.
- One change seen in many tests is one item.

### US-04

As a developer, I want the likely cause of each change, so I fix the right thing.

- Names the file, commit, package or config next to the change.
- Says "nothing on your side changed" when an outside service changed.

### US-05

As a developer, I want the changed code that no test ran, riskiest first, so I know where bugs
can hide.

- Per function, not per line.
- Money, messages, logins and database writes ranked first.
- At most one such item in the top 3.

### US-06

As a developer, I want to know whether a failure is flaky or real, so I don't rerun CI hoping it
goes green.

- A failing test is repeated before it's judged ([repeated attempts](repeats.md)).
- Flaky tests are labelled and kept out of the top 3.

### US-07

As a developer, I want to mark a change as intended once, so it isn't reported again.

- One click or one command.
- It becomes the new baseline once merged.

### US-08

As a developer, I want Assay to propose rules my app already follows, and to confirm each once,
so I stop repeating the same correction to my coding agent.

- Proposed only with strong evidence: seen 3 or more times and never broken.
- At most one suggestion per report.
- Confirming writes it to `CLAUDE.md` (or `AGENTS.md`) and adds a check.

### US-09

As a developer, I want to be told when a pull request breaks a confirmed rule, so a known
mistake never ships again.

- Names the file and the rule, with the flow before and after.
- "Not a rule here" records an exception.

### US-10

As a developer, I want what the coding agent says it did checked against what actually ran, so
"all tests pass" is never taken on trust.

- Reads the coding session for the branch.
- Flags tests that didn't run, or failed, after a success claim.

### US-11

As a coding agent, I want the report in a form I can act on after every change, so items are
fixed before a person reviews.

- JSON output, and an agent skill that runs it (`assay init --claude-code` adds one today).
- Confirmed rules are in the agent's instructions file.

### US-12

As a developer, I want to know when a change makes an endpoint do more work, so slowdowns and
bills don't grow silently.

- Queries and outside calls per test, before and after.
- Growth over 2 times is flagged, with its cause.

### US-13

As a lead, I want what each change cost to build and what it adds to run, so I can see the
cost of features and of AI use.

- Build cost from the coding sessions on the branch.
- Run cost from AI calls per request, before and after.
- Matches the provider's bill within 5%.

### US-14

As a reviewer, I want the same report on the pull request itself, so I know where to look
before I read the diff.

- One comment, updated on every push.
- Links to the evidence.

### US-15

As a lead, I want one monthly page with quality, reliability, cost and delivery time, so I can
show improvement with evidence.

- Measured counts and times only, no estimates.
- Each number shows its trend and links to its evidence.
- Exports to PDF and CSV.

### US-16

As a company, I want the same view across all teams and repositories, so I can compare them and
spot risk.

- Roll-up by team and repository, with a drill-down to any pull request.

### US-17

As a company, I want Assay self-hosted with SSO, roles and an audit log, so code and data stay
inside.

- Builds on the existing SSO, audit log and redaction.
- A retention policy per project.

### US-18

As a developer, I want a bug found in production to become a test, so the same bug can't come
back.

- Drafted from the production report or error.
- Kept as a case after one confirmation.
