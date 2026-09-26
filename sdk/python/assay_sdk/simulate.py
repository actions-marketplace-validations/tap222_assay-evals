"""Simulated users: test an agent over a whole conversation, not one message.

    from assay_sdk import Judge, Persona, simulate

    upset = Persona(goal="get a refund for order O-17, which arrived broken",
                    traits="impatient; gives the order number only when asked; pushes back once",
                    facts={"order_id": "O-17", "email": "ana@example.com"})

    def test_refund_when_upset(assay_case):
        sim = simulate(my_agent, upset, user=Judge("anthropic", "claude-opus-5"), run=assay_case,
                       success=lambda run: run.state_of("order:17").get("status") == "refunded")
        assert sim.goal_met, sim.reason

An LLM plays the user: it has a goal, a manner and facts it gives only when asked, and each turn
it writes what the user would type next, or ends the conversation. The agent answers each
message. The conversation is one run: every user message is a step, every reply the agent's
answer, and every tool call in between is recorded where it happened. So contracts and
expectations hold over the whole conversation ("no refund without an approval", in any turn).

Whether the goal was met is decided, in order, by:
  success   your deterministic check, given the run: the order's recorded state, a tool call
  the user  the simulated user's own view, when there's no success check: an opinion, and
            recorded as one
It's recorded as the check "goal"; the conversation's length as "turns", failed past max_turns.

A Persona with script= plays fixed messages instead, with no model: reproducible to the letter.
The simulated user runs at temperature 0 unless told otherwise, so an unchanged agent gets the
same conversation. An answer from the simulator that isn't the JSON asked for is asked for
again, then the result is an error (INVALID), never a verdict on the agent.

agent: agent(message) or agent(message, history), history as [{"role", "content"}]; sync or async.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

SYSTEM = """You play a user talking to an AI assistant, to test the assistant. Stay in character.

