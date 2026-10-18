"""`assay diff` (assay/diff.py): what behavior changed between two versions of an AI app."""
import json
import sys
from pathlib import Path

import pytest

from assay import diff
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")

APP = '''
import os, assay_sdk as assay
V = os.environ["APP_VERSION"]
new = V == "v1.9.0"
attempt = int(os.environ.get("ASSAY_TEST_ATTEMPT", "0"))
assay.init()
ver = {"version": V}

with assay.run("refund", test="refund_flow", version=ver) as r:  # the approval moves after the refund
    if new:
        r.tool("refund", {"id": "O-17"}, {"ok": True}); r.approval("refund", "approved", by="policy")
    else:
        r.approval("refund", "approved", by="policy"); r.tool("refund", {"id": "O-17"}, {"ok": True})
    r.answer("Refunded.")

with assay.run("support", test="support_agent", version=ver) as r:  # a new tool call
    r.expect(calls=[{"tool": "search_order"}])
    r.tool("search_order", {"id": "O-18"}, {"status": "shipped"})
    if new:
        r.tool("cancel_order", {"id": "O-18"}, {"ok": True})
    r.answer("It has shipped.")

for i in range(10):  # a field's accuracy drops
    with assay.run("extract", kind="pipeline", test=f"invoice_{i}", version=ver) as r:
        ok = not (new and i == 7)
        r.check("invoice_number", "pass" if ok else "fail", expected=f"INV-{i}", actual=f"INV-{i}" if ok else "INV-?")

with assay.run("faq", test="faq_answer", version=ver) as r:  # fixed
    r.expect(answer="30 days")
    r.answer("You have 30 days." if new else "You have 14 days.")

with assay.run("orders", test="order_status", version=ver) as r:  # another path, still fine
    r.tool("search_order", {"id": "O-2"}, {"status": "delivered"})
    if new:
        r.tool("lookup_faq", {"q": "delivery"}, {"text": "..."})
    r.answer("Delivered.")

with assay.run("greet", test="greeting", version=ver) as r:  # flaky, the same way in both
    r.expect(answer="Hello")
    r.answer("Hello!" if attempt != 1 else "Hi!")

with assay.run("billing", test="billing", version=ver) as r:
    r.tool("lookup_customer", {"id": 1}, {"ok": True}); r.answer("Done.")
'''


@pytest.fixture
def two_versions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} app.py"\nrepeat = 3\n\n'
                                         '[[contracts]]\nkind = "requires_approval"\nstep = "refund"\n', encoding="utf-8")
    monkeypatch.setenv("APP_VERSION", "v1.8.2")
    main(["test"])
    assert main(["accept"]) == 0
    monkeypatch.setenv("APP_VERSION", "v1.9.0")
    assert main(["test"]) == 1
    return tmp_path


def test_the_diff_between_two_versions(two_versions, capsys):
    capsys.readouterr()
    assert main(["diff", "v1.8.2", "v1.9.0"]) == 1  # something regressed
    out = capsys.readouterr().out
    assert out.startswith("AI BEHAVIOR DIFF\n" + "─" * 32)
    assert "Baseline: v1.8.2 (run " in out and "Current:  v1.9.0 (run " in out and "16 scenarios" in out
    for line in ("✓ 10 unchanged", "↑ 1 improved", "~ 1 changed, still passing", "✗ 3 regressed", "⚠ 1 flaky"):
        assert line in out
    assert ("1. refund_flow\n   Expected: approval(refund) → refund\n   Actual:   refund → approval(refund)\n"
            "   An approval moved\n") in out
    assert "2. support_agent\n   Expected: search_order\n   Actual:   search_order → cancel_order\n   New: cancel_order" in out
    assert out.count("Severity: HIGH") == 2
    assert "4. invoice_number accuracy\n   100% → 90%\n   Severity: MEDIUM" in out
    assert "CHANGED, STILL PASSING\n\n1. order_status" in out
    assert "- greeting: Answer passed 2 of 3 attempts, the way it did before" in out
    assert "IMPROVED\n\nfaq_answer" in out


def test_formats_defaults_and_errors(two_versions, capsys):
    capsys.readouterr()
    main(["diff", "--format", "json"])  # no arguments: the latest run against each case's baseline
    d = json.loads(capsys.readouterr().out)
    assert d["baseline"]["label"] == "each case's last passing run" and d["current"]["label"].startswith("v1.9.0")
    assert d["counts"]["regressed"] == 3 and [e["severity"] for e in d["regressions"]][:2] == ["HIGH", "HIGH"]
    main(["diff", "v1.8.2", "v1.9.0", "--format", "markdown"])
    md = capsys.readouterr().out
    assert "1. `refund_flow` · **HIGH**\n   - Expected: `approval(refund) → refund`" in md
    assert main(["diff", "v0.1"]) == 2 and "No run is 'v0.1'" in capsys.readouterr().err
    assert main(["diff", "v1.9.0", "v1.9.0"]) == 0  # a run against itself: nothing changed


