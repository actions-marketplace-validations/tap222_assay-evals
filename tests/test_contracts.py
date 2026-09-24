from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from assay import contracts
from assay.api import create_app
from assay.config import Settings
from assay.contracts import breaks
from assay.models import DocumentRecord, StageRun, Window

NOW = datetime(2026, 9, 20, 12)
WINDOW = Window(NOW - timedelta(days=1), NOW)


class FakeSource:
    name = "fake"

    def __init__(self, paths, docs=None):
        """paths: {doc_id: [stage, ...]}; docs: {doc_id: DocumentRecord} (default: all finished)."""
        self.runs, self.docs = [], {}
        for i, (d, path) in enumerate(paths.items()):
            start = NOW - timedelta(hours=6) + timedelta(seconds=i)
            for k, s in enumerate(path):
                self.runs.append(StageRun(document_id=d, stage=s, status="success",
                                          started_at=start + timedelta(minutes=k), sequence=k))
            self.docs[d] = (docs or {}).get(d) or DocumentRecord(d, start, completed_at=start + timedelta(hours=1))

    def stage_runs(self, w):
        return [r for r in self.runs if w.start <= r.started_at < w.end]

    def documents(self, w):
        return [d for d in self.docs.values() if w.start <= d.received_at < w.end]

    def document_detail(self, d):
        if d not in self.docs:
            return None
        return self.docs[d], [r for r in self.runs if r.document_id == d], []


# ---------- one path ----------

def test_a_retry_is_fine_and_a_delete_is_not():
    base = ["search", "order", "respond"]
    rules = [{"kind": "never", "step": "delete"}, {"kind": "max_runs", "step": "order", "max_runs": 3},
             {"kind": "before", "step": "search", "other": "respond"}]
    retry = ["search", "order", "order", "respond"]
    dangerous = ["search", "delete", "respond"]
    assert all(breaks(c, base) is None for c in rules)
    assert all(breaks(c, retry) is None for c in rules)
    b = breaks(rules[0], dangerous)
    assert b["stage"] == "delete" and b["at"] == 1


def test_each_kind():
    p = ["a", "b", "c", "c", "c"]
    assert breaks({"kind": "must_include", "step": "d"}, p)["at"] is None
    assert breaks({"kind": "must_include", "step": "d"}, p, finished=False) is None  # still in flight
    assert breaks({"kind": "before", "step": "c", "other": "a"}, p)["stage"] == "a"
    assert breaks({"kind": "before", "step": "a", "other": "z"}, p) is None  # only judged when both ran
    assert breaks({"kind": "only_after", "step": "b", "other": "z"}, p)["detail"].endswith("without z")
    assert breaks({"kind": "only_after", "step": "a", "other": "b"}, p)["detail"].endswith("before b")
    assert breaks({"kind": "only_after", "step": "c", "other": "a"}, p) is None
    m = breaks({"kind": "max_runs", "step": "c", "max_runs": 2}, p)
    assert m["at"] == 4 and "3 times" in m["detail"]
    assert breaks({"kind": "allowed_steps", "steps": ["a", "b"]}, p)["stage"] == "c"


def test_scope():
    doc = DocumentRecord("d", NOW, segment="admin")
    c = {"kind": "never", "step": "delete", "unless": {"segment": ["admin"]}}
    assert contracts.applies(c, doc) is False
    assert contracts.applies(c, DocumentRecord("e", NOW, segment="acme")) is True
    assert contracts.applies({"kind": "never", "step": "x", "when": {"segment": ["acme"]}}, doc) is False
    assert contracts.applies(c, None) is None  # scoped, but nothing to judge it by


def test_validate():
    assert contracts.validate({"kind": "before", "step": "a"}) == "A before contract needs other."
    assert "Unknown kind" in contracts.validate({"kind": "sometimes"})
    assert "not region" in contracts.validate({"kind": "never", "step": "x", "when": {"region": ["eu"]}})
    assert contracts.validate({"kind": "never", "step": "x"}) is None


# ---------- many documents ----------

def test_check_counts_breaks_and_skips_unfinished():
    paths = {f"ok{i}": ["search", "order", "respond"] for i in range(20)}
    paths |= {"bad": ["search", "delete", "respond"], "open": ["search"]}
    docs = {"open": DocumentRecord("open", NOW - timedelta(hours=6))}  # not completed
    src = FakeSource(paths, docs)
    r = contracts.check(src, WINDOW, [{"id": 1, "kind": "never", "step": "delete"},
                                      {"id": 2, "kind": "must_include", "step": "respond"}])
    never, must = r["contracts"]
    assert never["violations"] == 1 and never["examples"][0]["document_id"] == "bad" and never["judged"] == 22
    assert must["violations"] == 0 and must["judged"] == 21  # the open document isn't judged yet
    assert r["broken"] == 1 and r["violating_documents"] == 1


def test_suggestions_come_from_what_documents_do():
    paths = {f"d{i}": ["search", "order"] + (["order"] if i % 5 == 0 else []) +
             (["review"] if i % 3 == 0 else []) + ["respond"] for i in range(60)}
    s = contracts.suggest(FakeSource(paths), WINDOW)
    kinds = {(x["kind"], x.get("step"), x.get("other")) for x in s["suggestions"]}
    assert ("must_include", "search", None) in kinds and ("must_include", "review", None) not in kinds
    assert ("before", "search", "order") in kinds
    assert ("only_after", "review", "order") in kinds
    assert next(x for x in s["suggestions"] if x["kind"] == "max_runs")["max_runs"] == 2
    assert all(x["kept_rate"] == 1 for x in s["suggestions"])
    # Already saved: not suggested again.
    again = contracts.suggest(FakeSource(paths), WINDOW, [{"kind": "must_include", "step": "search"}])
    assert ("must_include", "search") not in {(x["kind"], x.get("step")) for x in again["suggestions"]}