Your goal: {goal}
Who you are: {traits}
What you know (share a fact only when it's asked for, or when a real user would volunteer it):
{facts}

Each turn, write only what this user would type next. Don't help the assistant more than this user
would, and don't break character to explain the test. On the first turn, open the conversation.
End it (done: true) when your goal is met, when it clearly won't be, or when a real user would give
up. Answer as JSON: {{"message": "what you type (empty when done)", "done": true or false,
"goal_met": true or false, "reason": "one sentence: why you're done, or what you're after"}}."""

SCHEMA = {"type": "object", "additionalProperties": False, "required": ["message", "done", "goal_met", "reason"],
          "properties": {"message": {"type": "string"}, "done": {"type": "boolean"}, "goal_met": {"type": "boolean"},
                         "reason": {"type": "string"}}}


@dataclass
class Persona:
    goal: str = ""
    traits: str = "an ordinary user"
    facts: Dict[str, Any] = field(default_factory=dict)
    script: Optional[List[str]] = None  # fixed messages, in order: no model plays this user
    name: str = "user"


@dataclass
class Simulation:
    transcript: List[dict]  # [{"role": "user" | "assistant", "content"}]
    turns: int
    goal_met: Optional[bool]  # None: it couldn't be told
    reason: str
    decided_by: str  # success | user | script | error
    status: str  # PASS | FAIL | INVALID | ERROR


def _takes_history(fn: Callable) -> bool:
    try:
        ps = [p for p in inspect.signature(fn).parameters.values()
              if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        return False
    return len(ps) >= 2


def _call(agent: Callable, message: str, history: List[dict]) -> str:
    out = agent(message, history) if _takes_history(agent) else agent(message)
    if inspect.isawaitable(out):
        out = asyncio.run(out) if not _running() else _in_thread(out)
    return out if isinstance(out, str) else json.dumps(out, default=str)


def _running() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _in_thread(coro):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(1) as ex:
        return ex.submit(asyncio.run, coro).result()


def _next(user, persona: Persona, transcript: List[dict], params: dict, tries: int = 2) -> dict:
    """The simulated user's next move: {"message", "done", "goal_met", "reason"}, or {"error"}."""
    facts = "\n".join(f"- {k}: {v}" for k, v in persona.facts.items()) or "- nothing in particular"
    system = SYSTEM.format(goal=persona.goal or "(none given)", traits=persona.traits, facts=facts)
    convo = "\n\n".join(f"{'YOU' if m['role'] == 'user' else 'ASSISTANT'}: {m['content']}" for m in transcript) \
        or "(the conversation hasn't started: open it)"
    last = None
    for _ in range(tries):
        r = user.ask(f"<conversation>\n{convo}\n</conversation>\n\nYour next turn, as JSON.", system=system,
                     schema=SCHEMA, **params)
        if r.ok and isinstance(r.structured, dict) and isinstance(r.structured.get("done"), bool) and \
                isinstance(r.structured.get("message"), str):
            return r.structured
        last = r.error or "the simulated user's answer wasn't the JSON asked for"
        if r.error_kind in ("timeout", "rate_limited", "unavailable", "error"):
            break
    return {"error": last}


def simulate(agent: Callable, persona: Persona, *, user=None, run=None, max_turns: int = 8,
             success: Optional[Callable[[Any], bool]] = None, user_params: Optional[dict] = None,
             task: str = "conversation") -> Simulation:
    """Run a conversation between `agent` and a simulated `persona` (see the module docstring).
    user: an assay_sdk.Judge that plays the persona (not needed with persona.script). run: the
    test case's run (the pytest assay_case) to record into; otherwise a run of its own is opened."""
    import assay_sdk as assay
    if persona.script is None and user is None:
        raise ValueError("simulate needs user= (an assay_sdk.Judge to play the persona), or a Persona with script=.")
    params = {"temperature": 0, **(user_params or {})}
    own = run is None
    ctx = assay.run(task, input=None) if own else None
    r = ctx.__enter__() if own else run
    transcript: List[dict] = []
    moves: List[dict] = []
    error = None
    try:
        for turn in range(max_turns):
            if persona.script is not None:
                if turn >= len(persona.script):
                    moves.append({"done": True, "goal_met": None, "reason": "the script ended"})
                    break
                move = {"message": persona.script[turn], "done": False}
            else:
                move = _next(user, persona, transcript, params)
                if "error" in move:
                    error = move["error"]
                    break
            moves.append(move)
            if move.get("done") or not (move.get("message") or "").strip():
                break
            message = move["message"]
            r.user(message)
            if turn == 0 and hasattr(r, "request") and r.request is None:
                r.request = message
            history = list(transcript)
            transcript.append({"role": "user", "content": message})
            reply = _call(agent, message, history)
            r.answer(reply)
            transcript.append({"role": "assistant", "content": reply})
        else:
            if persona.script is None:  # out of turns: one last look from the user's side
                final = _next(user, persona, transcript, params)
                if "error" not in final:
                    moves.append({**final, "done": True})
    finally:
        if own:
            ctx.__exit__(None, None, None)
    turns = sum(1 for m in transcript if m["role"] == "user")
    return _decide(r, transcript, moves, turns, max_turns, success, error, persona)


def _decide(r, transcript, moves, turns, max_turns, success, error, persona) -> Simulation:
    last = moves[-1] if moves else {}
    if error:
        sim = Simulation(transcript, turns, None, f"the simulated user couldn't go on: {error}", "error", "INVALID")
        _record(r, sim)
        return sim
    ran_out = turns >= max_turns and not (last.get("done") and last.get("goal_met"))
    if success is not None:
        try:
            met = bool(success(r))
            why = "your success check passed" if met else "your success check failed"
        except Exception as exc:  # a broken check isn't the agent's failure
            sim = Simulation(transcript, turns, None, f"the success check raised {type(exc).__name__}: {exc}",
                             "error", "ERROR")
            _record(r, sim)
            return sim
        by = "success"
    elif persona.script is not None:
        sim = Simulation(transcript, turns, None, "a scripted conversation: give success= to judge the goal",
                         "script", "PASS")
        _record(r, sim, goal=False)
        return sim
    else:
        met = bool(last.get("goal_met"))
        why = f"the simulated user says: {last.get('reason') or ('goal met' if met else 'goal not met')}"
        by = "user"
    if ran_out and not met:
        why += f" (no end after {max_turns} turns)"
    sim = Simulation(transcript, turns, met, why, by, "PASS" if met else "FAIL")
    _record(r, sim)
    return sim


def _record(r, sim: Simulation, goal: bool = True) -> None:
    if not hasattr(r, "check") or getattr(r, "test", None) is None:
        return
    try:
        if goal:
            status = {"PASS": "pass", "FAIL": "fail"}.get(sim.status, "error")
            r.check("goal", status, reason=sim.reason, evaluator=f"assay.simulate@1:{sim.decided_by}",
                    error_kind="invalid" if sim.status == "INVALID" else "error" if sim.status == "ERROR" else None,
                    actual=f"{sim.turns} turns")
        r.check("turns", "pass", actual=str(sim.turns))
    except ValueError:  # a run that isn't a test case: nothing to record against
        pass
