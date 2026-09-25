"""pytest plugin: each test that takes the `assay_case` fixture is a test case for Assay.

    def test_refund(assay_case):
        assay_case.expect(calls=[{"tool": "get_order", "args": {"order_id": "O-17"}}], answer="27.61")
        reply = my_agent("Refund O-17", run=assay_case)   # record steps on it: run.call, run.answer, ...
        assert "27.61" in reply

The fixture wraps the test in assay.run(<test name>, test=<test id>), and records the test's own
outcome (its asserts) as a check on the field "pytest", so `assay test` counts them. Tests that
don't take the fixture are left alone. Installed with assay-evals; nothing to configure.
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
        yield run
    report = getattr(node, "_assay_report", None)
    if report is None or report.skipped:
        return
    reason = None
    if report.failed:
        crash = getattr(report.longrepr, "reprcrash", None)
        reason = (crash.message if crash else str(report.longrepr)).strip()[:2000]
    run.check("pytest", "pass" if report.passed else "fail", reason=reason)
    assay.flush()
