"""Synthetic data (assay/synth.py): dimensions and hand-written tuples first, tuples then queries, the fix-first
check, runs through the real app kept apart from production, personas, and the comparison with real traffic."""
import json
import re
from types import SimpleNamespace as N

import pytest
from fastapi.testclient import TestClient

import assay_sdk as assay
from assay import judge as judge_mod
from assay import review, synth
from assay.__main__ import main
from assay.api import create_app
from assay.config import Settings
from assay_sdk.llm import Response

H, SRC = {"X-Tenant": "s"}, {"source": "events:s"}

TOML = '''[test]
command = "true"

[synthetic]
app = "bot.py:answer"
about = "A customer support assistant for a software subscription"
prompts = ["prompt.txt"]

[[synthetic.dimensions]]
name = "Issue type"
values = ["billing", "technical", "general"]
hypothesis = "Promises refunds it can't give"

[[synthetic.dimensions]]
name = "Customer mood"
values = ["frustrated", "neutral"]

[[synthetic.dimensions]]
name = "Prior context"
values = ["new issue", "follow-up"]
'''

BOT = '''
def answer(q):
    return "Sorry to hear that. " + ("Your invoice is attached." if "invoice" in q else "Let me look into it.")
'''


class Asker:
    """Stands in for the generating model: answers each kind of prompt the way a model would."""

    def __init__(self):
        self.calls, self.turn = [], 0

    def ask(self, prompt, system=None, schema=None, check=True, **kw):
        self.calls.append((system or "")[:40])
        if system == synth.FILTER_RUBRIC:
            rows = re.findall(r"^(\d+): (.*)$", prompt.split("<combinations>")[1], re.M)
            v = {"verdicts": [{"id": int(i), "valid": not ("general" in t and "follow-up" in t),
                               "reason": "a general question isn't followed up" if "general" in t and "follow-up" in t
                               else ""} for i, t in rows]}
        elif system == synth.DIRECT_RUBRIC:
            v = {"tuples": [{"Issue type": "technical", "Customer mood": "neutral", "Prior context": "follow-up"},
                            {"Issue type": "nope", "Customer mood": "neutral", "Prior context": "follow-up"}]}
        elif system == synth.QUERY_RUBRIC:
            t = dict(re.findall(r"^- ([^:]+): (.*)$", prompt.split("<user_traits>")[1].split("</user_traits>")[0], re.M))
            v = {"query": "Please help with my account" if t["Issue type"] == "general" else
                 f"{t['Customer mood']} about {t['Issue type']}: my invoice is wrong ({t['Prior context']})"}
        elif system == synth.PERSONA_RUBRIC:
            v = {"goal": "get the invoice corrected", "traits": "short replies, annoyed", "facts": {"invoice": "INV-9"}}
        elif system and system.startswith("You play a user"):
            self.turn += 1
            v = {"message": "My invoice INV-9 is wrong" if self.turn % 2 else "", "done": not self.turn % 2,
                 "goal_met": True, "reason": "got an answer"}
        else:
            return Response(error="unexpected prompt", error_kind="error")
        return Response(text=json.dumps(v), structured=v, model="fake")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.delenv("ASSAY_URL", raising=False)
    (tmp_path / "assay.toml").write_text(TOML, encoding="utf-8")
    (tmp_path / "bot.py").write_text(BOT, encoding="utf-8")
    (tmp_path / "prompt.txt").write_text("You help with billing and technical questions, and general ones. "
                                         "Handle a new issue or a follow-up on an earlier one.", encoding="utf-8")
    asker = Asker()
    monkeypatch.setattr(synth, "_asker", lambda j: asker)
    sent = []
    assay.init(transport=lambda batch: sent.extend(batch), flush_interval=60)
    yield N(root=tmp_path, sent=sent, asker=asker)
    assay.init(enabled=False)


def rows(root, name):
    return synth.read(root / "synthetic" / name)