def test_the_pr_comment_shows_the_flow_before_and_after(two_versions):
    md = (two_versions / ".assay" / "summary.md").read_text(encoding="utf-8")
    assert "1 changed, still passing" in md
    assert ("- `support_agent` → Tool usage: Called cancel\\_order(id='O-18'), which the reference doesn't expect\n"
            "  - Expected: `search_order`\n  - Actual: `search_order → cancel_order`") in md


def test_how_a_flow_changed():
    assert diff.flow_change(("a", "b"), ("a", "b")) is None
    ch = diff.flow_change(("approval(refund)", "refund"), ("refund", "approval(refund)"))
    assert ch["approval_moved"] and not ch["added"]
    ch = diff.flow_change(("search", "refund"), ("search", "escalate"))
    assert (ch["added"], ch["removed"], ch["reordered"]) == (["escalate"], ["refund"], False)
    twice = diff.flow_change(("search", "HTTP POST api.x.com/charges"),
                             ("search", "HTTP POST api.x.com/charges", "HTTP POST api.x.com/charges"))
    assert not twice["reordered"] and twice["counts"] == {"HTTP POST api.x.com/charges": [1, 2]}
    assert diff._describe(twice) == "HTTP POST api.x.com/charges: twice instead of once"


def _db(*steps, reads=None):
    out = [{"kind": "tool", "name": n, "args": {"set": st, "empty": em}} for n, st, em in steps]
    if reads is not None:
        out.append({"kind": "tool", "name": "DB reads", "args": reads})
    return {"steps": out}


def test_what_a_run_wrote_to_the_database():
    traj = _db(("DB INSERT bookings", ["amount", "locationId"], ["locationId"]),
               ("DB INSERT bookings", ["amount", "locationId", "note"], []), reads={"services": 3})
    assert diff.database(traj) == {"writes": {"DB INSERT bookings": {"set": ["amount", "locationId", "note"],
                                                                     "empty": [], "placeholder": {}}},
                                   "reads": {"services": 3}, "unwritten": {}}
    assert diff.flow(traj) == ("DB INSERT bookings", "DB INSERT bookings")  # reads aren't steps of the flow


def test_how_the_database_writes_changed():
    before = diff.database(_db(("DB INSERT bookings", ["amount", "locationId", "status"], []), reads={"services": 1}))
    now = diff.database(_db(("DB INSERT bookings", ["amount", "status", "note"], ["amount"]), reads={"services": 12}))
    assert diff.database_change(before, now) == [
        "DB INSERT bookings: no longer sets locationId; now leaves amount empty; now also sets note",
        "reads of services: 12 instead of 1"]
    assert diff.database_change(before, before) == []
    small = diff.database(_db(reads={"services": 3}))  # a few more reads isn't a change
    assert diff.database_change(before, small) == []
    filled = diff.database(_db(("DB INSERT bookings", ["amount", "locationId", "status"], ["locationId"])))
    assert diff.database_change(filled, before) == ["DB INSERT bookings: now fills locationId"]
    ch = diff.change({"flow": ("DB INSERT bookings",), "db": before}, {"flow": ("DB INSERT bookings",), "db": now})
    assert not ch["added"] and diff._describe(ch).startswith("DB INSERT bookings: no longer sets locationId")
    assert diff.change({"flow": ("a",), "db": before}, {"flow": ("a",), "db": before}) is None


def _w(name, set_, empty=(), placeholder=None):
    return {"kind": "tool", "name": name, "args": {"set": list(set_), "empty": list(empty),
                                                   **({"placeholder": placeholder} if placeholder else {})}}


def test_new_writes_are_checked_on_their_own():
    # The booking app's bugs: a branch sent but kept only in metadata, a pin dropped, a timezone saved as
    # UTC, a name saved as "Unknown", an amount left empty.
    now = diff.database({"steps": [
        _w("DB INSERT bookings", ["metadata", "branchId", "amountDue", "note", "cancelledAt", "startsAt", "practitionerId"],
           ["branchId", "amountDue", "note", "cancelledAt", "startsAt", "practitionerId"]),
        _w("DB INSERT locations", ["clinicId", "clinic_id"], ["clinic_id"]),
        {"kind": "tool", "name": "DB INSERT services", "args": {"set": ["name"], "empty": [], "placeholder": {"name": ""},
                                                                "fixture": True}},
        _w("DB INSERT clinics", ["name", "timezone"], placeholder={"timezone": "UTC"}),
        _w("DB UPDATE staff", ["name"], placeholder={"name": "Unknown"}),
        {"kind": "tool", "name": "DB unwritten", "args": {
            "createBooking": {"json": {"branchId": "bookings.metadata"}},
            "createLocation": {"never": ["pin.lat"]}}}]})
    assert diff.flow({"steps": [{"kind": "tool", "name": "DB unwritten", "args": {}}]}) == ()
    found = diff.write_checks(None, now)
    assert [f["text"] for f in found] == [
        "INSERT bookings leaves amountDue empty",
        'INSERT clinics sets timezone to "UTC"',
        "createBooking sent branchId; it reached only bookings.metadata (inside JSON), no column of its own, "
        "and INSERT bookings left branchId empty",
        "INSERT locations leaves clinic_id empty but sets clinicId",
        "createLocation sent pin.lat; no query used it",
        'UPDATE staff sets name to "Unknown"']
    # note, cancelledAt, startsAt and practitionerId can be empty by design; branchId is told once, with
    # what was sent; the service the test inserted itself (a fixture) isn't checked
    assert diff.write_checks(now, now) == []  # what the baseline did too isn't new
    only_amount = diff.database({"steps": [_w("DB INSERT bookings", ["amountDue"], ["amountDue"])]})
    assert [f["field"] for f in diff.write_checks(only_amount, now)] == ["timezone", "branchId", "clinic_id",
                                                                         "pin.lat", "name"]


