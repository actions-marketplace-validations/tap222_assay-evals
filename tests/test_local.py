"""Local testing: `assay init`, `assay test` and `assay accept` (assay/local.py), end to end."""
import json
import os
import sys
from pathlib import Path

import pytest

from assay import local
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ASSAY_URL", raising=False)
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([SDK, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.syspath_prepend(SDK)  # `assay test` checks the SDK's version in its own process
    return tmp_path


def config(root, command, repeat=1, contracts=""):
    (root / "assay.toml").write_text(f'[test]\ncommand = "{command}"\nrepeat = {repeat}\n{contracts}')


def test_init_writes_a_runnable_setup_once(project, capsys):
    assert main(["init"]) == 0
    assert (project / "assay.toml").exists() and (project / local.EXAMPLE).exists()
    assert (project / ".assay" / ".gitignore").read_text() == "*\n"  # nothing under .assay goes in git
    (project / "assay.toml").write_text("# mine\n")
    assert main(["init"]) == 0 and "nothing changed" in capsys.readouterr().out
    assert (project / "assay.toml").read_text() == "# mine\n"


def test_the_example_passes_then_a_bad_change_fails_then_the_fix_passes(project, capsys):
    main(["init"])
    config(project, f"{sys.executable} {local.EXAMPLE}",
           contracts='[[contracts]]\nkind = "never"\nstep = "delete_order"\n')
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "2 cases · 1 attempt each · no baseline yet" in out and "✓ Safety      2/2" in out
    first = json.loads((project / ".assay" / "state.json").read_text())["baseline"]

    good = (project / local.EXAMPLE).read_text()
    (project / local.EXAMPLE).write_text(good.replace(
        '        run.answer(f"Order {order_id} hasn\'t arrived yet, so it can\'t be refunded.")\n        return\n',
        '        run.call("delete_order", lambda order_id: None, order_id=order_id)\n'))
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert f"compared with the baseline, {first}" in out and "⚠ 1 case regressed (4 checks)" in out
    assert "refund_not_delivered" in out and "delete_order never runs" in out and "Failed." in out
    assert json.loads((project / ".assay" / "state.json").read_text())["baseline"] == first  # a failure isn't one

    (project / local.EXAMPLE).write_text(good)
    assert main(["test"]) == 0 and "This run is now the baseline." in capsys.readouterr().out


EXTRACT = '''
import os
import assay_sdk as assay
assay.init()
after, attempt = os.environ.get("MODE") == "after", int(os.environ["ASSAY_TEST_ATTEMPT"])
for i in range(20):
    date_ok = not (after and i < 4)
    vendor_ok = attempt % 2 == 0 if i == 19 else True   # flaky the same way in every run
    assay.check(None, f"inv_{i:02d}", "pass", field="total", expected="1", actual="1")
    assay.check(None, f"inv_{i:02d}", "pass" if date_ok else "fail", field="invoice_date",
                expected="2026-09-01", actual="2026-09-01" if date_ok else "2026-01-09")
    assay.check(None, f"inv_{i:02d}", "pass" if vendor_ok else "fail", field="vendor", expected="Acme",
                actual="Acme" if vendor_ok else "ACME")
'''


def test_fields_known_failures_accept_and_flakiness(project, capsys, monkeypatch):
    (project / "extract.py").write_text(EXTRACT)
    config(project, f"{sys.executable} extract.py", repeat=4)
    assert main(["test"]) == 1  # inv_19's vendor fails half its attempts, and there's no baseline
    out = capsys.readouterr().out
    assert "no baseline yet" in out and "inv_19" in out and "assay accept" in out
    assert main(["accept"]) == 0

    monkeypatch.setenv("MODE", "after")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "✗ invoice_date  16/20   100% → 80%" in out
    assert "invoice_date  4 cases: inv_00, inv_01, inv_02, …" in out  # one item, not four
    assert "1 flaky check" in out and "not blocking" in out  # inv_19's vendor: flaky before too

    monkeypatch.setenv("MODE", "before")
    assert main(["test"]) == 0  # only the flaky check fails: it doesn't block


def test_setup_problems_exit_2(project, capsys):
    assert main(["test"]) == 2 and "Run `assay init` first" in capsys.readouterr().err
    config(project, f"{sys.executable} -c pass")
    assert main(["test"]) == 2 and "recorded nothing" in capsys.readouterr().err
    config(project, "x", contracts='[[contracts]]\nkind = "sometimes"\nstep = "a"\n')
    assert main(["test"]) == 2 and "contract 1: Unknown kind 'sometimes'" in capsys.readouterr().err
    assert main(["accept"]) == 2 and "No test run yet" in capsys.readouterr().err


def test_an_old_or_missing_sdk_is_named_with_the_fix(project, monkeypatch, capsys):
    import assay_sdk
    config(project, "true")
    monkeypatch.setattr(assay_sdk, "__version__", "0.1.0")
    assert main(["test"]) == 2 and "pip install -U assay-evals" in capsys.readouterr().err
    monkeypatch.setitem(sys.modules, "assay_sdk", None)  # not installed
    assert main(["test"]) == 2 and "pip install assay-evals" in capsys.readouterr().err


def test_command_after_dashes_overrides_the_config(project, capsys):
    (project / "extract.py").write_text(EXTRACT.replace("i == 19", "False"))
    config(project, "false")
    assert main(["test", "--repeat", "2", "--", sys.executable, "extract.py"]) == 0
    assert "20 cases · 2 attempts each" in capsys.readouterr().out


def test_sdk_fills_in_the_run_and_attempt_under_assay_test(monkeypatch):
    sys.path.insert(0, SDK)
    import assay_sdk
    monkeypatch.setenv("ASSAY_TEST_RUN", "t-1")
    monkeypatch.setenv("ASSAY_TEST_ATTEMPT", "2")
    assert assay_sdk._test("c1") == {"case": "c1", "run": "t-1", "attempt": 2}
    assert assay_sdk._test({"case": "c1", "run": "mine", "attempt": 0}) == {"case": "c1", "run": "mine", "attempt": 0}
    monkeypatch.delenv("ASSAY_TEST_RUN")
    monkeypatch.delenv("ASSAY_TEST_ATTEMPT")
    assert assay_sdk._test("c1") == {"case": "c1", "run": "local"}


PII_AGENT = '''
import assay_sdk as assay
assay.init()
for case in ("c1", "c2"):
    with assay.run("support", test=case) as run:
        run.tool("lookup", {"email": "jo.smith@example.com"}, {"ok": True})
        if case == "c2":
            run.tool("log_event", {"note": "card 4111 1111 1111 1111"}, None)
        run.answer("done")
'''


def test_pii_in_tool_arguments_fails_unless_the_tool_is_allowed_it(project, capsys):
    (project / "agent.py").write_text(PII_AGENT)
    config(project, f"{sys.executable} agent.py", contracts='[pii]\nallow = { lookup = ["email"] }\n')
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "✗ PII         1/2" in out and "c2  PII" in out
    assert "card (411…11) sent to log_event (step 1)" in out and "email" not in out.split("⚠")[1]
    config(project, f"{sys.executable} agent.py", contracts="[pii]\ncheck = false\n")
    assert main(["test"]) == 0 and "PII" not in capsys.readouterr().out
    config(project, "x", contracts='[pii]\nallow = { lookup = ["shoe_size"] }\n')
    assert main(["test"]) == 2 and "[pii] allow.lookup" in capsys.readouterr().err


PYTEST_SUITE = '''
import pytest

@pytest.mark.parametrize("order_id", ["O-17", "O-18"])
def test_refund(assay_case, order_id):
    assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": order_id}}], max_steps=3)
    price = assay_case.call("get_order", lambda order_id: {"O-17": 27.61, "O-18": 12.0}[order_id],
                            order_id=order_id)
    assay_case.answer(f"Refunded ${price:.2f}.")
    assert price == 27.61

def test_without_the_fixture():
    assert True
'''


def test_pytest_plugin_makes_each_test_a_case_and_counts_its_asserts(project, capsys):
    (project / "test_agent.py").write_text(PYTEST_SUITE)
    config(project, f"{sys.executable} -m pytest -q -p no:cacheprovider -p assay_sdk.pytest_plugin test_agent.py")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "2 cases" in out  # the test without the fixture isn't one
    assert "✓ Tool usage    2/2" in out and "✗ Your asserts  1/2" in out
    assert "test_agent.py::test_refund[O-18]  Your asserts" in out and "assert 12.0 == 27.61" in out


def test_upload_sends_the_run_and_has_the_server_check_it(project, capsys, tmp_path_factory):
    from fastapi.testclient import TestClient
    from assay.api import create_app
    from assay.config import Settings
    main(["init"])
    main(["test"])
    capsys.readouterr()
    server = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path_factory.mktemp('srv') / 's.db'}")))

    def http(method, url, body, headers):
        r = server.request(method, url.replace("http://assay.test", ""), json=body, headers=headers)
        return r.status_code, r.json()
    assert local.upload(project, None, "http://assay.test", None, None, http=http) == 0
    assert "tenant 'default'" in capsys.readouterr().out
    runs = server.get("/v1/agents/runs", params={"source": "events:default"}).json()
    assert len(runs) == 1 and runs[0]["trajectories"] == 2 and runs[0]["evaluated"]
    assert local.upload(project, None, "http://assay.test", None, None, http=http) == 0  # again: no doubles
    assert server.get("/v1/agents/runs", params={"source": "events:default"}).json()[0]["trajectories"] == 2
    assert local.upload(project, None, None, None, None, http=http) == 2  # nowhere to send it


def test_a_run_the_command_left_open_is_reported_not_skipped(project, capsys):
    (project / "agent.py").write_text('''
import os
import assay_sdk as assay
assay.init()
with assay.run("t", test="ok") as run:
    run.answer("done")
run = assay.run("t", test="killed").__enter__()
run.tool("lookup", {"id": 1}, {"ok": True})
assay.flush()
os._exit(1)  # killed mid-run: run.end never comes
''')
    config(project, f"{sys.executable} agent.py")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "✗ Finished    1/2" in out and "killed  Finished" in out
    assert "Never finished after step 0: the command exited first." in out


def test_results_judged_on_the_wrong_data_are_listed_not_counted(project, capsys):
    (project / "agent.py").write_text('''
import assay_sdk as assay
assay.init()
for case, q, reply in (("c1", "Refund O-17 please", "Refunded $27.61."), ("c2", "Where is O-18?", "It ships tomorrow.")):
    with assay.run("support", input=q, test=case) as run:
        run.answer(reply)
    graded = q if case == "c2" else reply   # c2: the judge got the question as the "generation"
    run.check("helpful", "fail" if case == "c2" else "pass", evaluator="helpful@1",
              inputs={"query": q, "generation": graded})
''')
    config(project, f"{sys.executable} agent.py")
    assert main(["test"]) == 0  # c2's fail says nothing about the AI
    out = capsys.readouterr().out
    assert "? 1 result judged on data that doesn't match the trace (not counted)" in out
    assert "c2  helpful  helpful@1  (fail)" in out and "the app answered “It ships tomorrow.”" in out
    assert "✓ helpful     1/1" in out
