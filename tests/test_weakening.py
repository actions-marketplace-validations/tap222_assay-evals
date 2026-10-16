"""Weakened tests (assay/weakening.py): assertions a change removed or loosened, against the base branch."""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from assay import weakening
from assay.__main__ import main
from assay.weakening import compare, js_tests, py_tests

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")
REPO = str(Path(__file__).resolve().parents[1])


def kinds(found):
    return sorted((f["test"], f["kind"]) for f in found)


# ---------- pytest ----------

PY_BEFORE = '''
import pytest

def test_total():
    t = total([1, 2])
    assert t == 1200
    assert t.currency == "INR"

def test_rate():
    assert rate() == pytest.approx(0.25, rel=1e-3)

def test_price():
    assert price("cleaning") == 1200

def test_format():
    assert fmt(1) == "1"

class TestRefund:
    def test_amount(self):
        self.assertEqual(refund(), 500)
'''


def test_a_loosened_or_removed_python_assertion_is_found():
    after = '''
import pytest

def test_total():
    t = total([1,2])
    assert t > 0

def test_rate():
    assert rate() == pytest.approx(0.25, rel=0.5)

def test_price():
    assert price("cleaning") == 1350

def test_format():
    assert fmt(1)  ==  "1"   # only reformatted

class TestRefund:
    def test_amount(self):
        self.assertTrue(refund())
'''
    found = compare(py_tests(PY_BEFORE), py_tests(after))
    assert kinds(found) == [("TestRefund.test_amount", "loosened"), ("test_price", "rewritten"),
                            ("test_rate", "loosened"), ("test_total", "loosened"), ("test_total", "removed")]
    total = next(f for f in found if f["test"] == "test_total" and f["kind"] == "loosened")
    assert total["before"] == "assert t == 1200" and total["after"] == "assert t > 0" and total["line"] == 6
    removed = next(f for f in found if f["kind"] == "removed")
    assert "assert t.currency == 'INR'" in removed["before"] and removed["after"] == "2 → 1 assertions"


def test_reformatting_tightening_or_renaming_a_python_test_is_not_weakening():
    after = PY_BEFORE.replace("assert t == 1200", "assert t == 1200, 'the total'") \
        .replace("rel=1e-3", "rel=1e-4") \
        .replace("def test_format", "def test_formats")  # renamed: not compared, never a false alarm
    assert compare(py_tests(PY_BEFORE), py_tests(after)) == []


def test_a_file_that_doesnt_parse_is_skipped_not_guessed():
    assert py_tests("def test_x(:\n") is None


# ---------- Jest and Vitest ----------

JS_BEFORE = '''
describe("cart", () => {
  it("totals the cart", () => {
    const t = total(items);
    expect(t).toBe(1200);
    // expect(t).toBe(1) is a comment, not a check
    expect(send).toHaveBeenCalledWith("123", "Booked");
  });
  it("formats", () => { expect(fmt(1)).toEqual("1"); });
  it("prices", () => { expect(price("a")).toBe(500); });
  it("rounds", () => { expect(rate()).toBeCloseTo(0.25, 3); });
  it("never charges twice", () => { expect(charge).not.toHaveBeenCalled(); });
  it("returns the booking", () => { expect(book()).toEqual({ id: 7 }); });
});
'''


def test_a_loosened_or_removed_js_assertion_is_found():
    after = '''
describe("cart", () => {
  it("totals the cart", () => {
    const t = total(items);
    expect(t).toBeGreaterThan(0);
    expect(send).toHaveBeenCalled();
  });
  it("formats", () => {
    expect(fmt(1)).toStrictEqual("1");
  });
  it("prices", () => { expect(price("a")).toBe(650); });
  it("rounds", () => { expect(rate()).toBeCloseTo(0.25, 1); });
  it("never charges twice", () => { expect(charge).toHaveBeenCalled(); });
  it("returns the booking", () => { expect(book()).toEqual(expect.anything()); });
});
'''
    found = compare(js_tests(JS_BEFORE), js_tests(after))
    assert kinds(found) == [("never charges twice", "loosened"), ("prices", "rewritten"),
                            ("returns the booking", "loosened"), ("rounds", "loosened"),
                            ("totals the cart", "loosened"), ("totals the cart", "loosened")]
    first = next(f for f in found if f["test"] == "totals the cart")
    assert first["before"] == "expect(t).toBe(1200)" and first["after"] == "expect(t).toBeGreaterThan(0)"
    assert first["line"] == 5