def test_new_write_checks_are_ranked_and_cut_to_three():
    now = {"a": {"db": diff.database({"steps": [_w("DB INSERT t", ["location_id", "locationId", "price", "tz", "title"],
                                                   ["location_id", "price"], {"title": "N/A", "tz": "UTC"})]})}}
    found = diff.all_write_checks({}, {**now, "b": now["a"], "c": {"db": {}}})
    assert [f["field"] for f in found] == ["price", "tz", "location_id", "title"]  # money, time, id, the rest
    assert found[0]["cases"] == ["a", "b"]  # one finding in two tests is one item
    lines = diff.write_checks_lines(found, lambda c: c)
    assert lines[0] == "INSERT t leaves price empty (a, and 1 more test)" and len(lines) == 4
    assert lines[-1].startswith("(+1 more")


# ---------- on the server ----------

@pytest.fixture
def server(tmp_path):
    from fastapi.testclient import TestClient
    from assay.api import create_app
    from assay.config import Settings
    client = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'd.db'}")))
    client.post("/v1/contracts", json={"source": "events:t", "kind": "requires_approval", "step": "refund",
                                       "severity": "critical"})
    return client


def _version(client, run, version, new):
    n = iter(range(10 ** 6))
    ts = f"2026-09-25T1{int(new)}:00:00Z"  # the new version ran later
    ev = lambda rid, **k: {"v": 1, "id": f"{run}-{next(n)}", "ts": ts, "run_id": rid, **k}
    events = [{"v": 1, "id": f"{run}-x", "ts": "2026-09-25T10:00:00Z", "type": "expect", "case": "support_agent",
               "calls": [{"tool": "search_order"}]}]
    for case, steps in (
            ("refund_flow", [("tool", "refund"), ("approval", "refund")] if new else
             [("approval", "refund"), ("tool", "refund")]),
            ("support_agent", [("tool", "search_order")] + ([("tool", "cancel_order")] if new else [])),
            ("billing", [("tool", "lookup_customer")])):
        rid = f"{run}.{case}"
        events.append(ev(rid, type="run.start", test={"run": run, "case": case, "attempt": 0},
                         version={"version": version}))
        for i, (kind, name) in enumerate(steps):
            events.append(ev(rid, type="step", seq=i, kind=kind, name=name,
                             **({"decision": "approved"} if kind == "approval" else {"args": {}})))
        events += [ev(rid, type="step", seq=len(steps), kind="answer", text="Done."), ev(rid, type="run.end")]
    r = client.post("/v1/ingest", json=events, headers={"X-Tenant": "t"})
    assert r.status_code == 200, r.text


def test_the_diff_on_the_server(server):
    _version(server, "nightly-1", "v1.8.2", new=False)
    _version(server, "nightly-2", "v1.9.0", new=True)
    d = server.get("/v1/evals/runs/v1.9.0/diff", params={"source": "events:t", "baseline": "v1.8.2"}).json()
    assert d["baseline"]["label"] == "v1.8.2 (run nightly-1)" and d["current"]["label"] == "v1.9.0 (run nightly-2)"
    assert (d["scenarios"], d["counts"]["regressed"], d["counts"]["unchanged"]) == (3, 2, 1)
    refund, support = d["regressions"]
    assert refund["name"] == "refund_flow" and refund["flow_change"]["approval_moved"] and refund["severity"] == "HIGH"
    assert support["actual"] == ["search_order", "cancel_order"] and support["flow_change"]["added"] == ["cancel_order"]
    # The run before is the default baseline; markdown for a PR, and a version nobody recorded is a 404.
    assert server.get("/v1/evals/runs/nightly-2/diff", params={"source": "events:t"}).json()["baseline"]["run_id"] == \
        "nightly-1"
    md = server.get("/v1/evals/runs/nightly-2/diff", params={"source": "events:t", "format": "markdown"}).text
    assert "1. `refund_flow` · **HIGH**" in md
    assert server.get("/v1/evals/runs/v9/diff", params={"source": "events:t"}).status_code == 404
    assert server.get("/v1/evals/runs/nightly-1/diff", params={"source": "events:t"}).status_code == 404  # the first
