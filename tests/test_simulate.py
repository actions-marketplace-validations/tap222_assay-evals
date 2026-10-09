"""Simulated users (assay_sdk/simulate.py): a whole conversation as one run, the goal decided by your
own check before the simulated user's opinion, and contracts that hold across every turn."""
import json
import sys
from pathlib import Path
from types import SimpleNamespace as N

import pytest

import assay_sdk as assay
from assay_sdk import Judge, Persona, simulate
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")


class Plays:
    """A model playing the user: each call returns the next scripted move."""

    def __init__(self, *moves):
        self.moves, self.messages, self.calls = list(moves), self, []

    def create(self, **kw):
        self.calls.append(kw)
        m = self.moves.pop(0)
        text = m if isinstance(m, str) else json.dumps({"reason": "", "goal_met": False, "done": False, **m})
        return N(model="claude-opus-5", stop_reason="end_turn", content=[N(type="text", text=text)])


def user(*moves):
    return Judge("anthropic", "claude-opus-5", client=Plays(*moves))


def refund_agent(message, history):
    run = assay.current()
    if "O-" not in message:
        return "Sorry to hear that. What's your order number?"
    run.tool("refund", {"order_id": "O-17"}, {"ok": True})
    run.state("order:17", "update", {"status": "refunded"})
    return "Refunded O-17: the money is on its way."


UPSET = Persona(goal="get a refund for broken order O-17", traits="impatient", facts={"order_id": "O-17"})


def test_a_conversation_to_the_goal_decided_by_your_own_check(tmp_path, monkeypatch):
    monkeypatch.delenv("ASSAY_URL", raising=False)
    assay.init(path=str(tmp_path / "e.jsonl"))
    u = user({"message": "My order arrived broken and I want my money back."},
             {"message": "It's O-17."},
             {"message": "", "done": True, "goal_met": True, "reason": "refunded"})
    with assay.run("support", test={"run": "r", "case": "refund"}) as run:
        sim = simulate(refund_agent, UPSET, user=u, run=run,
                       success=lambda r: r.state_of("order:17").get("status") == "refunded")
    assay.shutdown()
    assert (sim.status, sim.goal_met, sim.turns, sim.decided_by) == ("PASS", True, 2, "success")
    assert [m["role"] for m in sim.transcript] == ["user", "assistant", "user", "assistant"]
    events = [json.loads(x) for x in (tmp_path / "e.jsonl").read_text(encoding="utf-8").splitlines()]
    kinds = [e["kind"] for e in events if e.get("type") == "step"]
    assert kinds == ["user", "answer", "user", "tool", "state", "answer"]  # the tools where they happened
    checks = {e["field"]: e["status"] for e in events if e.get("type") == "check"}
    assert checks == {"goal": "pass", "turns": "pass"}
    system = u._client.calls[0]["system"][0]["text"]
    assert "get a refund for broken order O-17" in system and "- order_id: O-17" in system
    assert u._client.calls[0]["temperature"] == 0  # the same agent gets the same conversation


def test_the_simulated_users_view_is_used_only_without_a_check(tmp_path):
    sim = simulate(lambda m: "I can't help with refunds.", UPSET, max_turns=2,
                   user=user({"message": "Refund O-17 please."},
                             {"message": "", "done": True, "goal_met": False, "reason": "it refused"}))
    assert (sim.status, sim.decided_by) == ("FAIL", "user") and "the simulated user says: it refused" in sim.reason


def test_out_of_turns_and_a_simulator_that_breaks(tmp_path):
    loop = user({"message": "hello?"}, {"message": "hello??"}, {"message": "", "done": True, "goal_met": False,
                                                                 "reason": "no progress"})
    sim = simulate(lambda m: "Hmm.", UPSET, user=loop, max_turns=2)
    assert sim.status == "FAIL" and "(no end after 2 turns)" in sim.reason
    broken = simulate(lambda m: "x", UPSET, user=user("not json", "still not"))
    assert (broken.status, broken.goal_met) == ("INVALID", None)  # the simulator's failure, not the agent's


def test_a_scripted_persona_needs_no_model():
    seen = []

    def agent(message, history):
        seen.append(len(history))
        return f"ok: {message}"
    sim = simulate(agent, Persona(script=["hi", "O-17"]))
    assert sim.turns == 2 and seen == [0, 2] and sim.decided_by == "script"
    with pytest.raises(ValueError, match="user="):
        simulate(agent, UPSET)


AGENT = '''
import json, os
from types import SimpleNamespace as N
import assay_sdk as assay
from assay_sdk import Judge, Persona, simulate

class Plays:
    def __init__(self, moves): self.moves, self.messages = list(moves), self
    def create(self, **kw):
        return N(model="m", stop_reason="end_turn", content=[N(type="text", text=json.dumps(self.moves.pop(0)))])

def agent(message):
    run = assay.current()
    if "O-" not in message:
        return "What's the order number?"
    run.tool("refund", {"order_id": "O-17"}, {"ok": True})  # on turn 2, and nobody approved it
    return "Refunded."

assay.init()
moves = [{"message": "Refund my broken order", "done": False, "goal_met": False, "reason": ""},
         {"message": "O-17", "done": False, "goal_met": False, "reason": ""},
         {"message": "", "done": True, "goal_met": True, "reason": "refunded"}]
with assay.run("support", test="refund_upset") as run:
    simulate(agent, Persona(goal="refund"), user=Judge("anthropic", "m", client=Plays(moves)), run=run)
'''


def test_contracts_hold_across_the_whole_conversation(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "agent.py").write_text(AGENT, encoding="utf-8")
    (tmp_path / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n\n[[contracts]]\n'
                                         'kind = "requires_approval"\nstep = "refund"\n', encoding="utf-8")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "refund_upset" in out and "Safety" in out and "without an approval" in out
