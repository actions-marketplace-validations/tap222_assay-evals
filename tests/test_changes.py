"""`assay changes`: changed lines no test ran, and deploy or dependency changes no CI job checked."""
import json
import subprocess

import pytest

from assay import changes
from assay.__main__ import main


def test_a_diff_gives_the_lines_each_file_gained():
    diff = """diff --git a/app.py b/app.py
--- a/app.py
+++ b/app.py
@@ -3,0 +4,2 @@ def pay():
+    charge(card)
+    log()
@@ -10 +12 @@ def refund():
-    old()
+    new()
--- a/gone.py
+++ /dev/null
@@ -1,2 +0,0 @@
-x
-y
"""
    assert changes.parse_diff(diff) == {"app.py": {4, 5, 12}}


# ---------- reading coverage ----------

def test_coverage_is_read_from_coverage_py_istanbul_and_lcov(tmp_path):
    py = {"files": {"app.py": {"executed_lines": [1, 2, 4], "missing_lines": [5, 6]}}}
    assert vars(changes._coverage_py_json(tmp_path, py)["app.py"]) == {"measured": {1, 2, 4, 5, 6}, "ran": {1, 2, 4},
                                                                     "functions": []}
    js = {str(tmp_path / "src" / "pay.ts"): {
        "path": str(tmp_path / "src" / "pay.ts"),
        "statementMap": {"0": {"start": {"line": 2}, "end": {"line": 3}}, "1": {"start": {"line": 6}, "end": {"line": 6}}},
        "s": {"0": 4, "1": 0},
        "fnMap": {"0": {"name": "refund", "loc": {"start": {"line": 5}, "end": {"line": 7}}}}}}
    cov = changes._istanbul(tmp_path, js)["src/pay.ts"]
    assert (cov.measured, cov.ran, cov.functions) == ({2, 3, 6}, {2, 3}, [(5, 7, "refund")])
    lcov = "TN:\nSF:lib/send.go\nFN:3,Send\nDA:4,1\nDA:5,0\nend_of_record\nSF:/elsewhere/x.go\nDA:1,0\nend_of_record\n"
    got = changes._lcov(tmp_path, lcov)
    assert list(got) == ["lib/send.go"]  # a file outside the repository is left out
    assert (got["lib/send.go"].ran, got["lib/send.go"].functions) == ({4}, [(3, 0, "Send")])
    assert changes._rel(tmp_path, "../outside.go") is None
    assert changes._rel(tmp_path, "/elsewhere/x.go") is None


def test_go_coverage_profiles_are_read_by_module_path(tmp_path):
    (tmp_path / "go.mod").write_text("module github.com/acme/shop\n\ngo 1.22\n", encoding="utf-8")
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "go.mod").write_text("module github.com/acme/shop/tools\n", encoding="utf-8")
    profile = tmp_path / "coverage.out"
    profile.write_text("mode: set\n"
                       "github.com/acme/shop/pay/refund.go:10.2,12.16 2 1\n"
                       "github.com/acme/shop/pay/refund.go:14.2,15.10 1 0\n"
                       "github.com/acme/shop/tools/gen/main.go:3.13,4.2 1 1\n"
                       "example.com/elsewhere/x.go:1.1,2.2 1 1\n", encoding="utf-8")
    assert changes.find_coverage(tmp_path) == profile
    got = changes.read_coverage(tmp_path, profile)
    assert set(got) == {"pay/refund.go", "tools/gen/main.go"}  # the nested module by its own go.mod
    assert (got["pay/refund.go"].measured, got["pay/refund.go"].ran) == ({10, 11, 12, 14, 15}, {10, 11, 12})


# ---------- changed lines no test ran ----------

APP = '''import requests


def total(items):
    return sum(items)


def upload(run):
    body = run.to_json()
    requests.post(URL, data=body)
    return True


class Billing:
    def refund(self, order):
        # give it back
        stripe.Refund.create(amount=order.amount)
'''


