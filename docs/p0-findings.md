[Assay](../README.md) › [Documentation](README.md) › [Roadmap](roadmap.md)

# P0 findings: replaying Assay's own history

P0 asks whether a per-PR report would be short and right before anything is built. We
replayed 22 past changes to this repository offline and checked what a report would have
flagged against the bugs that were later fixed by hand.

**Result: the targets were not met.** Reports were short, but the planned signal, changed code
that no test ran, would have flagged 1 of 9 later-fixed bugs. Changing how that signal works
and adding a second one gets to 6 of 9. P2 has been changed to match.

## How the replay worked

For each change, the base (the merge base for a pull request, the parent for a direct commit)
and the head were checked out in their own worktree. The whole Python suite ran on the head
under coverage, subprocesses included, with imports pointed at that worktree. Each changed
line was then marked as run or not run by a test, and grouped by the function it sits in.

- **8 changes introduced a bug** that was later fixed by hand. They were found by blaming the
  lines each fix changed.
- **14 are ordinary pull requests**, used to measure report size and noise.
- Test failures were not counted: in the replay they came from today's environment (dates,
  installed packages), not the old code.
- The JavaScript SDK changes (#53, #56, #57, #58) were replayed, but only Python coverage was
  measured.
- Corrections repeated to the coding agent were not checked: the coding sessions for this
  history weren't kept.

## The bugs

| Bug | Introduced by | Fixed in | Kind | Changed lines run by a test? |
|---|---|---|---|---|
| `--upload` never uploaded | e6ded5e | #37 | Python | No: the line that sends had never run |
| Plain pytest sent results to the server | 5067e12 | #51 | Python, environment | Yes, but every test unset `ASSAY_URL` |
| Field accuracy counted checks that aren't fields | 5710ed4 | #11 | Python, logic | Yes |
| `assay demo` crashed (`UnboundLocalError`) | a1e9530 | aa8a663 | Python | No: no test ran the new lines |
| Release workflow failed | a1e9530 | #43 | Workflow | Not code |
| Install pin broke installs | 4a4d5a0 | 8989ccb | Packaging | Not code |
| Vercel returned 500 | #27 | #28 | Deploy config | Not code |
| Vercel build failed | #28 | #29 | Deploy config | Not code |
| Dashboard took 12.9 s to load | #47 | #48 | Performance | Yes |

Most of these came from direct commits to main, from before the repository used pull
requests. #45 (something left out, not something broken), #60 (Windows only) and #31 (a test
that failed after a fixed date) were fixed by hand too, but left out: no report on the change
could have caught them.

## What each signal would have caught

| Signal | Bugs flagged | Reports with 3 items or fewer | Noise |
|---|---|---|---|
| A changed function no test ran at all | 1 of 9 | 22 of 22 (21 empty) | none |
| A changed line no test ran | 2 of 9 | 15 of 22 | not measured |
| A deploy or dependency file changed | 4 of 9 | adds 1 item to 11 of 22 | 7 of 11 |
| The last two together | 6 of 9 | 14 of 22 | |

- **Function level is too coarse.** The upload bug sat in a function that tests ran, just not
  on the line that sent the run. Only the demo crash had a whole function no test reached.
- **Line level catches one more, at a cost.** Most reports stay short, but four changes had 11
  to 25 untested lines (5710ed4 had 25, #65 had 15, a1e9530 had 14, #67 had 11). They need to
  be grouped and ranked, not listed.
- **Four of the nine bugs were in files coverage can't see:** a requirements file, an ignore
  file, a workflow and a version pin. Flagging every such change catches all four, but most of
  those flags would have been noise (a new `action.yml` input, a version bump in
  `package.json`).
- **Two bugs ran clean under the tests.** The field-accuracy bug was wrong logic that every
  test exercised without checking the number. The upload bug only happened with `ASSAY_URL`
  set, and every test unset it.
- **The slow dashboard** needed a count of queries per request, which is P4.

## The weakened-test guard

The guard (`assay.weakening.scan`) found no test that was deleted, skipped or loosened. It
flagged two expected values rewritten in the same change as the code they check:

- **4a4d5a0:** a test's expected exit code changed from 0 to 3, with its messages rewritten.
  The behavior change was intended, but this is the item a reviewer should look at.
- **#32:** a new verdict, `JUDGE_UNSTABLE`, was added to an expected count. The assertion got
  stricter, not looser: this is noise.

## What changes on the roadmap

1. **P2 flags changed lines, not functions, that no test ran.** They're grouped by function
   and ranked by risk, with outside calls (HTTP) ranked with money, messages, logins and
   database writes. The upload bug was a send to a server.
2. **P2 adds deploy and dependency changes as their own item**, under a narrower rule: a
   requirements file, lock file, ignore file, workflow or version pin changed, and no CI job
   in the pull request built, installed or deployed from it. That rule still needs to be checked
   against these 11 changes.
3. **The guard's rewritten-value check stays,** but no longer lists an expected value that only
   gained entries, like #32 (done). Rerun on these two changes, it lists 4a4d5a0 and not #32.
4. **To try next:** flag changed code that reads a setting (an environment variable) that every
   test leaves unset. That would have caught the `ASSAY_URL` bug. Logic bugs like field accuracy
   need a behavior diff, which P2 already plans, and won't come from coverage.

## Limits

Nine bugs is a small sample, and the oldest come from a time without pull requests or CI.
P0 should be repeated on a larger app with a longer pull-request history, and with its
JavaScript and TypeScript code measured, before P2 starts.
