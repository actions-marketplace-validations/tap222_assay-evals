"""The v1 event schema (assay/schema.py) and the Python SDK (sdk/python) against a real server."""
import json
import os
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from assay.api import create_app
from assay.config import Settings

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "sdk" / "python"))
import assay_sdk as assay  # noqa: E402


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}")))


@pytest.fixture
def sdk(client):
    """The SDK, sending through the test server; returns what it sent."""
    sent, responses = [], []

    def transport(batch):
        sent.extend(batch)
        r = client.post("/v1/ingest", json={"events": batch}, headers={"X-Tenant": "s"})
        responses.append(r)
        assert r.status_code == 200, r.text
    assay.init(transport=transport, flush_interval=60, strict=True)
    yield sent
    assay.shutdown()


def ev(**kw):
    return {"v": 1, "id": kw.pop("id"), "ts": kw.pop("ts", "2026-09-24T10:00:00Z"), **kw}


# ---------- the schema ----------

def test_schema_is_published_and_strict(client):
    s = client.get("/v1/schema").json()
    assert s["title"] == "Assay events, v1" and set(s["$defs"]) >= {"RunStart", "Step", "RunEnd", "Check"}
    bad = client.post("/v1/ingest", json=[ev(id="1", type="step", run_id="r", seq=0, kind="tool")])
    assert bad.status_code == 422 and "needs a name" in str(bad.json())
    bad = client.post("/v1/ingest", json=[ev(id="1", type="step", run_id="r", seq=0, kind="answer", args={"x": 1})])
    assert "doesn't take args" in str(bad.json())
    bad = client.post("/v1/ingest", json=[ev(id="1", type="run.start", run_id="r", taks="typo")])
    assert bad.status_code == 422  # unknown fields fail loudly
    assert client.post("/v1/ingest", json=[{**ev(id="1", type="run.start", run_id="r"), "v": 2}]).status_code == 422


def test_a_streamed_run_lands_whole_and_resending_changes_nothing(client):
    events = [
        ev(id="e3", type="step", run_id="r1", seq=1, kind="tool", name="get_order", args={"order_id": "O-17"},
           result={"price": 27.61}, ts="2026-09-24T10:00:02Z"),  # arrives before run.start
        ev(id="e1", type="run.start", run_id="r1", task="refund", input="refund O-17", version={"prompt": "p@v5"},
           test={"run": "nightly", "case": "c17", "attempt": 0}),
        ev(id="e2", type="step", run_id="r1", seq=0, kind="llm", model="m", tokens_in=600, tokens_out=100,
           cost_usd=0.002, ts="2026-09-24T10:00:01Z"),
        ev(id="e4", type="step", run_id="r1", seq=2, kind="state", name="refund:O-17", op="create",
           value={"amount": 27.61}),
        ev(id="e5", type="step", run_id="r1", seq=3, kind="answer", text="Refunded $27.61."),
    ]
    h = {"X-Tenant": "t"}
    assert client.post("/v1/ingest", json=events, headers=h).json()["by_type"] == {"run.start": 1, "step": 4}
    # The run is still going: every step so far is visible.
    t = client.get("/v1/agents/trajectories/r1", params={"source": "events:t"}).json()
    assert [s["kind"] for s in t["steps"]] == ["reason", "tool", "state", "answer"] and t["status"] == "running"
    end = [ev(id="e6", type="run.end", run_id="r1", ts="2026-09-24T10:00:05Z"),
           ev(id="e7", type="feedback", run_id="r1", kind="thumbs_up"),
           ev(id="e8", type="check", test={"run": "nightly", "case": "c17"}, run_id="r1", status="pass",
              field="answer")]
    client.post("/v1/ingest", json={"events": end}, headers=h)
    client.post("/v1/ingest", json={"events": events + end}, headers=h)  # a retry of everything
    t = client.get("/v1/agents/trajectories/r1", params={"source": "events:t"}).json()
    assert len(t["steps"]) == 4 and t["answer"] == "Refunded $27.61." and t["status"] == "completed"
    assert t["run_id"] == "nightly" and t["case_id"] == "c17" and t["lineage"] == {"prompt": "p@v5"}
    st = client.get("/v1/connect/status", params={"source": "events:t"}).json()["records"]
    assert (st["trajectories"]["total"], st["feedback"]["total"], st["eval_results"]["total"],
            st["inputs"]["total"]) == (1, 1, 1, 1)
    runs = client.get("/v1/evals/runs", params={"source": "events:t"}).json()
    assert runs[0]["lineage"] == {"prompt": "p@v5"}  # checks inherit the run's versions


