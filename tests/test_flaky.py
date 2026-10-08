from datetime import datetime, timedelta

from assay import flaky
from assay.flaky import assess, fisher_lower, interval, summarize

T = datetime(2026, 9, 1)


class R:
    def __init__(self, case, status, actual="x", attempt=0, field="f", evaluator="e", votes=None):
        self.case_id, self.field, self.evaluator = case, field, evaluator
        self.status, self.actual, self.attempt, self.ts = status, actual, attempt, T + timedelta(seconds=attempt)
        self.judgements_passed, self.judgements = votes or (None, None)


def runs(case, outcomes, actuals=None, **kw):
    return [R(case, {"P": "pass", "F": "fail", "E": "error"}[o], (actuals or "x" * len(outcomes))[i], i, **kw)
            for i, o in enumerate(outcomes)]


def one(cand, base):
    return assess(flaky.attempts_by_check(cand), flaky.attempts_by_check(base))[("c", "f", "e")]


def test_exact_statistics():
    lo, hi = interval(4, 5)
    assert round(lo, 3) == 0.284 and round(hi, 3) == 0.995
    assert round(fisher_lower(5, 0, 4, 1), 3) == 0.5
    assert round(fisher_lower(50, 0, 4, 1), 3) == 0.091
    assert [round(q, 3) for q in flaky.bh([0.01, 0.04, 0.03, 0.5])] == [0.04, 0.053, 0.053, 0.5]


def test_pass_pass_fail_pass_pass_is_not_a_regression_on_its_own():
    # Always passed before, one failure in five now: that happens by chance, even with nothing changed.
    x = one(runs("c", "PPFPP"), runs("c", "PPPPP"))
    assert x["state"] == "flaky" and x["outcomes"] == "●●○●●"
    # Two failures in five: plausibly worse, too few attempts to tell. Rerun.
    x = one(runs("c", "PFFPP"), runs("c", "PPPPP"))
    assert x["state"] == "needs_reruns" and x["reruns"] > 0
    # Already about that flaky before: flaky, no worse.
    x = one(runs("c", "PPFPP", "xxyxx"), runs("c", "PPFPPPPFPP", "xxyxxxxyxx"))
    assert x["state"] == "flaky" and x["flake"] == "output" and x["since"] == "flaky"
    # Rerun enough and a real drop shows: 50/50 before, 12/20 now.
    x = one(runs("c", "PPFPP" * 2 + "PFPFP" * 2), runs("c", "P" * 50))
    assert x["state"] == "got_worse"


def test_what_varies_in_a_flaky_check():
    assert one(runs("c", "PFP", "xyx"), [])["flake"] == "output"      # the model's output changes
    assert one(runs("c", "PFP", "xxx"), [])["flake"] == "evaluator"    # same output, different verdicts
    assert one(runs("c", "PEP"), [])["flake"] == "infrastructure"      # an attempt errored


def test_many_checks_dont_produce_false_alarms():
    # 2,000 checks, each 5/5 before and one failure in five now purely by chance: nothing "got worse".
    cand = [r for i in range(2000) for r in runs(f"c{i}", "PPPPF" if i % 10 == 0 else "PPPPP")]
    base = [r for i in range(2000) for r in runs(f"c{i}", "PPPPP")]
    states = assess(flaky.attempts_by_check(cand), flaky.attempts_by_check(base))
    s = summarize(states, tolerance=0.02)
    assert s["states"]["got_worse"] == 0 and s["states"]["needs_reruns"] == 0 and s["outcome"] == "advance"


def test_the_correction_counts_every_check_not_just_the_ones_that_dropped():
    # One check 8/8 → 5/8 is suspicious alone, and ordinary among 50 checks that could have moved.
    alone = assess(flaky.attempts_by_check(runs("c0", "PPPPPFFF")), flaky.attempts_by_check(runs("c0", "P" * 8)))
    assert alone[("c0", "f", "e")]["state"] == "needs_reruns"
    cand = runs("c0", "PPPPPFFF") + [r for i in range(1, 50) for r in runs(f"c{i}", "P" * 8)]
    base = [r for i in range(50) for r in runs(f"c{i}", "P" * 8)]
    x = assess(flaky.attempts_by_check(cand), flaky.attempts_by_check(base))[("c0", "f", "e")]
    assert x["q_worse"] > flaky.PLAUSIBLE and x["state"] == "flaky"


def test_a_capability_that_collapsed_is_never_waved_through():
    # 8/8 → 0/8 on one task of 50: beyond chance even corrected for 50.
    cand = runs("c0", "F" * 8) + [r for i in range(1, 50) for r in runs(f"c{i}", "P" * 8)]
    base = [r for i in range(50) for r in runs(f"c{i}", "P" * 8)]
    s = summarize(assess(flaky.attempts_by_check(cand), flaky.attempts_by_check(base)))
    assert s["outcome"] == "hold" and [w["case_id"] for w in s["got_worse"]] == ["c0"]
    # 2/2 → 0/2 is too few to call, but a whole check gone is rerun, not called chance.
    cand = runs("c0", "FF") + [r for i in range(1, 50) for r in runs(f"c{i}", "PP")]
    base = [r for i in range(50) for r in runs(f"c{i}", "PP")]
    s = summarize(assess(flaky.attempts_by_check(cand), flaky.attempts_by_check(base)))
    assert s["outcome"] == "rerun" and [r["case_id"] for r in s["reruns"]] == ["c0"]


