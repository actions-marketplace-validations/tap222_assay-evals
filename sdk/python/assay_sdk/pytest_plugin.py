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
"""
from __future__ import annotations

import hashlib

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