def test_unrun_lines_are_grouped_by_function_and_ranked_by_risk(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "test_app.py").write_text("def test_total():\n    assert total([1]) == 1\n", encoding="utf-8")
    (tmp_path / "helpers.py").write_text("def helper():\n    return 1\n", encoding="utf-8")
    (tmp_path / "notes.md").write_text("hi\n", encoding="utf-8")
    cov = {"app.py": changes.FileCoverage(measured={1, 4, 5, 8, 9, 10, 11, 14, 15, 17},
                                          ran={1, 4, 5, 8, 9, 14, 15})}
    changed = {"app.py": {5, 9, 10, 11, 16, 17}, "test_app.py": {2}, "helpers.py": {1, 2}, "notes.md": {1}}
    u = changes.untested(tmp_path, changed, cov)
    assert [(x["file"], x["function"], x["spans"], x["risk"]) for x in u["items"]] == [
        ("app.py", "Billing.refund", "17", "money"),  # the comment on line 16 isn't counted
        ("app.py", "upload", "10-11", "outside calls"),
        ("helpers.py", None, "1-2", "")]  # Python coverage, and no test loaded it
    assert u["items"][2]["unloaded"] and (u["ran"], u["measured"]) == (2, 7)
    assert changes._item(u["items"][0]) == "app.py: Billing.refund, line 17 (money)"
    assert changes._item(u["items"][2]) == "helpers.py: no test loaded this file, lines 1-2"


def test_a_file_none_of_whose_lines_ran_is_one_item(tmp_path):
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    cov = {"app.py": changes.FileCoverage(measured={1, 4, 5, 8, 9, 10, 11, 14, 15, 17}, ran=set())}
    items = changes.untested(tmp_path, {"app.py": {5, 10, 17}}, cov)["items"]
    assert [(x["function"], x["spans"], x["risk"], x.get("unloaded")) for x in items] == \
        [(None, "3 lines in 5-17", "money", True)]


def test_a_js_line_is_named_after_the_declaration_above_it_when_coverage_names_none():
    source = ["export async function sendReceipt(order) {", "  await mailer.send(order.email);", "}",
              "const refund = async (id) => {", "  return api.post(`/refunds/${id}`);", "};"]
    assert changes.function_of(2, [], source) == "sendReceipt"
    assert changes.function_of(5, [], source) == "refund"
    assert changes.function_of(5, [], source, scan=False) is None


@pytest.mark.parametrize("line, kind", [
    ("stripe.Refund.create(amount=5)", "money"),
    ("send_email(to)", "messages"),
    ("if not verify_password(p):", "logins"),
    ("await this.repo.save(booking)", "database writes"),
    ('cur.execute("INSERT INTO t VALUES (1)")', "database writes"),
    ("requests.post(url)", "outside calls"),
    ("self.client.post('/v1/ingest')", "outside calls"),
    ("out[path].update(more)", ""),  # a dict, not a database
    ("value = cache.get(key)", ""),
    ("tokens_in = usage.input_tokens", ""),  # a model's tokens, not a login
])
def test_what_a_line_does_names_its_risk(line, kind):
    assert changes.risk([line]) == kind


# ---------- deploy and dependency changes ----------

ON_PR = """name: tests
on: [push, pull_request]
jobs:
  test:
    steps:
      - uses: ./
      - run: |
          pip install -r requirements-dev.txt
          pip install -e sdk/python -e ".[dev]"
      - run: npm ci
        working-directory: sdk/js
"""
RELEASE = """name: release
on:
  push:
    tags: ["v*"]
jobs:
  publish:
    steps:
      - run: python -m build
"""
ACTION_YML = """name: x
inputs:
  token:
    description: a token
runs:
  using: composite
  steps:
    - run: assay test
"""


def _check(changed, files, lines=None):
    return {x["file"]: x["why"] for x in changes.unchecked(changed, files.get, list(files), lines)}


def test_a_deploy_or_dependency_change_is_listed_only_when_no_pull_request_job_used_it():
    files = {".github/workflows/tests.yml": ON_PR, ".github/workflows/release.yml": RELEASE, "action.yml": ACTION_YML,
             "requirements-dev.txt": "pytest\n", "requirements.txt": "fastapi\n", "pyproject.toml": "[project]\n",
             "sdk/python/pyproject.toml": "[project]\n", "sdk/js/package.json": "{}\n", "web/package.json": "{}\n",
             "Dockerfile": "FROM python\n", "vercel.json": "{}\n", ".vercelignore": "x\n",
             "examples/demo/package.json": "{}\n", "app.py": "x = 1\n"}
    got = _check(list(files), files)
    assert got == {
        ".github/workflows/release.yml": "doesn't run on pull requests, only on push, tags: its first run is after "
                                         "the merge",
        "requirements.txt": "no job on pull requests installs or builds from it",
        "web/package.json": "no job on pull requests installs or runs from it",
        "Dockerfile": "no job on pull requests builds the image",
        "vercel.json": "no job on pull requests deploys with it (a preview deploy outside CI isn't seen here)",
        ".vercelignore": "no job on pull requests deploys with it (a preview deploy outside CI isn't seen here)"}
    # tests.yml runs on the PR; requirements-dev, both pyprojects and sdk/js are installed there; the
    # action is used (uses: ./); examples aren't deployed; app.py isn't a deploy file


