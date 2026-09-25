"""pytest plugin: each test that takes the `assay_case` fixture is a test case for Assay.

    def test_refund(assay_case):
        assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": "O-17"}}], answer="27.61")
        reply = my_agent("Refund O-17", run=assay_case)   # record steps on it: run.call, run.answer, ...
        assert "27.61" in reply

The fixture wraps the test in assay.run(<test name>, test=<test id>), and records the test's own
outcome (its asserts) as a check on the field "pytest", so `assay test` counts them. Tests that
don't take the fixture are left alone. Installed with assay-evals; nothing to configure.

With assay-server installed, the test also fails when the run does: after the test body, the run
gets the checks `assay test` makes (the case's expectations, and the contracts and PII rules in
assay.toml), so pytest's own pass/fail is the answer. `[pytest] checks = false` in assay.toml
turns that off. assay_sdk.testing has assertions for the test body: assert_called, ...

`pytest --assay` also compares the session with each test's last passing run, like `assay test`:
Assay's report is in pytest's summary, and the exit code says whether anything got worse. A test
that failed before too doesn't fail the session; one that doesn't take the fixture fails it as
usual. Exit 6 means inconclusive: nothing got worse, but some results couldn't be judged.
"""
from __future__ import annotations

import hashlib
import os

import pytest

import assay_sdk as assay

MAX_CASE = 128  # the event schema's limit on a case id


def case_id(nodeid: str) -> str:
    """The test id, e.g. tests/test_agent.py::test_refund[O-17]; long ids keep a hash of the rest."""
    if len(nodeid) <= MAX_CASE:
        return nodeid
    return nodeid[:MAX_CASE - 9] + "~" + hashlib.sha1(nodeid.encode()).hexdigest()[:8]


def _config(config):
    """assay.toml's settings, read once per session; None without assay-server."""
    if not hasattr(config, "_assay_cfg"):
        try:
            from assay import local
            config._assay_cfg = local.find_config(config.rootpath)
        except ImportError:
            config._assay_cfg = None
    return config._assay_cfg


INCONCLUSIVE = 6  # pytest uses 0-5; 3 would read as its "internal error"


def pytest_addoption(parser):
    g = parser.getgroup("assay")
    g.addoption("--assay", action="store_true",
                help="Compare with each test's last passing run (needs assay-server); the exit code says "
                     "whether anything got worse")
    g.addoption("--assay-baseline", metavar="RUN", help="Compare with this run instead; 'none' for no baseline")
    g.addoption("--assay-upload", action="store_true", help="Also send the run to ASSAY_URL (with ASSAY_KEY)")


_pytest_config = None  # the session's config, for hooks that aren't given it


def pytest_configure(config):
    global _pytest_config
    _pytest_config = config
    config._assay_session = None
    if not config.getoption("assay", False):
        return
    if os.environ.get("ASSAY_TEST_RUN") and not os.environ.get("ASSAY_PYTEST_SESSION"):
        return  # under `assay test`, which compares the run itself
    if hasattr(config, "workerinput"):  # a pytest-xdist worker: records into the session's file
        return
    try:
        from assay import local
    except ImportError:
        raise pytest.UsageError("pytest --assay compares runs with assay-server: pip install assay-server")
    problem = local.sdk_problem()
    if problem:
        raise pytest.UsageError(problem)
    home = local.ensure_home(config.rootpath)
    (home / "runs").mkdir(exist_ok=True)
    run_id = local.new_run_id()
    # Workers and anything the tests start inherit these: one recording for the whole session.
    os.environ.update(ASSAY_PATH=str(home / "runs" / f"{run_id}.jsonl"), ASSAY_TEST_RUN=run_id,
                      ASSAY_PYTEST_SESSION="1")
    os.environ.pop("ASSAY_URL", None)  # record locally; --assay-upload sends it afterwards
    if assay._client is not None:  # init() already ran, e.g. in a conftest: record to the session's file
        assay.init()
    config._assay_session = {"run_id": run_id, "other_failures": 0, "report": None}


def pytest_runtest_logreport(report):
    """A failing test Assay knows nothing about still fails the session. (Called in the main
    process for every test, including those xdist workers ran.)"""
    s = _session(_pytest_config) if _pytest_config is not None else None
    if s and report.failed and not any(k == "assay_case" for k, _ in report.user_properties):
        s["other_failures"] += 1


def _session(config):
    return getattr(config, "_assay_session", None)


@pytest.hookimpl(trylast=True)
def pytest_sessionfinish(session, exitstatus):
    s = _session(session.config)
    if not s:
        return
    from assay import local
    assay.shutdown()  # everything recorded is on disk before it's read
    if not os.path.exists(os.environ["ASSAY_PATH"]):
        s["report"] = "Nothing was recorded: no test took the assay_case fixture."
        return
    cfg = local.find_config(session.config.rootpath)
    code, text = local.finish(session.config.rootpath, cfg, s["run_id"], 1, [],
                              session.config.getoption("assay_baseline"))
    if session.config.getoption("assay_upload") and code != 2:
        from io import StringIO
        from contextlib import redirect_stdout, redirect_stderr
        buf = StringIO()
        with redirect_stdout(buf), redirect_stderr(buf):
            sent = local.upload(session.config.rootpath, s["run_id"], None, None, None)
        text += "\n\n" + buf.getvalue().strip()
        code = code or sent
    if s["other_failures"]:
        n = s["other_failures"]
        text += (f"\n{n} failing test{'s' * (n != 1)} {'don' if n != 1 else 'doesn'}'t take the assay_case fixture: "
                 f"{'they fail' if n != 1 else 'it fails'} the session as usual.")
    s["report"] = text
    if int(exitstatus) not in (0, 1):  # interrupted, usage error, no tests: pytest's own word stands
        return
    if code == 2:  # nothing Assay could check: pytest's result stands
        return
    code = INCONCLUSIVE if code == 3 else code
    session.exitstatus = 1 if s["other_failures"] else code


def pytest_terminal_summary(terminalreporter, config):
    s = _session(config)
    if s and s["report"]:
        terminalreporter.write_sep("=", "assay")
        terminalreporter.write_line(s["report"])


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item):
    out = yield  # a failing test raises here, and fails as it would anyway
    run = getattr(item, "_assay_run", None)
    cfg = _config(item.config) if run is not None else None
    if cfg and cfg["pytest"]["checks"]:
        from assay import local
        problems = local.check_run(run.steps, run.expected, run.answer_text, cfg)
        if problems:
            item._assay_checks_failed = True  # the test's own asserts passed: record them as such
            pytest.fail("The run failed Assay's checks:\n  " + "\n  ".join(problems), pytrace=False)
    return out


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when == "call":
        item._assay_report = report


@pytest.fixture
def assay_case(request):
    if assay._client is None:
        assay.init()
    node = request.node
    node.user_properties.append(("assay_case", case_id(node.nodeid)))
    with assay.run(node.originalname or node.name, test=case_id(node.nodeid),
                   tags={"pytest": node.nodeid[:200]}) as run:
        node._assay_run = run
        yield run
    report = getattr(node, "_assay_report", None)
    if report is None or report.skipped:
        return
    if getattr(node, "_assay_checks_failed", False):  # Assay's checks failed it; they're recorded as themselves
        run.check("pytest", "pass")
        assay.flush()
        return
    reason = None
    if report.failed:
        crash = getattr(report.longrepr, "reprcrash", None)
        reason = (crash.message if crash else str(report.longrepr)).strip()[:2000]
    run.check("pytest", "pass" if report.passed else "fail", reason=reason)
    assay.flush()
