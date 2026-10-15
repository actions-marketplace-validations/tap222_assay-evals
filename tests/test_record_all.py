"""Every test recorded (pytest --assay --assay-all): plain tests as cases, their outbound HTTP calls,
who ran them, and the weakened tests a run found, through upload to the dashboard's run list."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from assay import failures, local
from assay_sdk import http_calls

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")
REPO = str(Path(__file__).resolve().parents[1])
PYTEST = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"]

SERVER = '''
import http.server, threading

class H(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(404 if "missing" in self.path else 200)
        self.end_headers()
        self.wfile.write(b"ok")
    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.do_GET()
    def log_message(self, *a):
        pass

def start():
    s = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=s.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{s.server_port}"
'''


def test_the_same_call_gets_the_same_name_every_run():
    t = http_calls.template
    assert t("https://api.x.com/orders/12345/items/9f86d081884c7d659a2feaa0c55ad015?page=2&q=a&page=3") \
        == "api.x.com/orders/{id}/items/{id}?page&q"
    assert t("https://api.x.com/users/3fa85f64-5717-4562-b3fc-2c963f66afa6") == "api.x.com/users/{id}"
    assert t("http://localhost:8000") == "localhost/" and t("https://api.x.com/v1/charges") == "api.x.com/v1/charges"


def test_outbound_calls_are_recorded_into_the_run_once_each(tmp_path):
    # In its own process: record() patches http.client and httpx for the whole process.
    script = tmp_path / "calls.py"
    script.write_text(SERVER + textwrap.dedent('''
        import json, urllib.request, httpx
        import assay_sdk as assay
        from assay_sdk import http_calls
        sent = []
        assay.init(transport=lambda b: sent.extend(b), flush_interval=60)
        http_calls.record()
        base = start()
        urllib.request.urlopen(base + "/before-any-run")  # no run: nothing to record into
        with assay.run("checkout"):
            urllib.request.urlopen(base + "/orders/123?page=2")
            httpx.post(base + "/charges", json={"amount": 1200})
            try:
                urllib.request.urlopen(base + "/missing/7")
            except Exception:
                pass
        assay.flush()
        print(json.dumps([e for e in sent if e.get("type") == "step"]))
    '''), encoding="utf-8")
    out = subprocess.run([sys.executable, str(script)], capture_output=True, text=True,
                         env={**os.environ, "PYTHONPATH": os.pathsep.join([SDK, REPO])})
    assert out.returncode == 0, out.stderr
    steps = [s for s in json.loads(out.stdout) if s.get("kind") == "tool"]
    assert [s["name"] for s in steps] == ["HTTP GET 127.0.0.1/orders/{id}?page", "HTTP POST 127.0.0.1/charges",
                                          "HTTP GET 127.0.0.1/missing/{id}"]
    assert [s["result"] for s in steps] == [{"status": 200}, {"status": 200}, {"status": 404}]
    assert "1200" not in out.stdout  # bodies are never kept


def test_model_apis_and_the_assay_server_are_not_http_steps(monkeypatch):
    monkeypatch.setenv("ASSAY_URL", "https://assay.example.com")
    assert http_calls._skipped("api.anthropic.com") and http_calls._skipped("us-central1-aiplatform.googleapis.com")
    assert http_calls._skipped("assay.example.com") and not http_calls._skipped("api.stripe.com")


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def plain_project(tmp_path, monkeypatch):
    """A repository whose tests don't know about Assay: no assay_case anywhere."""
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_BASE_REF",
              "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "GITHUB_ACTIONS"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([SDK, REPO]))
    monkeypatch.setenv("NO_COLOR", "1")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "server.py").write_text(SERVER, encoding="utf-8")
    (tmp_path / "tests" / "conftest.py").write_text("import sys, os\nsys.path.insert(0, os.path.dirname(__file__))\n",
                                                    encoding="utf-8")
    (tmp_path / "tests" / "test_shop.py").write_text(textwrap.dedent('''
        import urllib.request
        from server import start

        def total(items):
            return sum(items)

        def test_total():
            assert total([500, 700]) == 1200

        def test_checkout_charges_once():
            base = start()
            assert urllib.request.urlopen(base + "/charges").status == 200
    '''), encoding="utf-8")
    (tmp_path / "assay.toml").write_text('[test]\ncommand = "pytest -q tests"\n', encoding="utf-8")
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "shop")
    return tmp_path