def test_a_version_bump_or_an_actions_inputs_alone_change_nothing_a_build_reads():
    files = {"web/package.json": '{\n  "name": "web",\n  "version": "1.2.0",\n  "dependencies": {"x": "^2"}\n}\n',
             "action.yml": ACTION_YML, "pyproject.toml": '[project]\nversion = "0.7.0"\ndependencies = ["httpx<1"]\n'}
    assert _check(list(files), files, {"web/package.json": {3}, "action.yml": {4}, "pyproject.toml": {2}}) == {}
    assert set(_check(list(files), files, {"web/package.json": {4}, "action.yml": {8}, "pyproject.toml": {3}})) == \
        {"web/package.json", "action.yml", "pyproject.toml"}


# ---------- the command, in git ----------

def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def test_the_command_reports_against_the_base_branch(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("ASSAY_BASE_REF", raising=False)
    git(tmp_path, "init", "-q", "-b", "main")
    git(tmp_path, "config", "user.email", "t@example.com")
    git(tmp_path, "config", "user.name", "t")
    (tmp_path / "app.py").write_text("def total(items):\n    return sum(items)\n", encoding="utf-8")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-q", "-m", "base")
    git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / "app.py").write_text(APP, encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")  # new, not committed
    (tmp_path / "coverage.json").write_text(json.dumps({"files": {"app.py": {
        "executed_lines": [1, 4, 5, 8, 14, 15], "missing_lines": [9, 10, 11, 17]}}}), encoding="utf-8")
    assert main(["changes", "--base", "main"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[:4] == ["Check 3 things before merging",
                       "  1. not run by any test: app.py: Billing.refund, line 17 (money)",
                       "  2. not run by any test: app.py: upload, lines 9-11 (outside calls)",
                       "  3. requirements.txt: no job on pull requests installs or builds from it"]
    assert out[-1] == "(against main, 3 changed files; 4 of 8 changed lines ran in a test)"
    assert not (tmp_path / ".assay").exists()  # nothing written in a repository that doesn't use Assay
    assert main(["changes", "--base", "main", "--format", "json"]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["changes"]["untested"]["ran"] == 4 and [x["file"] for x in r["changes"]["deploy"]] == ["requirements.txt"]
    assert main(["changes", "--base", "main", "--format", "markdown"]) == 0
    md = capsys.readouterr().out
    assert md.startswith("### Check 3 things before merging\n\n1. not run by any test:")
    assert "4 of 8 changed lines ran in a test" in md
    assert main(["changes", "--base", "nope"]) == 2


# ---------- the change report: one verdict, three items ----------

RUN = {"regressions": [{"name": "refund_flow", "severity": "HIGH", "reasons": ["refund ran before its approval"],
                        "setup": ["prompt refund@3 → refund@4"]}],
       "new_failures": [], "flaky": [{"name": "greets"}, {"name": "books"}],
       "changed": [{"name": "search", "flow_change": {"text": "new: HTTP GET maps"}}],
       "write_checks": [{"kind": "placeholder", "write": "INSERT clinics", "field": "timezone",
                         "text": "INSERT clinics sets timezone to \"UTC\"", "cases": ["t.py::creates"],
                         "names": ["creates"]}]}
LOOSE = {"file": "tests/test_pay.py", "line": 4, "test": "test_total", "kind": "loosened",
         "before": "== 1200", "after": "> 0"}
NOTE = {**LOOSE, "kind": "rewritten", "before": "== 1200", "after": "== 1350"}
CHANGES = {"base": "main", "files": 5, "deploy": [{"file": "requirements.txt", "why": "no job installs it"}],
           "untested": {"ran": 3, "measured": 9, "items": [
               {"file": "app.py", "function": "refund", "lines": [9], "spans": "9", "risk": "money"},
               {"file": "app.py", "function": "fmt", "lines": [20], "spans": "20", "risk": ""}]}}


def test_the_report_ranks_what_broke_first_then_what_looks_wrong_then_what_nothing_checked():
    found = changes.items(CHANGES, RUN, [LOOSE, NOTE])
    assert [x["kind"] for x in found] == ["regressed", "weakened", "new write", "untested, risky", "deploy",
                                         "changed", "untested", "expected value"]
    assert found[0]["text"] == "refund_flow: refund ran before its approval"
    assert found[2]["text"] == 'INSERT clinics sets timezone to "UTC" (creates)'
    assert found[0]["detail"] == "HIGH · changed: prompt refund@3 → refund@4"  # the likely cause, next to it
    assert changes.verdict(found) == "Not safe to merge: 2 things broke or stopped being checked"
    assert changes.verdict(found[2:]) == "Check 3 things before merging"
    assert changes.verdict(found[-1:]) == "Check 1 thing before merging"
    assert changes.verdict([]).startswith("Nothing to check")


def test_the_report_shows_three_items_and_counts_the_rest(monkeypatch, tmp_path):
    from assay import weakening
    monkeypatch.setattr(weakening, "scan", lambda root, ref: [LOOSE, NOTE])
    r = changes.report(tmp_path, CHANGES)
    assert r["run"] is None and r["rest"] == {"untested": 1, "expected value": 1}  # no test results here
    assert changes.report_text(r).splitlines()[:2] == [
        "NOT SAFE TO MERGE: 1 THING BROKE OR STOPPED BEING CHECKED",
        "  1. loosens an assertion in test_total (tests/test_pay.py:4): == 1200 → > 0"]
    r["items"] = changes.items(CHANGES, RUN, [LOOSE, NOTE])
    r["verdict"], r["flaky"] = changes.verdict(r["items"]), 2
    r["rest"] = {"new write": 1, "untested, risky": 1, "deploy": 1, "changed": 1, "untested": 1, "expected value": 1}
    text = changes.report_text(r).splitlines()
    assert text[0] == "NOT SAFE TO MERGE: 2 THINGS BROKE OR STOPPED BEING CHECKED"
    assert text[1:3] == ["  1. refund_flow: refund ran before its approval (HIGH · changed: prompt refund@3 → refund@4)",
                         "  2. loosens an assertion in test_total (tests/test_pay.py:4): == 1200 → > 0"]
    assert text[4] == ("Also: 1 more new write to check, 2 more untested changes, 1 more unchecked deploy change, "
                       "1 more behavior change, still passing, 1 more rewritten expected value; 2 flaky, as before. "
                       "`assay changes --all` lists everything.")
    every = changes.report_text(r, limit=10 ** 9).splitlines()  # --all: every item, and only the flaky count
    assert len(every) == 11 and every[9] == "Also: 2 flaky, as before."


def test_one_change_in_many_cases_is_one_item():
    run = {"regressions": [{"name": n, "severity": "HIGH", "reasons": ["refund ran before its approval"]}
                           for n in ("refund_a", "refund_b", "refund_c")],
           "changed": [{"name": "search", "flow_change": {"text": "new: HTTP GET maps"}}]}
    found = changes.items({"untested": None, "deploy": []}, run, [])
    assert [x["text"] for x in found] == ["refund ran before its approval (refund_a, and 2 more cases)",
                                         "search: new: HTTP GET maps"]


def test_an_item_marked_intended_is_left_out_until_it_changes(tmp_path, monkeypatch, capsys):
    from assay import weakening
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(weakening, "scan", lambda root, ref: [])
    monkeypatch.setattr(changes, "compute", lambda root, ref, cov: json.loads(json.dumps(CHANGES)))
    monkeypatch.setattr(weakening, "base_ref", lambda root, given=None: "main")
    assert main(["changes", "--base", "main", "--intended", "2"]) == 2  # a reason is needed
    assert "--reason" in capsys.readouterr().err
    assert main(["changes", "--base", "main", "--intended", "2", "--reason", "an old pin, checked by hand"]) == 0
    assert "Marked intended in assay.intended.toml: requirements.txt: no job installs it" in capsys.readouterr().out
    saved = changes.load_intended(tmp_path)
    assert saved[0]["key"] == "deploy:requirements.txt:" and saved[0]["reason"] == "an old pin, checked by hand"
    assert main(["changes", "--base", "main"]) == 0
    out = capsys.readouterr().out
    assert "requirements.txt" not in out.splitlines()[1] and "1 marked intended in assay.intended.toml" in out
    changed = json.loads(json.dumps(CHANGES))
    changed["deploy"][0]["digest"] = "different"  # the file changed again: news again
    monkeypatch.setattr(changes, "compute", lambda root, ref, cov: changed)
    assert main(["changes", "--base", "main", "--all"]) == 0
    assert "requirements.txt: no job installs it" in capsys.readouterr().out
    assert main(["changes", "--base", "main", "--intended", "9", "--reason", "x"]) == 2
