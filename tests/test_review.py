"""Reading production conversations (assay/review.py): notes with checked quotes, grouped into categories
that keep their identity, reviewed by a person, and turned into test cases and personas."""
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace as N

import pytest
from fastapi.testclient import TestClient

from assay import judge as judge_mod
from assay import review
from assay.api import create_app
from assay.config import Settings

H, SRC = {"X-Tenant": "q"}, {"source": "events:q"}


class Reader:
    """A model reading conversations: flags the ones where the assistant answered the policy, not the question."""

    def __init__(self, invent=()):
        self.messages, self.invent, self.calls = self, set(invent), []

    def create(self, **kw):
        self.calls.append(kw)
        system = kw["system"][0]["text"] if isinstance(kw["system"], list) else kw["system"]
        prompt = kw["messages"][-1]["content"]
        if "group reviewers' notes" in system:
            ids = [int(x) for x in re.findall(r"^- (\d+): ", prompt.split("<notes>")[1], re.M)]
            known = re.findall(r"^- (\d+): ", prompt.split("<notes>")[0], re.M)
            cat = known[0] if known else "new:1"
            v = {"assign": [{"note": i, "category": cat} for i in ids],
                 "new": [] if known else [{"key": "new:1", "name": "Answers the policy, not the question",
                                           "description": "asked about an order, got the refund policy"}]}
        elif "refund policy" in prompt:
            order = re.search(r"order (O-\d+)", prompt).group(1)
            quote = "Our refund policy is 30 days" if order not in self.invent else "we lost your parcel"
            if order in self.invent and "retry" in self.invent:
                self.invent.discard(order)  # it gets it right when read again, the next day
            self.invent.add("retry") if order in self.invent else None
            v = {"went_wrong": True, "note": "The user asked where the order is; the answer quoted the refund policy.",
                 "hint": "missed intent", "quotes": [quote]}
        else:
            v = {"went_wrong": False, "note": "", "hint": "", "quotes": []}
        return N(model="claude-opus-5", stop_reason="end_turn", content=[N(type="text", text=json.dumps(v))])