def test_pipeline_runs_become_stages_and_model_calls(client):
    h = {"X-Tenant": "p"}
    client.post("/v1/ingest", headers=h, json=[
        ev(id="a", type="run.start", run_id="doc-1", kind="pipeline", task="invoice"),
        ev(id="b", type="step", run_id="doc-1", seq=0, kind="stage", name="extract", outputs={"total": "12.40"},
           prompt="extract_fields@v13", ended_at="2026-09-24T10:00:03Z"),
        ev(id="c", type="step", run_id="doc-1", seq=1, parent_seq=0, kind="llm", name="extract", model="m",
           cost_usd=0.01, ended_at="2026-09-24T10:00:02Z"),
        ev(id="d", type="run.end", run_id="doc-1", ts="2026-09-24T10:00:04Z"),
        ev(id="e", type="correction", run_id="doc-1", field="total", expected="1240.00", observed="12.40")])
    tr = client.get("/v1/trace/doc-1", params={"source": "events:p"}).json()
    assert [s["stage"] for s in tr["stages"]] == ["extract"] and tr["calls"][0]["cost_usd"] == 0.01
    assert client.get("/v1/agents/trajectories/doc-1", params={"source": "events:p"}).status_code == 404
    err = client.get("/v1/errors/doc-1", params={"source": "events:p"}).json()
    assert err["errors"][0]["origin_stage"] == "extract"  # the correction is traced to its step


# ---------- the SDK ----------

def test_sdk_agent_run_end_to_end(client, sdk):
    def get_order(order_id):
        return {"order_id": order_id, "price": 27.61}

    with assay.run("refund", input="refund O-17 for jo@x.com", version={"prompt": "p@v5"}) as run:
        run.llm(model="m", tokens_in=600, tokens_out=100, cost_usd=0.002)
        order = run.call("get_order", get_order, order_id="O-17")
        with pytest.raises(ZeroDivisionError):
            run.call("risky", lambda: 1 / 0)
        run.state("refund:O-17", "create", {"amount": order["price"]})
        run.answer("Refunded $27.61.")
    assay.feedback(run.id, "thumbs_down", note="wrong order")
    assay.expect("c1", calls=[{"tool": "get_order", "args": {"order_id": "O-17"}}], answer="27.61")
    assert assay.flush()
    assert [e["type"] for e in sdk] == ["run.start", "step", "step", "step", "step", "step", "run.end", "feedback",
                                        "expect"]
    assert all(e["v"] == 1 and e["id"] and e["ts"].endswith("Z") for e in sdk)
    t = client.get(f"/v1/agents/trajectories/{run.id}", params={"source": "events:s"}).json()
    assert [s["name"] for s in t["steps"] if s["kind"] == "tool"] == ["get_order", "risky"]
    assert t["steps"][2]["error"].startswith("ZeroDivisionError") and t["answer"] == "Refunded $27.61."


def test_sdk_run_that_raises_is_failed_and_keeps_its_steps(client, sdk):
    with pytest.raises(RuntimeError):
        with assay.run("refund") as run:
            run.tool("get_order", {"order_id": "O-1"}, {"ok": True})
            raise RuntimeError("crashed")
    assay.flush()
    t = client.get(f"/v1/agents/trajectories/{run.id}", params={"source": "events:s"}).json()
    assert t["status"] == "failed" and len(t["steps"]) == 1


def test_sdk_pipeline_and_checks(client, sdk):
    with assay.run("invoice", kind="pipeline", input_ref="s3://inbox/a.pdf") as run:
        with run.stage("extract", prompt="extract_fields@v13") as s:
            run.llm(model="m", cost_usd=0.01)
            s.outputs.update(total="12.40")
    assay.check("nightly", "case-1", "fail", run_id=run.id, field="total", expected=1240.0, actual="12.40",
                evaluator="exact@1")
    assay.correction(run.id, "total", expected="1240.00", observed="12.40")
    assay.flush()
    stage = next(e for e in sdk if e["type"] == "step" and e["kind"] == "stage")
    llm = next(e for e in sdk if e["type"] == "step" and e["kind"] == "llm")
    assert llm["parent_seq"] == stage["seq"] == 0 and llm["seq"] == 1
    out = client.get("/v1/evals/runs/nightly/failures", params={"source": "events:s"}).json()
    assert out["failures"] == 1 and out["groups"][0]["examples"][0]["origin_stage"] == "extract"