def test_hand_tuples_first_then_every_combination_filtered(project, capsys):
    assert main(["synth", "tuples"]) == 2 and "Write 20 tuples by hand first" in capsys.readouterr().err
    assert main(["synth", "tuple", "Issue=billing", "mood=frustrated", "Prior context=new issue"]) == 0
    assert "19 more before generating" in capsys.readouterr().out
    assert main(["synth", "tuple", "Issue=billing", "mood=frustrated", "Prior context=new issue"]) == 0
    assert "there already" in capsys.readouterr().out
    assert main(["synth", "tuple", "Issue=refunds", "mood=neutral", "Prior=new issue"]) == 2
    assert "isn't a value of Issue type" in capsys.readouterr().err
    assert main(["synth", "tuples", "--force"]) == 0
    out = capsys.readouterr().out
    assert "9 new tuples, 2 dropped" in out  # 12 combinations: one by hand, two no user would send
    t = rows(project.root, synth.TUPLES)
    gone = [r for r in t if r["status"] == "rejected"]
    assert len(gone) == 2 and all(r["values"]["Issue type"] == "general" and r["reason"] for r in gone)
    assert main(["synth", "tuples", "--force", "--direct", "-n", "5"]) == 0  # the invalid one is dropped
    assert "0 new tuples" in capsys.readouterr().out  # the valid one existed already
    assert main(["synth", "check"]) == 0
    out = capsys.readouterr().out
    assert "Customer mood: frustrated" in out.split("never mentions:")[1]  # fix the prompt before testing it
    assert "neutral" not in out.split("never mentions:")[1] and "billing" not in out.split("never mentions:")[1]


def test_queries_one_prompt_each_near_duplicates_dropped_then_run_through_the_app(project, capsys):
    main(["synth", "tuple", "Issue=general", "mood=frustrated", "Prior=new issue"])
    main(["synth", "tuple", "Issue=general", "mood=neutral", "Prior=new issue"])
    main(["synth", "tuple", "Issue=billing", "mood=frustrated", "Prior=follow-up"])
    capsys.readouterr()
    assert main(["synth", "queries"]) == 0
    assert "2 queries written, 1 dropped as near-duplicates" in capsys.readouterr().out
    assert project.asker.calls.count(synth.QUERY_RUBRIC[:40]) == 3  # a prompt of its own for each tuple
    assert main(["synth", "queries"]) == 0 and "0 queries" in capsys.readouterr().out  # nothing twice
    assert main(["synth", "run"]) == 0
    assert "Ran 2 queries" in capsys.readouterr().out
    starts = [e for e in project.sent if e["type"] == "run.start"]
    assert len(starts) == 2 and all(e["tags"]["origin"] == "synthetic" for e in starts)
    assert {e["tags"]["dim.Issue type"] for e in starts} == {"general", "billing"}


def test_an_app_that_opens_its_own_runs_is_tagged_not_wrapped(project, capsys):
    (project.root / "bot.py").write_text('import assay_sdk as assay\n\ndef answer(q):\n'
                                    '    with assay.run("support", input=q) as r:\n        r.answer("ok")\n    return "ok"\n', encoding="utf-8")
    main(["synth", "tuple", "Issue=billing", "mood=frustrated", "Prior=follow-up"])
    main(["synth", "queries"])
    assert main(["synth", "run"]) == 0
    starts = [e for e in project.sent if e["type"] == "run.start"]
    assert len(starts) == 1 and starts[0]["task"] == "support" and starts[0]["tags"]["origin"] == "synthetic"


def test_personas_for_multi_turn_runs(project, capsys):
    main(["synth", "tuple", "Issue=billing", "mood=frustrated", "Prior=follow-up"])
    assert main(["synth", "personas"]) == 0
    p = assay.load_personas(str(project.root / "synthetic" / synth.PERSONAS))
    assert len(p) == 1 and p[0].facts == {"invoice": "INV-9"}
    assert main(["synth", "run", "--personas"]) == 0
    steps = [e for e in project.sent if e["type"] == "step"]
    assert [s["kind"] for s in steps] == ["user", "answer"]
    start = next(e for e in project.sent if e["type"] == "run.start")
    assert start["tags"]["synthetic_kind"] == "persona" and start["conversation_id"].startswith("synth-")