def run(project, *args, env=None):
    return subprocess.run([*PYTEST, "--assay", *args, "tests"], capture_output=True, text=True, cwd=project,
                          env={**os.environ, **(env or {})}, encoding="utf-8", errors="replace")


def recorded(project):
    runs = sorted((project / ".assay" / "runs").glob("*.jsonl"))
    events = [json.loads(x) for x in runs[-1].read_text(encoding="utf-8").splitlines() if x.strip()]
    meta = json.loads(runs[-1].with_suffix(".meta.json").read_text(encoding="utf-8"))
    return events, meta


def test_plain_tests_are_left_alone_unless_every_test_is_asked_for(plain_project):
    out = run(plain_project)
    assert out.returncode == 0 and "no test took the assay_case fixture" in out.stdout


def test_every_test_is_a_case_with_its_calls_who_ran_it_and_what_it_weakened(plain_project):
    first = run(plain_project, "--assay-all", env={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "sess-123"})
    assert first.returncode == 0, first.stdout + first.stderr
    events, meta = recorded(plain_project)
    cases = {e["test"]["case"] for e in events if e.get("type") == "run.start"}
    assert cases == {"tests/test_shop.py::test_total", "tests/test_shop.py::test_checkout_charges_once"}
    calls = [e["name"] for e in events if e.get("type") == "step" and e.get("kind") == "tool"]
    assert calls == ["HTTP GET 127.0.0.1/charges"]
    assert meta["ran_by"]["agent"] == "Claude Code" and meta["ran_by"]["session"] == "sess-123"
    assert meta["ran_by"]["branch"] == "main" and meta["ran_by"]["uncommitted"] is False

    # The agent loosens the assertion its change broke, and the checkout now calls twice.
    shop = plain_project / "tests" / "test_shop.py"
    shop.write_text(shop.read_text(encoding="utf-8").replace("== 1200", "> 0").replace(
        'assert urllib.request.urlopen(base + "/charges").status == 200',
        'urllib.request.urlopen(base + "/charges")\n    assert urllib.request.urlopen(base + "/charges").status == 200'),
        encoding="utf-8")
    second = run(plain_project, "--assay-all", env={"ASSAY_BASE_REF": "main"})
    events, meta = recorded(plain_project)
    assert [e["name"] for e in events if e.get("kind") == "tool"] == ["HTTP GET 127.0.0.1/charges"] * 2
    assert "agent" not in meta["ran_by"] and meta["ran_by"]["uncommitted"] is True
    assert [(f["test"], f["kind"]) for f in meta["weakened_tests"]] == [("test_total", "loosened")]
    assert "loosens an assertion in test_total" in second.stdout or "Weakened against main" in second.stdout
    # The behavior diff: the checkout now charges twice.
    d = subprocess.run([sys.executable, "-m", "assay", "diff", *sorted(
        p.stem for p in (plain_project / ".assay" / "runs").glob("*.jsonl"))], capture_output=True, text=True,
        cwd=plain_project, encoding="utf-8", errors="replace")
    assert "1 changed, still passing" in d.stdout and "HTTP GET 127.0.0.1/charges: twice instead of once" in d.stdout