def test_sdk_redacts_samples_and_never_raises(client):
    sent = []
    assay.init(transport=lambda b: sent.extend(b), flush_interval=60,
               redact=lambda v: v.replace("jo@x.com", "<email>") if isinstance(v, str) else v)
    with assay.run("t", input="mail jo@x.com") as run:
        run.answer("sent to jo@x.com")
    assay.flush()
    assert "jo@x.com" not in str(sent) and "<email>" in str(sent)

    sent.clear()
    assay.init(transport=lambda b: sent.extend(b), flush_interval=60, sample=0.0)
    with assay.run("t") as run:
        run.answer("x")
    assay.feedback(run.id, "thumbs_up")
    assay.flush()
    assert [e["type"] for e in sent] == ["feedback"]  # the run wasn't sampled; the outcome is still sent

    def down(batch):
        raise ConnectionError("server down")
    assay.init(transport=down, flush_interval=60)
    with assay.run("t") as run:  # nothing raises into the app
        run.answer("x")
    assert assay.flush() is False
    kept = assay._client._queue
    assert len(kept) == 3  # kept for the next try, in order
    assay.shutdown()


def test_sdk_streams_in_the_background(client):
    got = []
    assay.init(transport=lambda b: got.extend(b), flush_interval=0.05)
    with assay.run("t") as run:
        run.answer("x")
    deadline = time.time() + 2
    while len(got) < 3 and time.time() < deadline:
        time.sleep(0.02)
    assert len(got) == 3
    assay.shutdown()


# ---------- no server: record locally, then `assay load` ----------

def test_sdk_records_locally_without_a_server_and_load_brings_it_in(tmp_path, monkeypatch, capsys):
    from assay.__main__ import main
    monkeypatch.delenv("ASSAY_URL", raising=False)
    monkeypatch.delenv("ASSAY_PATH", raising=False)
    monkeypatch.chdir(tmp_path)
    assay.init(flush_interval=60, strict=True)
    with assay.run("refund", input="refund O-17", test={"run": "local-1", "case": "c1"}) as run:
        run.tool("get_order", {"order_id": "O-17"}, {"price": 27.61})
        run.answer("Refunded $27.61.")
    assay.check("local-1", "c1", "pass", run_id=run.id, field="answer")
    assay.shutdown()

    log = tmp_path / ".assay" / "events.jsonl"
    lines = log.read_text().splitlines()
    assert [json.loads(x)["type"] for x in lines] == ["run.start", "step", "step", "run.end", "check"]
    assert (tmp_path / ".assay" / ".gitignore").read_text() == "*.jsonl\n"  # recorded inputs stay out of git

    monkeypatch.setenv("ASSAY_STORE_URL", f"sqlite:///{tmp_path / 'store.db'}")
    assert main(["load"]) == 0 and "Loaded 5 events into tenant 'local'" in capsys.readouterr().out
    assert main(["load", str(log)]) == 0  # again: nothing doubles
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}")))
    t = c.get(f"/v1/agents/trajectories/{run.id}", params={"source": "events:local"}).json()
    assert t["status"] == "completed" and len(t["steps"]) == 2 and t["answer"] == "Refunded $27.61."


def test_sdk_local_path_and_processes_appending_to_one_file(tmp_path, monkeypatch):
    import subprocess
    sdk_dir = str(Path(__file__).resolve().parents[1] / "sdk" / "python")
    script = ("import assay_sdk as assay\n"
              "assay.init(flush_interval=60, batch_size=7)\n"
              "for _ in range(40):\n"
              "    with assay.run('t') as run:\n"
              "        run.answer('x' * 3000)\n")  # long lines, many batches, all at once
    env = {**os.environ, "PYTHONPATH": sdk_dir, "ASSAY_PATH": str(tmp_path / "runs.jsonl")}
    env.pop("ASSAY_URL", None)
    procs = [subprocess.Popen([sys.executable, "-c", script], env=env) for _ in range(4)]
    assert all(p.wait(timeout=60) == 0 for p in procs)
    lines = (tmp_path / "runs.jsonl").read_text().splitlines()
    assert len(lines) == 4 * 40 * 3 and all(json.loads(x)["v"] == 1 for x in lines)  # no line split or lost
    assert not (tmp_path / ".gitignore").exists()  # only a folder the SDK created gets one


def test_load_checks_every_line_and_loads_nothing_from_a_bad_file(tmp_path, monkeypatch, capsys):
    from assay.__main__ import main
    good = json.dumps(ev(id="a", type="run.start", run_id="r"))
    bad_step = json.dumps(ev(id="b", type="step", run_id="r", seq=0, kind="tool"))  # a tool step needs a name
    f = tmp_path / "events.jsonl"
    f.write_text("\n".join([good, "{not json", bad_step]) + "\n")
    monkeypatch.setenv("ASSAY_STORE_URL", f"sqlite:///{tmp_path / 'store.db'}")
    assert main(["load", str(f)]) == 1
    err = capsys.readouterr().err
    assert "2 bad line(s)" in err and "line 2: not JSON" in err and "line 3:" in err and "needs a name" in err
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}")))
    assert c.get("/v1/agents/trajectories/r", params={"source": "events:local"}).status_code == 404
    assert main(["load", str(tmp_path / "missing.jsonl")]) == 2