def test_a_small_suite_isnt_rolled_back_on_a_narrow_interval():
    # Five checks, each wobbling by an attempt or two: a paired t interval spans zero.
    base = [r for i, o in enumerate(["PPPPPPPP", "PPPPPPPF", "PPPPPPFF", "PPPPPPPP", "PPPPPFFF"]) for r in runs(f"c{i}", o)]
    cand = [r for i, o in enumerate(["PPPPPPPF", "PPPPPPFF", "PPPPPPPF", "PPPPPPPF", "PPPPFFFF"]) for r in runs(f"c{i}", o)]
    s = summarize(assess(flaky.attempts_by_check(cand), flaky.attempts_by_check(base)))
    assert s["change"]["low"] < 0 < s["change"]["high"] and s["outcome"] == "advance"
    assert any("could be lower by up to" in r for r in s["reasons"])


def test_run_decisions():
    base = [r for i in range(200) for r in runs(f"c{i}", "PPP")]
    only_flaky = [r for i in range(200) for r in runs(f"c{i}", "PFP" if i < 3 else "PPP", "xyx" if i < 3 else "xxx")]
    s = summarize(assess(flaky.attempts_by_check(only_flaky), flaky.attempts_by_check(base)))
    assert s["outcome"] == "advance"  # 3/3 → 2/3 on three checks of 200: chance, not a reason to rerun

    broken = [r for i in range(200) for r in runs(f"c{i}", "FFF" if i < 40 else "PPP")]
    states = assess(flaky.attempts_by_check(broken), flaky.attempts_by_check(base))
    assert summarize(states)["outcome"] == "rollback"
    # The same failures, all on infrastructure: rerun them, not a rollback.
    roles = {k: "infrastructure" for k, x in states.items() if x["passed"] == 0}
    s = summarize(states, roles=roles)
    assert s["outcome"] == "rerun" and all(r["why"] == "failed on infrastructure" for r in s["reruns"])
    # Accepted as an intended change: advance.
    assert summarize(states, roles={k: "accepted" for k in roles})["outcome"] == "advance"
    assert summarize(states, roles={k: "intended" for k in roles})["outcome"] == "hold"


def judged(case, outcomes, split, n=3):
    """Attempts whose answer was judged n times: the first `split` of them got a split vote."""
    out = runs(case, outcomes)
    for i, r in enumerate(out):
        p = n if r.status == "pass" else 0
        r.judgements, r.judgements_passed = n, (n - 1 if r.status == "pass" else 1) if i < split else p
    return out


def test_a_judge_that_disagrees_with_itself_is_judge_unstable_not_flaky():
    # 8/8 → 7/8, and the same answer judged again got another verdict: the judge varies, not only the AI.
    x = one(judged("c", "PPPPPPPF", split=2), runs("c", "P" * 8))
    assert x["state"] == "judge_unstable" and x["flake"] == "evaluator" and not x["worse"]
    assert (x["judge_split"], x["rejudged"]) == (2, 8) and x["since"] == "flaky"
    # 8/8 → 5/8 could be worse: still not settled, but more attempts of the AI won't settle it.
    x = one(judged("c", "PPPPPFFF", split=3), runs("c", "P" * 8))
    assert x["state"] == "judge_unstable" and x["worse"] and x["reruns"] is None
    s = summarize({("c", "f", "e"): x, ("d", "f", "e"): one(runs("c", "P" * 8), runs("c", "P" * 8))})
    assert s["outcome"] == "rerun" and s["states"]["judge_unstable"] == 1 and s["reruns"] == []
    assert "their judge gave the same answer different verdicts" in s["reasons"][0]
    assert s["judge_unstable"][0]["case_id"] == "c" and s["judge_unstable"][0]["worse"]
    # Asked again, the judge agreed every time: the AI varies, and it's flaky or needs reruns as before.
    assert one(judged("c", "PPPPPPPF", split=0), runs("c", "P" * 8))["state"] == "flaky"
    assert one(judged("c", "PPPPPFFF", split=0), runs("c", "P" * 8))["state"] == "needs_reruns"
    # A collapse is a regression whatever the judge did: 8/8 → 0/8 isn't the judge.
    assert one(judged("c", "F" * 8, split=2), runs("c", "P" * 8))["state"] == "got_worse"


def test_a_flaky_judge_that_isnt_worse_doesnt_hold_the_run():
    states = assess(flaky.attempts_by_check(judged("c", "PPPPPPPF", split=2)), flaky.attempts_by_check(runs("c", "P" * 8)))
    s = summarize(states)
    assert s["outcome"] == "advance" and any("vary with the judge" in r for r in s["reasons"])