def convo(c, cid, asked, answered, hours_ago=2):
    ts = (datetime.utcnow() - timedelta(hours=hours_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = lambda i, **k: {"v": 1, "id": f"{cid}-{i}", "ts": ts, "run_id": cid, **k}
    r = c.post("/v1/ingest", headers=H, json=[
        ev(0, type="run.start", task="support", input=asked, conversation_id=cid, turn=0),
        ev(1, type="step", seq=0, kind="tool", name="lookup_policy", args={}),
        ev(2, type="step", seq=1, kind="answer", text=answered), ev(3, type="run.end", outcome="resolved")])
    assert r.status_code == 200


@pytest.fixture
def app(tmp_path, monkeypatch):
    reader = Reader(invent={"O-3"})
    monkeypatch.setattr(judge_mod, "_client", lambda: reader)
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}")))
    c.reader = reader
    for i in range(1, 4):  # fluent, nothing errors, and it misses what was asked
        convo(c, f"bad-{i}", f"Where is my order O-{i}? It hasn't arrived.", "Our refund policy is 30 days from delivery.")
    for i in range(4, 7):
        convo(c, f"ok-{i}", f"Where is my order O-{i}?", f"O-{i} is out for delivery today.")
    return c


def test_reading_finds_what_no_rule_does_and_groups_it(app):
    out = app.post("/v1/review/run", params={**SRC, "sample": 10}).json()
    assert (out["read"], out["went_wrong"]) == (5, 2)  # O-3's note quoted what isn't there: dropped, not counted
    assert out["new_categories"] == ["Answers the policy, not the question"]
    cats = app.get("/v1/review/categories", params=SRC).json()
    assert len(cats) == 1 and cats[0]["notes"] == 2 and cats[0]["share"] == pytest.approx(0.4)
    ex = cats[0]["examples"][0]
    assert ex["quotes"] == ["Our refund policy is 30 days"] and "refund policy" in ex["note"]

    assert app.post("/v1/review/run", params={**SRC, "sample": 10}).json()["read"] == 1  # O-3 is read again
    convo(app, "bad-9", "Where is my order O-9?", "Our refund policy is 30 days from delivery.")
    again = app.post("/v1/review/run", params={**SRC, "sample": 10}).json()
    assert again["read"] == 1 and again["new_categories"] == []  # into the category that exists
    assert app.get("/v1/review/categories", params=SRC).json()[0]["notes"] == 4  # O-1, O-2, O-3 read again, O-9


def test_a_person_reviews_categories_and_they_feed_the_loop(app):
    app.post("/v1/review/run", params={**SRC, "sample": 10})
    cid = app.get("/v1/review/categories", params=SRC).json()[0]["id"]
    assert app.put(f"/v1/review/categories/{cid}", params=SRC, json={"status": "confirmed"}).json()["status"] == "confirmed"
    personas = app.get(f"/v1/review/categories/{cid}/personas", params=SRC).json()
    assert personas and personas[0]["goal"].startswith("get what you asked for: Where is my order O-")
    made = app.post(f"/v1/review/categories/{cid}/candidates", params=SRC).json()
    assert made and made[0]["pattern"] == f"review:{cid}"
    assert made[0]["provenance"][0]["from"] == "review" and "refund policy" in made[0]["provenance"][0]["detail"]
    assert app.put(f"/v1/review/categories/{cid}", params=SRC, json={"merge_into": cid}).status_code == 422


def test_quotes_are_checked():
    text = "USER: where is O-1?\nASSISTANT: Our refund policy is 30 days."
    assert review.check_note({"went_wrong": True, "note": "x", "quotes": ["refund policy is 30 days"]}, text) is None
    assert "aren't in the conversation" in review.check_note({"went_wrong": True, "note": "x", "quotes": ["lost it"]}, text)
    assert review.check_note({"went_wrong": False}, text) is None
    assert "isn't the note" in review.check_note("nope", text)


def test_a_person_reads_the_queue_and_their_notes_replace_the_models(app):
    app.post("/v1/review/run", params={**SRC, "sample": 10})
    Q = app.get("/v1/review/queue", params=SRC).json()
    assert len(Q) == 6 and all(c["turns"] for c in Q)
    assert [c["suggestion"]["went_wrong"] for c in Q[:2]] == [True, True]  # the likeliest wrong first
    one = Q[0]
    roles = [t["role"] for t in one["turns"]]
    assert roles[0] == "user" and "tool" in roles and roles[-1] == "assistant"
    tool = next(t for t in one["turns"] if t["role"] == "tool")
    assert tool["name"] == "lookup_policy" and tool["seq"] == 0
    # Accept the model's suggestion: it becomes the person's note, in the same category.
    r = app.post("/v1/review/notes", params=SRC, json={"conversation": one["conversation"], "went_wrong": True,
                                                       "accept": one["suggestion"]["id"],
                                                       "first_step": {"trace_id": tool["trace_id"], "seq": 0}})
    assert r.status_code == 200 and not r.json()["by"].startswith("model:")
    cats = app.get("/v1/review/categories", params=SRC).json()
    assert cats[0]["notes"] == 2 and cats[0]["by_people"] == 1  # replaced, not counted twice
    # Reject the other: nothing went wrong, in the person's judgement; the model's note stops counting.
    two = Q[1]
    app.post("/v1/review/notes", params=SRC, json={"conversation": two["conversation"], "went_wrong": False})
    assert app.get("/v1/review/categories", params=SRC).json()[0]["notes"] == 1
    # A note of their own on one the model thought fine, grouped on the next run.
    fine = next(c for c in Q if c["suggestion"] and not c["suggestion"]["went_wrong"])
    assert app.post("/v1/review/notes", params=SRC, json={"conversation": fine["conversation"],
                                                          "went_wrong": True}).status_code == 422  # say what
    app.post("/v1/review/notes", params=SRC, json={"conversation": fine["conversation"], "went_wrong": True,
                                                   "note": "It never said when the parcel would arrive."})
    left = {c["conversation"] for c in app.get("/v1/review/queue", params=SRC).json()}
    assert not left & {one["conversation"], two["conversation"], fine["conversation"]}
    app.post("/v1/review/run", params={**SRC, "sample": 10})  # people's notes are grouped with the rest
    cat = app.get("/v1/review/categories", params=SRC).json()[0]
    assert (cat["notes"], cat["by_people"]) == (3, 2)  # and O-3, read right by the model this time