def test_a_removed_js_assertion_and_a_skipped_neighbour_dont_mix():
    after = JS_BEFORE.replace('    expect(send).toHaveBeenCalledWith("123", "Booked");\n', "") \
        .replace('it("formats"', 'it.skip("formats"')
    found = compare(js_tests(JS_BEFORE), js_tests(after))
    assert kinds(found) == [("totals the cart", "removed")]
    assert 'toHaveBeenCalledWith("123", "Booked")' in found[0]["before"]


def test_unchanged_js_tests_and_equivalent_matchers_are_not_weakening():
    assert compare(js_tests(JS_BEFORE), js_tests(JS_BEFORE)) == []
    same = JS_BEFORE.replace('toEqual("1")', 'toStrictEqual( "1" )')
    assert compare(js_tests(JS_BEFORE), js_tests(same)) == []
    # A formatter switching quote style changes no expected value (seen in a real project's history).
    requoted = JS_BEFORE.replace('"123", "Booked"', "'123', 'Booked'").replace('toBe(1200)', "toBe(1200 )")
    assert compare(js_tests(JS_BEFORE), js_tests(requoted)) == []


# ---------- against the base branch, in git ----------

def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ASSAY_BASE_REF", raising=False)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "checkout.ts").write_text("export const total = () => 1200;\n", encoding="utf-8")
    (tmp_path / "src" / "checkout.test.ts").write_text(
        'import { total } from "./checkout";\nit("totals", () => { expect(total()).toBe(1200); });\n', encoding="utf-8")
    (tmp_path / "test_api.py").write_text("def test_ok():\n    assert status() == 200\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    return tmp_path


def test_scan_finds_a_test_weakened_alongside_the_code_it_checks(repo):
    (repo / "src" / "checkout.ts").write_text("export const total = () => 1350;\n", encoding="utf-8")
    (repo / "src" / "checkout.test.ts").write_text(
        'import { total } from "./checkout";\nit("totals", () => { expect(total()).toBeDefined(); });\n', encoding="utf-8")
    git(repo, "commit", "-qam", "make it pass")
    (repo / "test_api.py").write_text("def test_ok():\n    assert status()\n", encoding="utf-8")  # not committed yet
    (repo / "test_new.py").write_text("def test_new():\n    assert 1\n", encoding="utf-8")  # a new file can't weaken
    found = weakening.scan(repo, "main")
    assert [(f["file"], f["kind"]) for f in found] == [("src/checkout.test.ts", "loosened"), ("test_api.py", "loosened")]
    assert found[0]["related"] == ["src/checkout.ts"]
    assert weakening.describe(found[0]) == ("loosens an assertion in totals (src/checkout.test.ts:2): "
                                            "expect(total()).toBe(1200) → expect(total()).toBeDefined(), "
                                            "while src/checkout.ts changed")
    assert weakening.scan(repo, None) == []  # nothing to compare with


def test_assay_guard_exit_codes(repo, capsys):
    assert main(["guard", "--base", "main"]) == 0 and "No test was weakened." in capsys.readouterr().out
    assert main(["guard", "--base", "nowhere"]) == 2
    (repo / "test_api.py").write_text("def test_ok():\n    assert status() == 201\n", encoding="utf-8")
    assert main(["guard", "--base", "main"]) == 0  # a changed value is listed, not a failure
    assert "~ changes an expected value in test_ok (test_api.py:2)" in capsys.readouterr().out
    (repo / "test_api.py").write_text("def test_ok():\n    assert status() >= 200\n", encoding="utf-8")
    assert main(["guard"]) == 1  # compared with the default branch when none is given
    out = capsys.readouterr().out
    assert "1 assertion change weakens the tests:" in out
    assert "- loosens an assertion in test_ok (test_api.py:2): assert status() == 200 → assert status() >= 200" in out


# ---------- in a pull request, end to end ----------

SUITE = '''
def status():
    return 200

def test_status(assay_case):
    assay_case.answer("ok")
    assert status() == 200
'''


def test_a_pr_that_weakens_a_test_fails_until_a_maintainer_accepts_it(repo):
    (repo / "tests").mkdir()
    (repo / "tests" / "test_app.py").write_text(SUITE, encoding="utf-8")
    (repo / "assay.toml").write_text('[test]\ncommand = "pytest -q tests"\n', encoding="utf-8")
    (repo / "base.toml").write_text('[test]\ncommand = "pytest -q tests"\n', encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "suite")
    git(repo, "branch", "-f", "main")  # the suite is on the base branch
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([SDK, REPO]), "NO_COLOR": "1", "ASSAY_BASE_REF": "main"}
    for k in ("ASSAY_URL", "ASSAY_PATH", "ASSAY_TEST_RUN", "ASSAY_POLICY", "ASSAY_POLICY_CHANGE"):
        env.pop(k, None)
    run = lambda **extra: subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--assay", "tests"],
                                         capture_output=True, text=True, cwd=repo, env={**env, **extra},
                                         encoding="utf-8", errors="replace")
    assert run().returncode == 0

    (repo / "tests" / "test_app.py").write_text(SUITE.replace("assert status() == 200", "assert status()"), encoding="utf-8")
    pr = {"ASSAY_POLICY": str(repo / "base.toml")}
    out = run(**pr)
    assert out.returncode == 1, out.stdout  # every test passes; the run doesn't
    assert ("- loosens an assertion in test_status (tests/test_app.py:7): assert status() == 200 → assert status()"
            in out.stdout)
    md = (repo / ".assay" / "summary.md").read_text(encoding="utf-8")
    assert "## Checks weakened" in md and "- loosens: loosens an assertion in test\\_status" in md
    accepted = run(**pr, ASSAY_POLICY_CHANGE="accepted")
    assert accepted.returncode == 0 and "the change is accepted" in accepted.stdout

    # Outside a pull request it's said, not failed.
    local = run()
    assert local.returncode == 0 and "Weakened against main: loosens an assertion in test_status" in local.stdout


def test_with_no_assay_tests_a_pr_is_still_checked_for_weakened_tests(repo, monkeypatch, capsys):
    monkeypatch.syspath_prepend(SDK)
    for k in ("ASSAY_POLICY_CHANGE", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    (repo / "test_api.py").write_text("def test_ok():\n    assert status()\n", encoding="utf-8")
    assert main(["test", "--", sys.executable, "-c", "pass"]) == 2  # nothing ran, nothing to compare with
    assert main(["test", "--base", "main", "--", sys.executable, "-c", "pass"]) == 1
    out = capsys.readouterr().out
    assert "1 assertion change weakens the tests:" in out
    md = (repo / ".assay" / "summary.md").read_text(encoding="utf-8")
    assert "## Checks weakened: this PR loosens the tests that judge it" in md
    assert "- loosens: loosens an assertion in test\\_ok (test\\_api.py:2)" in md
    monkeypatch.setenv("ASSAY_POLICY_CHANGE", "accepted")
    assert main(["test", "--base", "main", "--", sys.executable, "-c", "pass"]) == 0
    assert "## Tests weakened, and accepted" in (repo / ".assay" / "summary.md").read_text(encoding="utf-8")
