from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from assay import learn
from assay.api import create_app
from assay.config import Settings


def test_pii_scan_and_redact():
    text = "I'm jo@example.com, call +44 20 7946 0958, card 4111 1111 1111 1111, order O-10017"
    kinds = {x["kind"] for x in learn.pii_scan(text)}
    assert kinds == {"email", "phone", "card"}
    red = learn.redact({"msg": text, "n": 3})
    assert "<email>" in red["msg"] and "<card>" in red["msg"] and "O-10017" in red["msg"] and red["n"] == 3
    assert not learn.pii_scan("order O-10017 for 3 items")  # ids and small numbers aren't personal data


def test_representatives_are_typical_then_different():
    feats = {"a": {1, 2, 3}, "b": {1, 2, 3}, "c": {1, 2, 3, 4}, "z": {9, 8}}
    reps = learn.representatives(["a", "b", "c", "z"], feats, k=3)
    assert reps[0] in ("a", "b", "c") and "z" in reps and len(set(reps)) == len(reps)
    assert not ({"a", "b"} <= set(reps))  # near-duplicates aren't both chosen


def test_request_values_fill_expected_arguments():
    shape = learn._shape(["O-10017", "O-20001", "O-555"])
    import re
    assert re.search(shape, "refund order O-11409 please").group(0) == "O-11409"
    assert learn._shape(["free text here", "more text"]) is None


# ---------- the loop, end to end ----------

@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}")))


def trace(i, ts, looped=False, deleted=False):
    steps = [{"kind": "tool", "name": "lookup_customer", "args": {"email": f"c{i}@example.com"}}]
    if looped:
        steps += [{"kind": "tool", "name": "lookup_customer", "args": {"email": f"c{i}@example.com"}}] * 2
    steps += [{"kind": "tool", "name": "get_order", "args": {"order_id": f"O-{1000 + i}"}},
              {"kind": "tool", "name": "delete_order" if deleted else "cancel_order", "args": {"order_id": f"O-{1000 + i}"}},
              {"kind": "answer", "text": "Cancelled."}]
    return {"trajectory_id": f"t{i}", "task": "cancel_order", "started_at": ts.isoformat(),
            "finished_at": (ts + timedelta(seconds=9)).isoformat(), "answer": "Cancelled.",
            "input": f"Please cancel O-{1000 + i}, I'm c{i}@example.com", "steps": steps}


def test_production_failures_become_protected_regression_cases(client):
    src, now = "events:p", datetime.utcnow()
    client.post("/v1/contracts", json={"source": src, "kind": "never", "step": "delete_order", "severity": "critical"})
    trajs = [trace(i, now - timedelta(hours=40 - i // 4), looped=i % 25 == 0, deleted=i % 10 == 3) for i in range(120)]
    assert client.post("/v1/events/trajectories", json=trajs, headers={"X-Tenant": "p"}).json() == {"ingested": 120}
    client.post("/v1/events/feedback", json=[{"trace_id": "t13", "kind": "thumbs_down"}], headers={"X-Tenant": "p"})

    an = client.get("/v1/learn/anomalies", params={"source": src}).json()
    t13 = next(a for a in an["items"] if a["trace_id"] == "t13")
    assert {s["type"] for s in t13["signals"]} >= {"contract", "feedback"}

    pats = client.get("/v1/learn/patterns", params={"source": src}).json()
    deleted = next(p for p in pats["patterns"] if p["type"] == "contract")
    assert deleted["traces"] == 12 and deleted["status"] == "open" and deleted["kind"] == "failure"
    assert any(p["type"] == "loop" for p in pats["patterns"])

    made = client.post("/v1/learn/patterns/candidates", params={"source": src, "key": deleted["key"]}).json()
    assert 1 <= len(made) <= 3
    c = made[0]
    ref = c["case"]["reference"]
    assert [x["tool"] for x in ref["calls"]] == ["lookup_customer", "get_order", "cancel_order"]
    assert "email" not in str(ref["calls"])  # personal data never becomes an expectation
    assert ref["calls"][1]["args"]["order_id"].startswith("O-")
    assert c["pii"] and {p["from"] for p in c["provenance"]} >= {"pattern", "passing_traces", "property"}
    # Proposing again doesn't duplicate traces.
    again = client.post("/v1/learn/patterns/candidates", params={"source": src, "key": deleted["key"]}).json()
    assert not {x["trace_id"] for x in again} & {x["trace_id"] for x in made}

    ok = client.post(f"/v1/learn/candidates/{c['id']}/approve", json={"source": src, "suite": "prod"}).json()
    client.post(f"/v1/learn/candidates/{made[-1]['id']}/reject", json={"source": src, "note": "duplicate"})
    cases = client.get("/v1/learn/suites/prod", params={"source": src}).json()
    assert cases[0]["case_id"] == ok["case_id"] and "<email>" in cases[0]["input"]
    refs = client.get("/v1/agents/references", params={"source": src}).json()
    assert refs[0]["case_id"] == ok["case_id"]  # evaluation runs will check it

    pats = client.get("/v1/learn/patterns", params={"source": src}).json()
    assert next(p for p in pats["patterns"] if p["key"] == deleted["key"])["status"] == "protected"
    assert pats["loop"]["protected"] == 1 and pats["loop"]["median_hours_to_test"] is not None

    # The case runs in an eval: passing before, failing now → the production bug is back.
    case = ok["case_id"]
    for run, ts, bad in (("e1", now - timedelta(hours=5), False), ("e2", now - timedelta(hours=1), True)):
        t = trace(3 if bad else 4, ts, deleted=bad) | {"trajectory_id": f"{run}.{case}", "run_id": run,
                                                       "case_id": case, "attempt": 0}
        t["steps"][1]["args"] = t["steps"][2]["args"] = {"order_id": ref["calls"][1]["args"]["order_id"]}
        client.post("/v1/events/trajectories", json=[t], headers={"X-Tenant": "p"})
        client.post(f"/v1/agents/runs/{run}/evaluate", params={"source": src})
    out = client.get("/v1/evals/runs/e2/failures", params={"source": src}).json()
    assert out["stability"]["production_bugs_back"] == 1 and out["stability"]["outcome"] in ("hold", "rollback")
    g = next(g for g in out["groups"] if g.get("guards"))
    assert g["evidence"][0].startswith("Production bug back")


def test_dismissing_a_pattern(client):
    src, now = "events:q", datetime.utcnow()
    client.post("/v1/contracts", json={"source": src, "kind": "never", "step": "delete_order"})
    client.post("/v1/events/trajectories", json=[trace(i, now - timedelta(hours=2), deleted=i < 3) for i in range(30)],
                headers={"X-Tenant": "q"})
    key = client.get("/v1/learn/patterns", params={"source": src}).json()["patterns"][0]["key"]
    client.put("/v1/learn/patterns/status", json={"source": src, "key": key, "status": "dismissed"})
    p = client.get("/v1/learn/patterns", params={"source": src}).json()
    assert p["patterns"][-1]["status"] == "dismissed" and p["loop"]["patterns"] == 0
