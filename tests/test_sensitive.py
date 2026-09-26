"""Evals when traces hold sensitive data: consented traces, experts with raw access (on the record), claim-level
expert decisions, checking redaction's output, and checking that edited traces behave like the real ones."""
import json
from datetime import datetime, timedelta
from types import SimpleNamespace as N

import pytest
from fastapi.testclient import TestClient

import assay_sdk as assay
from assay import auth, claims, learn, redaction, report
from assay.__main__ import main
from assay.api import create_app
from assay.config import Settings

SRC = {"source": "events:acme"}


def test_the_redactor_and_consistent_stand_ins():
    t = ("Hi, my name is Ana Lopez, born on 1988-04-12. Mail ana@x.com or ANA@x.com. Card 4111 1111 1111 1111, "
         "call +1 (415) 555-0199 from 10.0.0.12, at 42 Baker Street. Order O-10017.")
    assert learn.redact(t) == ("Hi, my name is <name>, born on <birth_date>. Mail <email> or <email>. Card <card>, "
                               "call <phone> from <ip>, at <address>. Order O-10017.")
    fake, mapping = learn.pseudonymize(t)
    assert "Alex Morgan" in fake and "person1@example.com or person1@example.com" in fake  # the same person, once
    assert "4242 4242 4242 4242" in fake and "+1 (415) 555-0001" in fake and "O-10017" in fake
    again, _ = learn.pseudonymize({"to": "ana@x.com", "cc": ["bob@y.org"]}, mapping)  # across a trace
    assert again == {"to": "person1@example.com", "cc": ["person2@example.com"]}
    assert learn._luhn(fake.split("Card ")[1][:19])  # a stand-in card still passes a checksum


EVENTS = [
    {"type": "run.start", "run_id": "r1", "task": "support", "input": "Where is my order? I'm ana@x.com"},
    {"type": "step", "run_id": "r1", "kind": "tool", "name": "lookup", "args": {"email": "ana@x.com"}},
    {"type": "step", "run_id": "r1", "kind": "answer", "text": "Found your account, ana@x.com: it ships today."},
    {"type": "run.end", "run_id": "r1", "status": "completed"},
    {"type": "run.start", "run_id": "r2", "task": "support", "input": "My card 4111 1111 1111 1111 was charged twice"},
    {"type": "step", "run_id": "r2", "kind": "tool", "name": "old_tool"},
    {"type": "step", "run_id": "r2", "kind": "answer", "text": "Refund started."},
    {"type": "run.end", "run_id": "r2", "status": "completed"},
    {"type": "run.start", "run_id": "t1", "task": "support", "input": "x@y.com", "test": {"case": "c"}},
]

