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
