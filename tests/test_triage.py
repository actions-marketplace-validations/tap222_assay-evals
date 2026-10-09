"""Cheap evaluators first (assay/triage.py, assay/upkeep.py): each failure category triaged into fix the prompt,
a code check (drafted, and tried on real conversations), or a judge only if it persisted after a prompt change;
and what each judge costs to keep."""
import json
import re
from datetime import datetime, timedelta
from types import SimpleNamespace as N

import pytest
from fastapi.testclient import TestClient

from assay import judge as judge_mod
from assay import store, triage
from assay.__main__ import main
from assay.api import create_app
from assay.config import Settings

H, SRC = {"X-Tenant": "q"}, {"source": "events:q"}
LONG = " ".join(["Thanks for reaching out about your order, here is everything you might want to know."] * 12)


class Model:
    def __init__(self, answer):
        self.messages, self.answer, self.calls = self, answer, []

    def create(self, **kw):
        system = kw["system"][0]["text"] if isinstance(kw["system"], list) else kw["system"]
        prompt = kw["messages"][-1]["content"]
        self.calls.append((system, prompt))
        if "group reviewers' notes" in system:
            ids = [int(x) for x in re.findall(r"^- (\d+): ", prompt.split("<notes>")[1], re.M)]
            v = {"assign": [{"note": i, "category": "new:1"} for i in ids],
                 "new": [{"key": "new:1", "name": "Answers far too long", "description": "a wall of text for a yes or no"}]}
        elif system == triage.RUBRIC:
            v = self.answer
        else:
            v = {"went_wrong": False, "note": "", "hint": "", "quotes": []}
        return N(model="claude-opus-5", stop_reason="end_turn", content=[N(type="text", text=json.dumps(v))])