APP = '''
import re
import assay_sdk as assay

@assay.tool
def lookup(email):
    return {"found": True}

def answer(q):
    m = re.search(r"[\\w.+-]+@[\\w-]+\\.\\w+", q)
    if not m:
        return "What email is your account under?"
    lookup(m.group(0))
    return f"Found your account, {m.group(0)}: it ships today."
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ASSAY_URL", raising=False)
    (tmp_path / ".assay").mkdir()
    (tmp_path / ".assay" / "events.jsonl").write_text("".join(json.dumps(e) + "\n" for e in EVENTS))
    (tmp_path / "bot.py").write_text(APP)
    yield tmp_path
    assay.init(enabled=False)


def test_redact_check_finds_what_got_through(project, capsys):
    assert main(["redact", "check"]) == 1
    out = capsys.readouterr().out
    assert "email" in out and "card" in out and "tool.args" in out and "ana@x.com" not in out  # masked
    clean = [{**e, "input": learn.redact(e.get("input"))} if "input" in e else e for e in EVENTS[:1]]
    (project / "clean.jsonl").write_text("".join(json.dumps(e) + "\n" for e in clean))
    assert main(["redact", "check", "--file", "clean.jsonl"]) == 0
    assert "Read these yourself" in capsys.readouterr().out


def test_edited_traces_are_checked_against_what_the_real_one_did(project, capsys):
    assert main(["redact", "replay", "--app", "bot.py:answer"]) == 0  # r2 differs, but so does its rerun
    out = capsys.readouterr().out
    assert "2 runs with personal data" in out and "1 behaved the same" in out and "r2: differs from the recording even unedited" in out
    assert main(["redact", "replay", "--app", "bot.py:answer", "--mode", "placeholder"]) == 1
    out = capsys.readouterr().out
    assert "r1 (email): tools lookup became (none); the answer changed" in out  # <email> isn't an email
    runs = redaction.runs_in(EVENTS)
    assert [r["run_id"] for r in runs] == ["r1", "r2"]  # a test-case run isn't a real input


def client(tmp_path, **kw):
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", **kw)))
    e = c.app.state.engine
    keys = {s: auth.create_key(e, "acme", s, scopes)[1] for s, scopes in
            {"ingest": ["ingest"], "read": ["read", "manage"], "expert": ["read", "manage", "sensitive"],
             "admin": ["admin"]}.items()}
    c.h = {k: {"Authorization": f"Bearer {v}"} for k, v in keys.items()}
    return c


def convo(c, rid, asked, answered, tags=None):
    ts = (datetime.utcnow() - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = lambda i, **k: {"v": 1, "id": f"{rid}-{i}", "ts": ts, "run_id": rid, **k}
    r = c.post("/v1/ingest", headers=c.h["ingest"], json=[
        ev(0, type="run.start", task="support", input=asked, tags=tags),
        ev(1, type="step", seq=0, kind="answer", text=answered), ev(2, type="run.end")])
    assert r.status_code == 200, r.text


def test_consent_and_experts_who_may_see_raw_data(tmp_path):
    c = client(tmp_path, review_consented_only=True, review_person_first=0)
    convo(c, "shared-1", "I'm ana@x.com, where's my order?", "On its way.", tags={"consent": "shared"})
    convo(c, "private-1", "I'm bob@y.org, cancel it", "Cancelled.")
    convo(c, "private-2", "Change my address please", "Done.")
    q = c.get("/v1/review/queue", params=SRC, headers=c.h["read"]).json()
    assert [x["conversation"] for x in q["conversations"]] == ["shared-1"] and not q["raw"] and q["consented_only"]
    assert "ana@x.com" not in json.dumps(q) and "<email>" in json.dumps(q)
    q = c.get("/v1/review/queue", params=SRC, headers=c.h["expert"]).json()
    assert q["raw"] and len(q["conversations"]) == 3 and "bob@y.org" in json.dumps(q)
    log = c.get("/v1/audit", headers=c.h["admin"]).json()
    seen = [x for x in log if x["action"] == "viewed raw conversations"]
    assert seen and set(seen[0]["detail"]["conversations"]) == {"shared-1", "private-1", "private-2"}
    assert "sensitive" not in auth.expand(["admin"])  # never implied: granted on its own
    r = c.post("/v1/review/notes", params=SRC, headers=c.h["expert"], json={
        "conversation": "private-1", "went_wrong": True, "note": "Cancelled bob@y.org's order without asking"})
    assert r.json()["note"] == "Cancelled <email>'s order without asking"  # stored redacted
    assert c.post("/v1/consent", params=SRC, headers=c.h["read"],
                  json={"trace_ids": ["private-2"]}).json()["updated"] == 1
    q = c.get("/v1/review/queue", params=SRC, headers=c.h["read"]).json()
    assert {x["conversation"] for x in q["conversations"]} == {"shared-1", "private-2"}  # private-1: read already


def test_an_experts_decisions_on_claims_become_labels_signals_and_findings(tmp_path, monkeypatch, capsys):
    c = client(tmp_path)
    convo(c, "ans-1", "Does drug A interact with B?", "Yes: A raises B's levels (Smith 2020). No dose change needed.")
    sent = []
    assay.init(transport=lambda b: sent.extend(b), flush_interval=60)
    try:
        assay.claim_review("ans-1", "A raises B's levels", "supported", evidence=[{"id": "smith-2020"}], by="clinician-1")
        assay.claim_review("ans-1", "No dose change needed", "wrong", correction="Halve the dose of B", by="clinician-1",
                           evidence=[{"id": "label-b"}])
        assay.claim_review("ans-1", "Onset in two days", "conflict_resolved", note="Took the label over the review")
        with pytest.raises(ValueError):
            assay.claim_review("ans-1", "x", "maybe")
        assay.flush()
    finally:
        assay.init(enabled=False)
    assert c.post("/v1/ingest", headers=c.h["ingest"], json={"events": sent}).status_code == 200
    s = c.get("/v1/claims", params=SRC, headers=c.h["read"]).json()
    assert (s["reviewed"], s["supported"], s["wrong"], s["conflict_resolved"]) == (3, 1, 1, 1)
    items = c.get("/v1/claims/golden", params=SRC, headers=c.h["read"]).json()["items"]
    assert [(x["output"], x["score"]) for x in items] == [("A raises B's levels", 1), ("No dose change needed", 0)]
    assert items[1]["critique"] == "It should say: Halve the dose of B"
    assert items[0]["input"]["question"] == "Does drug A interact with B?"
    from assay.models import Window
    from assay.sources.events import EventsSource
    e = c.app.state.engine
    now = datetime.utcnow()
    sc = learn.score(EventsSource(e, "acme"), Window(now - timedelta(days=1), now), e)
    sig = next(a for a in sc["anomalous"] if a["trace_id"] == "ans-1")["signals"]
    assert any(x["type"] == "expert" and "No dose change needed" in x["text"] for x in sig)
    md = report.markdown(report.build(e, "acme", source=EventsSource(e, "acme")))
    assert "Experts checked 3 claims in 1 answer: 1 supported, 1 wrong, 1 conflicts between sources resolved" in md
    # Into the golden set, from the command line: none twice.
    from assay import local
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assay.toml").write_text('[test]\ncommand = "true"\n')
    monkeypatch.setattr(local, "_http", lambda m, url, body, h: (200, c.get(
        url.replace("http://s", ""), headers=c.h["read"]).json()))
    monkeypatch.setenv("ASSAY_URL", "http://s")
    assert main(["golden", "claims", "--source", "events:acme"]) == 0
    assert main(["golden", "claims", "--source", "events:acme"]) == 0
    assert "0 claim labels from experts added" in capsys.readouterr().out.splitlines()[-1]
    assert len((tmp_path / "golden.jsonl").read_text().splitlines()) == 2