class Placer:
    """The server's model: reads conversations for review, and places them on the dimensions."""

    def __init__(self):
        self.messages = self

    def create(self, **kw):
        system = kw["system"][0]["text"] if isinstance(kw["system"], list) else kw["system"]
        prompt = kw["messages"][-1]["content"]
        convo = prompt.split("<conversation>")[-1]
        if system == synth.CLASSIFY_RUBRIC:
            issue = "billing" if "invoice" in convo else "technical" if "crash" in convo else "none of these"
            v = {"values": {"Issue type": issue, "Customer mood": "frustrated" if "!" in convo else "neutral",
                            "Prior context": "new issue"}}
        elif "group reviewers' notes" in system:
            ids = [int(x) for x in re.findall(r"^- (\d+): ", prompt.split("<notes>")[1], re.M)]
            v = {"assign": [{"note": i, "category": "new:1"} for i in ids],
                 "new": [{"key": "new:1", "name": "Promises a refund", "description": "offers what policy forbids"}]}
        else:
            v = {"went_wrong": False, "note": "", "hint": "", "quotes": []}
        return N(model="claude-opus-5", stop_reason="end_turn", content=[N(type="text", text=json.dumps(v))])


def test_synthetic_runs_stay_out_of_production_and_compare_with_it(project, monkeypatch, capsys):
    for pairs in (["Issue=billing", "mood=frustrated", "Prior=new issue"], ["Issue=general", "mood=neutral", "Prior=new issue"]):
        main(["synth", "tuple", *pairs])
    main(["synth", "queries"])
    main(["synth", "run"])
    assay.flush()
    monkeypatch.setattr(judge_mod, "_client", lambda: Placer())
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{project.root / 's.db'}", review_person_first=0)))
    assert c.post("/v1/ingest", headers=H, json={"events": project.sent}).status_code == 200
    from datetime import datetime, timedelta
    ts = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    for i, (asked, said) in enumerate([("My invoice is wrong!", "Fixed."), ("The app crashes on start", "Reinstall."),
                                       ("Can you write me a poem?", "Sure.")]):
        ev = lambda k, **x: {"v": 1, "id": f"p{i}-{k}", "ts": ts, "run_id": f"prod-{i}", **x}
        c.post("/v1/ingest", headers=H, json=[ev(0, type="run.start", task="support", input=asked),
                                              ev(1, type="step", seq=0, kind="answer", text=said), ev(2, type="run.end")])
    engine = c.app.state.engine
    made = review.synthetic(engine, "s")
    assert len(made) == 2 and {v["Issue type"] for v in made.values()} == {"billing", "general"}
    Q = c.get("/v1/review/queue", params=SRC).json()["conversations"]
    assert sum(1 for x in Q if x["synthetic"]) == 2 and sum(1 for x in Q if not x["synthetic"]) == 3
    # A person finds a failure in a synthetic run: it's a category, but not a production share.
    synthetic_one = next(x for x in Q if x["synthetic"])
    c.post("/v1/review/notes", params=SRC, json={"conversation": synthetic_one["conversation"], "went_wrong": True,
                                                 "note": "Promised a refund the policy doesn't allow."})
    c.post("/v1/review/run", params={**SRC, "sample": 0})
    cat = c.get("/v1/review/categories", params=SRC).json()[0]
    assert (cat["notes"], cat["synthetic"], cat["only_synthetic"], cat["share"]) == (1, 1, True, None)
    from assay import learn
    from assay.models import Window
    from assay.sources.events import EventsSource
    sc = learn.score(EventsSource(engine, "s"), Window(datetime.utcnow() - timedelta(days=1), datetime.utcnow()), engine)
    assert set(sc["facts"]) == {"prod-0", "prod-1", "prod-2"}  # synthetic runs aren't scored as production
    out = c.post("/v1/synthetic/compare", params=SRC, json={"dimensions": [
        {"name": d.name, "values": d.values} for d in synth.config(synth.local_toml(project.root)).dimensions]}).json()
    assert (out["synthetic_runs"], out["placed"]) == (2, 3)
    issue = next(d for d in out["dimensions"] if d["dimension"] == "Issue type")
    assert issue["only_production"] == ["technical"] and issue["only_synthetic"] == ["general"]
    assert issue["fits_none"] == 1  # the poem: no value fits it
    assert out["categories_only_synthetic"] == ["Promises a refund"]
    assert c.post("/v1/synthetic/compare", params=SRC, json={"dimensions": [
        {"name": d.name, "values": d.values} for d in synth.config(synth.local_toml(project.root)).dimensions]}
    ).json()["summary"]["samples"] == 0  # placed once, kept
    text = synth.comparison_text(out)
    assert "Users ask this, nothing generated does: technical" in text and "Found only in synthetic runs" in text
