import json
import sqlite3
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from assay import connect, integrations, store
from assay.api import create_app
from assay.client import Assay
from assay.config import Settings


@pytest.fixture
def client(tmp_path):
    return TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'store.db'}")))


@pytest.fixture
def fake():
    """A local stand-in for Slack / Jira: records what it's sent, answers like them."""
    got = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
            got.append({"path": self.path, "body": body, "auth": self.headers.get("Authorization")})
            out = {"key": "QA-7"} if "/rest/api/3/issue" in self.path else {"ok": True}
            self.send_response(201 if "issue" in self.path else 200)
            self.end_headers()
            self.wfile.write(json.dumps(out).encode())

        def do_GET(self):
            got.append({"path": self.path})
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps({"name": "Quality"}).encode())

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", got
    srv.shutdown()


# ---------- spreadsheets ----------

CORRECTIONS = "Document,Field,Correct value,Was,Reviewer\ninv-1,total,1240.00,1204.00,sam\ninv-2,date,2026-03-07,2026-07-03,ana\n"


def test_a_spreadsheet_is_recognised_and_mapped():
    p = connect.preview(CORRECTIONS)
    assert p["kind"] == "errors" and p["valid"] == 2 and not p["problems"]
    assert p["mapping"] == {"document_id": "Document", "field": "Field", "expected": "Correct value",
                            "observed": "Was", "kind": None, "reporter": "Reviewer", "source": None,
                            "reported_at": None}
    tests = "Test case\tResult\tExpected\tGot\ncase-1\t✓\ta\ta\ncase-2\tFAIL\tb\tc\n"  # pasted cells are tab-separated
    p = connect.preview(tests, "eval_results")
    assert p["mapping"]["case_id"] == "Test case" and p["mapping"]["status"] == "Result"
    assert p["ask_run_name"] and p["valid"] == 2  # the person names the run instead of seeing an error
    good, bad, _ = connect.parse("eval_results", tests, defaults={"run_id": "upload-1"})
    assert [g.status for g in good] == ["pass", "fail"] and not bad


def test_bad_rows_are_explained_not_fatal():
    good, bad, _ = connect.parse("feedback", "Conversation,Reaction\nc1,👎\nc2,shrug\n")
    assert [g.kind for g in good] == ["thumbs_down"] and bad[0]["row"] == 3 and "kind" in bad[0]["problem"]


def test_import_turns_features_on(client):
    src = "events:t"
    st = client.get("/v1/connect/status", params={"source": src}).json()
    assert st["ready"] == 0 and not st["receiving"]
    r = client.post("/v1/connect/import", json={"text": CORRECTIONS}, headers={"X-Tenant": "t"}).json()
    assert r == {"kind": "errors", "imported": 2, "skipped": 0, "problems": [], "mapping": r["mapping"]}
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    client.post("/v1/connect/import", json={"text": f"id,received,type\ninv-1,{now},invoice\ninv-2,{now},invoice\n",
                                            "kind": "documents"}, headers={"X-Tenant": "t"})
    st = client.get("/v1/connect/status", params={"source": src}).json()
    ready = {f["id"] for f in st["features"] if f["ready"]}
    assert "monitoring" in ready and "errors" not in ready and st["receiving"]
    errors = next(f for f in st["features"] if f["id"] == "errors")
    assert errors["missing"] == ["steps or agent runs"] and errors["next"]
    h = client.get("/v1/connect/handoff", params={"source": src, "method": "otel"}).json()["text"]
    assert "/v1/otlp" in h and "events:t" in h


# ---------- integrations ----------

def test_slack_setup_hides_the_secret_and_gets_alerts(client, fake):
    url, got = fake
    src = "events:t"
    saved = client.put("/v1/integrations/slack", json={"source": src, "config": {"webhook_url": url + "/hook"}}).json()
    assert saved["webhook_url"] == integrations.MASK
    assert client.get("/v1/integrations", params={"source": src}).json()["configured"]["slack"]["webhook_url"] == \
        integrations.MASK
    # Saving the masked value back keeps the real one.
    client.put("/v1/integrations/slack", json={"source": src, "config": {"webhook_url": integrations.MASK}})
    t = client.post("/v1/integrations/slack/test", params={"source": src}).json()
    assert t["ok"] and got[-1]["path"] == "/hook" and "connected" in got[-1]["body"]["text"]
    notify = integrations.notifier(_engine(client), src)
    notify("opened", {"kind": "anomaly", "source": src, "message": "Cost per document jumped", "measure_id": "x"})
    assert "Cost per document jumped" in got[-1]["body"]["text"]


def _engine(client):
    return client.app.state.engine


def test_jira_ticket_from_a_pattern(client, fake):
    url, got = fake
    src, now = "events:j", datetime.utcnow()
    bad = client.put("/v1/integrations/jira", json={"source": src, "config": {"site": url}})
    assert bad.status_code == 422 and "API token" in bad.json()["detail"]
    client.put("/v1/integrations/jira", json={"source": src, "config": {
        "site": url, "email": "me@acme.com", "api_token": "tok", "project": "QA"}})
    assert client.post("/v1/integrations/jira/test", params={"source": src}).json()["message"] == \
        "Connected to Jira project Quality."
    client.post("/v1/contracts", json={"source": src, "kind": "never", "step": "delete_order"})
    trajs = [{"trajectory_id": f"t{i}", "task": "cancel", "started_at": (now - timedelta(hours=1)).isoformat(),
              "steps": [{"kind": "tool", "name": "delete_order" if i < 3 else "cancel_order", "args": {}}]}
             for i in range(20)]
    client.post("/v1/events/trajectories", json=trajs, headers={"X-Tenant": "j"})
    key = client.get("/v1/learn/patterns", params={"source": src}).json()["patterns"][0]["key"]
    t = client.post("/v1/learn/patterns/ticket", params={"source": src, "key": key}).json()
    assert t == {"tracker": "jira", "id": "QA-7", "url": f"{url}/browse/QA-7"}
    sent = got[-1]
    assert sent["body"]["fields"]["project"] == {"key": "QA"} and sent["auth"].startswith("Basic ")
    assert "delete_order" in sent["body"]["fields"]["summary"]
    p = client.get("/v1/learn/patterns", params={"source": src}).json()["patterns"][0]
    assert p["ticket_id"] == "QA-7"