def test_upload_carries_who_ran_it_and_the_weakened_tests(plain_project, monkeypatch):
    run(plain_project, "--assay-all", env={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "sess-9"})
    sent = []

    def http(method, url, body, headers):
        if method == "POST" and url.endswith("/v1/ingest"):
            sent.extend(body["events"])
        return (200, {"tenant": "*"}) if url.endswith("/whoami") else (200, {})
    # A weakened test found by the run, saved with it.
    home = plain_project / ".assay"
    run_id = local._state(home)["last"]
    meta_path = home / "runs" / f"{run_id}.meta.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["weakened_tests"] = [{"file": "tests/test_shop.py", "test": "test_total", "kind": "loosened", "line": 8,
                               "before": "assert total([500, 700]) == 1200", "after": "assert total([500, 700]) > 0",
                               "related": []}]
    meta_path.write_text(json.dumps(meta), encoding="utf-8")
    assert local.upload(plain_project, None, "http://assay.test", None, None, http=http) == 0
    tags = next(e["tags"] for e in sent if e.get("type") == "run.start")
    assert tags["ran_by_agent"] == "Claude Code" and tags["agent_session"] == "sess-9" and tags["branch"] == "main"
    assert json.loads(tags["weakened_tests"])[0]["kind"] == "loosened" and tags["weakened_total"] == 1
    origin = failures._origin(tags)  # what the dashboard's run list shows
    assert origin["ran_by_agent"] == "Claude Code" and origin["weakened_tests"][0]["test"] == "test_total"
    assert "related" not in origin["weakened_tests"][0]


def test_a_bad_weakened_tests_tag_is_dropped_not_fatal():
    o = failures._origin({"repo": "r", "weakened_tests": "{not json", "commit": "abc1234"})
    assert o == {"repo": "r", "commit": "abc1234"}


def test_a_plain_test_isnt_held_to_agent_checks(plain_project):
    # Polling an endpoint three times is a loop for an agent, and just a test for a test.
    (plain_project / "tests" / "test_poll.py").write_text(textwrap.dedent('''
        import urllib.request
        from server import start

        def test_polls_until_ready():
            base = start()
            for _ in range(3):
                assert urllib.request.urlopen(base + "/status").status == 200
    '''), encoding="utf-8")
    out = run(plain_project, "--assay-all")
    assert out.returncode == 0 and "Looped" not in out.stdout, out.stdout
    events, _ = recorded(plain_project)
    assert all(e["tags"].get("assay_record") == "plain" for e in events if e.get("type") == "run.start")


def test_on_the_server_a_plain_test_is_judged_by_its_asserts_and_says_who_ran_it(plain_project, tmp_path_factory):
    from fastapi.testclient import TestClient
    from assay.api import create_app
    from assay.config import Settings
    (plain_project / "tests" / "test_poll.py").write_text(textwrap.dedent('''
        import urllib.request
        from server import start

        def test_polls_until_ready():
            base = start()
            for _ in range(3):
                assert urllib.request.urlopen(base + "/status").status == 200
    '''), encoding="utf-8")
    assert run(plain_project, "--assay-all", env={"CLAUDECODE": "1", "CLAUDE_CODE_SESSION_ID": "s-1"}).returncode == 0
    server = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path_factory.mktemp('srv') / 's.db'}")))

    def http(method, url, body, headers):
        r = server.request(method, url.replace("http://assay.test", ""), json=body, headers=headers)
        return r.status_code, r.json()
    assert local.upload(plain_project, None, "http://assay.test", None, None, http=http) == 0
    source = f"events:{local.project_name(plain_project)}"
    (r,) = server.get("/v1/evals/runs", params={"source": source}).json()
    assert r["failed"] == 0 and r["passed"] > 0  # three calls to /status aren't a loop for a test
    assert r["origin"]["ran_by_agent"] == "Claude Code" and r["origin"]["branch"] == "main"
    agents_tab = server.get(f"/v1/agents/runs/{r['run_id']}", params={"source": source}).json()
    assert not any("fail" in t["checks"].values() for t in agents_tab["current"]["trajectories_list"])
    assert "efficiency" not in agents_tab["current"]["checks"]