def test_shifts_list_new_steps_but_ignore_noise():
    prev = {f"p{i}": ["a", "b", "c"] for i in range(100)}
    cur = {f"c{i}": ["a", "b", "c"] if i % 10 else ["a", "x", "c"] for i in range(100)}
    src = FakeSource(cur)
    old = FakeSource(prev)
    for r in old.runs:
        r.started_at -= timedelta(days=1)
    src.runs += old.runs
    src.docs |= {k: DocumentRecord(k, v.received_at - timedelta(days=1), v.completed_at) for k, v in old.docs.items()}
    out = contracts.shifts(src, WINDOW)
    kinds = [s["kind"] for s in out["shifts"]]
    assert kinds[0] == "new_step" and out["shifts"][0]["step"] == "x"
    assert "share" in kinds  # a → b dropped from 100% to 90%, beyond noise


# ---------- alerts and API ----------

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}")))


def send(client, paths, hours_ago=2):
    now = datetime.utcnow() - timedelta(hours=hours_ago)
    docs, runs = [], []
    for d, path in paths.items():
        docs.append({"document_id": d, "received_at": now.isoformat(),
                     "completed_at": (now + timedelta(minutes=30)).isoformat(), "segment": "acme"})
        runs += [{"document_id": d, "stage": s, "status": "success", "sequence": k,
                  "started_at": (now + timedelta(minutes=k)).isoformat()} for k, s in enumerate(path)]
    assert client.post("/v1/events", json={"documents": docs, "stage_runs": runs},
                       headers={"X-Tenant": "t"}).status_code in (200, 201)


def test_one_break_opens_an_alert_and_a_clean_run_resolves_it(client):
    src = "events:t"
    c = client.post("/v1/contracts", json={"source": src, "kind": "never", "step": "delete"}).json()
    assert c["label"] == "delete never runs"
    send(client, {"ok": ["search", "order", "respond"], "bad": ["search", "delete", "respond"]})
    client.post("/v1/runs", json={"source": src, "days": 1})
    open_ = client.get("/v1/alerts", params={"source": src, "state": "open"}).json()
    assert len(open_) == 1 and open_[0]["kind"] == "contract" and "bad" in open_[0]["message"]

    wf = client.get("/v1/workflow", params={"source": src, "days": 1}).json()
    node = next(n for n in wf["nodes"] if n["id"] == "delete")
    assert node["health"] == "bad" and node["contract_violations"][0]["documents"] == 1
    assert next(e for e in wf["edges"] if e["to"] == "delete")["violations"] == 1
    doc = client.get("/v1/contracts/documents/bad", params={"source": src}).json()
    assert doc[0]["stage"] == "delete" and doc[0]["at"] == 1

    # Still broken on the next run: stays open, no second alert.
    client.post("/v1/runs", json={"source": src, "days": 1})
    assert len(client.get("/v1/alerts", params={"source": src, "state": "open"}).json()) == 1
    # Changing the rule closes the alert raised under the old one.
    client.put(f"/v1/contracts/{c['id']}", json={"source": src, "kind": "never", "step": "delete",
                                                   "unless": {"segment": ["acme"]}})
    assert client.get("/v1/alerts", params={"source": src, "state": "open"}).json() == []


def test_a_clean_run_resolves_and_an_unjudged_one_does_not(tmp_path):
    from assay import alerts, store
    engine = store.make_engine(f"sqlite:///{tmp_path / 'a.db'}")

    def run(violations, judged):
        with engine.begin() as conn:
            rid = conn.execute(store.measure_runs.insert().values(started_at=datetime.utcnow(), source="s",
                                                                  window_start=NOW, window_end=NOW)).inserted_primary_key[0]
        ex = [{"document_id": "d", "detail": "ran delete (step 2)"}] if violations else []
        return alerts.evaluate_contracts(engine, rid, {"contracts": [
            {"id": 7, "label": "delete never runs", "violations": violations, "judged": judged, "examples": ex}]})

    assert len(run(1, 10)["opened"]) == 1
    assert run(1, 10)["opened"] == []  # already open
    assert run(0, 0)["resolved"] == []  # nothing judged: can't say it's fixed
    assert len(run(0, 10)["resolved"]) == 1


def test_deleting_a_contract_resolves_its_alert(client):
    src = "events:t"
    c = client.post("/v1/contracts", json={"source": src, "kind": "must_include", "step": "review"}).json()
    send(client, {"a": ["search", "respond"]})
    client.post("/v1/runs", json={"source": src, "days": 1})
    assert len(client.get("/v1/alerts", params={"source": src, "state": "open"}).json()) == 1
    assert client.delete(f"/v1/contracts/{c['id']}").json() == {"deleted": c["id"]}
    client.post("/v1/runs", json={"source": src, "days": 1})
    assert client.get("/v1/alerts", params={"source": src, "state": "open"}).json() == []


def test_bad_contract_is_rejected(client):
    r = client.post("/v1/contracts", json={"source": "events:t", "kind": "only_after", "step": "a"})
    assert r.status_code == 422 and "other" in r.json()["detail"]