def test_linear_ticket(client, monkeypatch):
    src = "events:l"
    client.put("/v1/integrations/linear", json={"source": src, "config": {"api_key": "lin_x", "team_id": "T1"}})
    seen = {}

    def post(url, body, headers=None, timeout=10):
        seen.update(url=url, body=body, headers=headers)
        return {"data": {"issueCreate": {"success": True, "issue": {"identifier": "ENG-3", "url": "https://l/ENG-3"}}}}
    monkeypatch.setattr(integrations, "_post", post)
    out = integrations.create_ticket(_engine(client), src, {"name": "Loops on search", "traces": 4, "trace_ids": ["a"]})
    assert out["id"] == "ENG-3" and seen["body"]["variables"]["i"]["teamId"] == "T1"


def test_ci_configs(client):
    for system in ("github", "gitlab", "script"):
        text = client.get("/v1/integrations/ci", params={"source": "events:t", "system": system}).json()["text"]
        assert "/v1/evals/runs/$RUN_ID/gate" in text and 'test "$OUTCOME" = "advance"' in text
    try:
        import yaml
    except ImportError:
        return
    gh = yaml.safe_load(client.get("/v1/integrations/ci", params={"source": "events:t"}).json()["text"])
    assert gh["jobs"]["gate"]["steps"][0]["env"]["ASSAY_KEY"] == "${{ secrets.ASSAY_KEY }}"
    gl = yaml.safe_load(client.get("/v1/integrations/ci", params={"source": "events:t", "system": "gitlab"}).json()["text"])
    assert "gate" in gl["assay-gate"]["script"][0]


# ---------- SDK ----------

def test_sdk_sends_agents_tests_inputs_and_feedback():
    sent = []
    a = Assay("http://x", transport=lambda path, payload: sent.append((path, payload)))
    with a.trajectory("run-1", task="refund", input="refund O-1", run_id="r", case_id="c") as t:
        t.reason(model="m", tokens=10)
        assert t.call_tool("get_order", {"order_id": "O-1"}, lambda order_id: {"id": order_id}) == {"id": "O-1"}
        with pytest.raises(KeyError):
            t.call_tool("issue_refund", {"order_id": "O-1"}, lambda order_id: {}["x"])
        t.state("refund:O-1", "create", {"amount": 5})
        t.answer("Done")
    a.eval_result("r", "c", "pass", field="answer", evaluator="exact@1")
    a.feedback("run-1", "thumbs_down")
    a.input("doc-9", input_ref="s3://x/doc-9.pdf")
    a.flush()
    body = sent[0][1]
    traj = body["trajectories"][0]
    assert [s["kind"] for s in traj["steps"]] == ["reason", "tool", "tool", "state", "answer"]
    assert traj["steps"][2]["error"].startswith("KeyError") and traj["answer"] == "Done"
    assert isinstance(traj["started_at"], str)  # datetimes inside steps are serialized too
    assert body["eval_results"][0]["status"] == "pass" and body["feedback"][0]["kind"] == "thumbs_down"
    assert body["inputs"][0]["input_ref"] == "s3://x/doc-9.pdf"


def test_sdk_payload_is_accepted_by_the_server(client):
    a = Assay("http://x", transport=lambda path, payload: client.post(path, json=payload, headers={"X-Tenant": "s"}))
    with a.trajectory("run-1", task="refund", input="hi") as t:
        t.tool("get_order", {"order_id": "O-1"}, {"ok": True})
        t.answer("Done")
    a.eval_result("r", "c", "fail", field="answer", expected="x", actual="y")
    a.feedback("run-1", "retry")
    a.flush()
    st = client.get("/v1/connect/status", params={"source": "events:s"}).json()["records"]
    assert (st["trajectories"]["total"], st["eval_results"]["total"], st["feedback"]["total"], st["inputs"]["total"]) \
        == (1, 1, 1, 1)


# ---------- upgrades ----------

def test_an_older_database_is_upgraded_in_place(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE pattern_log (source VARCHAR(64), key VARCHAR(512), name VARCHAR(512), "
                "first_seen DATETIME NOT NULL, last_seen DATETIME NOT NULL, traces INTEGER NOT NULL, "
                "status VARCHAR(16) NOT NULL, PRIMARY KEY (source, key))")
    con.execute("INSERT INTO pattern_log VALUES ('s','k','n','2026-01-01','2026-01-02',3,'open')")
    con.commit()
    con.close()
    e = store.make_engine(f"sqlite:///{db}")
    cols = [c[1] for c in sqlite3.connect(db).execute("pragma table_info(pattern_log)")]
    assert {"kind", "ticket_url", "protected_at"} <= set(cols)
    assert sqlite3.connect(db).execute("select traces from pattern_log").fetchone() == (3,)  # data kept
    assert store.upgrade(e) == []