def convo(c, cid, answer, days_ago):
    ts = (datetime.utcnow() - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ev = lambda i, **k: {"v": 1, "id": f"{cid}-{i}", "ts": ts, "run_id": cid, **k}
    assert c.post("/v1/ingest", headers=H, json=[
        ev(0, type="run.start", task="support", input=f"Did {cid} ship?"),
        ev(1, type="step", seq=0, kind="llm", model="m", prompt="support@13" if days_ago < 3 else "support@12"),
        ev(2, type="step", seq=1, kind="answer", text=answer), ev(3, type="run.end")]).status_code == 200


def make(tmp_path, monkeypatch, answer, after_bad=3):
    model = Model(answer)
    monkeypatch.setattr(judge_mod, "_client", lambda: model)
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", review_person_first=0)))
    c.model = model
    assert c.post("/v1/ingest", headers=H, json=[
        {"v": 1, "id": "p12", "ts": "2026-09-01T00:00:00Z", "type": "prompt", "prompt_id": "support", "version": "12",
         "template": "You are a support assistant. Help the customer."},
        {"v": 1, "id": "p13", "ts": "2026-09-01T00:00:00Z", "type": "prompt", "prompt_id": "support", "version": "13",
         "template": "You are a support assistant. Help the customer, politely."}]).status_code == 200
    e = c.app.state.engine
    now = datetime.utcnow()
    pv = store.prompt_versions
    with e.begin() as conn:  # v12 ten days ago, v13 three days ago
        for v, d in (("12", 10), ("13", 3)):
            conn.execute(pv.update().where(pv.c.version == v).values(registered_at=now - timedelta(days=d)))
    runs = [(f"bad-{i}", LONG, 5) for i in range(4)] + [(f"ok-{i}", "Yes, it shipped today.", 5) for i in range(4)]
    runs += [(f"bad-late-{i}", LONG, 1) for i in range(after_bad)]
    runs += [(f"ok-late-{i}", "Yes, it shipped yesterday.", 1) for i in range(6 - after_bad)]
    for cid, ans, d in runs:
        convo(c, cid, ans, d)
        r = c.post("/v1/review/notes", params=SRC, json={
            "conversation": cid, "went_wrong": ans == LONG, "trace_ids": [cid],
            "note": "A wall of text for a yes or no question." if ans == LONG else None})
        assert r.status_code == 200, r.text
    c.post("/v1/review/run", params={**SRC, "sample": 0})  # group the notes
    c.cid = c.get("/v1/review/categories", params=SRC).json()[0]["id"]
    return c


def test_a_rule_that_catches_it_becomes_a_drafted_code_check(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch, {"action": "code_check", "reason": "Length separates them.", "instruction": "",
                                     "check": {"kind": "max_words", "value": "60"}})
    t = c.post(f"/v1/review/categories/{c.cid}/triage", params=SRC).json()
    assert t["recommend"] == "code_check" and "at most 60 words" in t["why"]
    assert (t["trial"]["caught"], t["trial"]["of"], t["trial"]["false_alarms"], t["trial"]["of_fine"]) == (7, 7, 0, 7)
    compile(t["draft"], "draft.py", "exec")
    assert "len(answer.split()) <= 60" in t["draft"] and "pytest.mark.skip" in t["draft"]
    system, prompt = c.model.calls[-1]
    assert "Help the customer, politely." in prompt  # the latest registered version of what its conversations ran
    assert c.get("/v1/review/categories", params=SRC).json()[0]["triage"]["recommend"] == "code_check"  # kept


def test_a_rule_that_doesnt_separate_them_isnt_offered(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch, {"action": "code_check", "reason": "x", "instruction": "",
                                     "check": {"kind": "must_include", "value": "order"}})
    t = c.post(f"/v1/review/categories/{c.cid}/triage", params=SRC).json()
    assert not t["trial"]["offered"] and t["draft"] is None
    assert t["recommend"] == "judge" and "isn't offered" in t["why"]  # it persisted, so a judge is worth it


def test_a_judge_only_for_what_persisted_after_the_prompt_changed(tmp_path, monkeypatch):
    judge_says = {"action": "judge", "reason": "Tone is subjective.", "instruction": "", "check": {"kind": "none", "value": ""}}
    c = make(tmp_path, monkeypatch, judge_says)
    t = c.post(f"/v1/review/categories/{c.cid}/triage", params=SRC).json()
    per = t["persistence"]
    assert (per["state"], per["change"], per["before"], per["after"]) == ("persisted", "support@13", 0.5, 0.5)
    assert t["recommend"] == "judge" and "persisted after support@13" in t["why"]


def test_fixed_by_the_prompt_needs_no_evaluator(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch, {"action": "judge", "reason": "x", "instruction": "", "check": {"kind": "none", "value": ""}},
             after_bad=0)
    t = c.post(f"/v1/review/categories/{c.cid}/triage", params=SRC).json()
    assert t["persistence"]["state"] == "fixed" and t["recommend"] == "none" and "50% of conversations before" in t["why"]


def test_a_missing_instruction_is_a_prompt_fix(tmp_path, monkeypatch):
    c = make(tmp_path, monkeypatch, {"action": "fix_prompt", "reason": "Nothing asks for brevity.",
                                     "instruction": "Answer yes-or-no questions in one sentence.",
                                     "check": {"kind": "none", "value": ""}})
    t = c.post(f"/v1/review/categories/{c.cid}/triage", params=SRC).json()
    assert t["recommend"] == "fix_prompt" and "Answer yes-or-no questions in one sentence." in t["why"]


def test_subjective_with_no_prompt_change_tried_is_the_prompt_first():
    per = {"state": "no_change", "prompts": []}
    rec, why = triage._decide("judge", {"model": {"instruction": "", "reason": ""}}, per)
    assert rec == "fix_prompt" and "build a judge only if it persists" in why
    assert triage.check_problem({"kind": "must_match", "value": "("}).startswith("not a regular expression")
    assert triage.fails({"kind": "json", "value": ""}, '{"a": 1}') is False


def test_the_cli_writes_drafts_only_with_apply(tmp_path, monkeypatch, capsys):
    c = make(tmp_path, monkeypatch, {"action": "code_check", "reason": "x", "instruction": "",
                                     "check": {"kind": "max_words", "value": "60"}})
    from assay import local
    monkeypatch.chdir(tmp_path)
    route = lambda m, url, body, h: (lambda r: (r.status_code, r.json()))(
        c.request(m, url.replace("http://s", ""), json=body))
    monkeypatch.setattr(local, "_http", route)
    assert main(["triage", "--url", "http://s", "--source", "events:q", "--run"]) == 0
    out = capsys.readouterr().out
    assert "Answers far too long" in out and "A code check" in out and "would write tests/ai/test_triage_answers_far_too_long.py" in out
    assert not (tmp_path / "tests").exists()
    assert main(["triage", "--url", "http://s", "--source", "events:q", "--apply"]) == 0
    assert (tmp_path / "tests" / "ai" / "test_triage_answers_far_too_long.py").exists()


def test_what_each_judge_costs_to_keep(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assay.toml").write_text('[test]\ncommand = "true"\n', encoding="utf-8")
    (tmp_path / ".assay").mkdir()
    e = store.make_engine(f"sqlite:///{tmp_path / '.assay' / 'assay.db'}")
    now = datetime.utcnow()
    rows = []
    for i in range(10):
        rows.append(dict(tenant="local", result_id=f"c{i}", run_id="r1", case_id=f"case{i}", field="has_order_id",
                         status="pass", evaluator="pytest", ts=now))
        rows.append(dict(tenant="local", result_id=f"h{i}", run_id="r1", case_id=f"case{i}", field="helpful",
                         status="fail" if i < 4 else "pass", evaluator="assay.judge@1", judge_model="claude-opus-5",
                         score=2 if i < 4 else 4, reason="FAIL: far too long for a yes or no" if i < 4 else "fine", ts=now))
        rows.append(dict(tenant="local", result_id=f"t{i}", run_id="r1", case_id=f"case{i}", field="tone",
                         status="pass", evaluator="assay.judge@1", judge_model="claude-opus-5", score=4, reason="ok", ts=now))
    keys = {k for r in rows for k in r}
    rows = [{k: r.get(k) for k in keys} for r in rows]  # one shape: an insert of many takes the first row's columns
    with e.begin() as conn:
        conn.execute(store.eval_results.insert(), rows)
        conn.execute(store.calibrations.insert().values(
            tenant="local", run_id="cal-1", created_at=now - timedelta(days=19), judge="evals/judges.py:helpful",
            golden="x", passed=True, result={"calibration": {"field": "helpful", "n": 38, "spearman": 0.8,
                                                             "models": ["claude-opus-5"]}}))
    assert main(["evals", "audit"]) == 0
    out = capsys.readouterr().out
    assert "1 code check, 2 judges" in out
    assert "helpful: 38 labels; a judge needs 100" in out and "not calibrated in 19 days" in out
    assert "4 of its 4 failures are about length or format" in out
    assert "tone: never calibrated against people" in out
