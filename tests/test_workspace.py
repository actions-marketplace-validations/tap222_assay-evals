"""Setting up where the tests run (assay/workspace.py): no assay.toml, a worktree's dependencies,
the pre-push hook, and assay doctor."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from assay import workspace
from assay.__main__ import main

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")
REPO = str(Path(__file__).resolve().parents[1])


def git(root, *args):
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def jsrepo(tmp_path, monkeypatch):
    """A Jest project with its dependencies installed in the main checkout (node_modules ignored), and
    a second worktree of it with nothing installed."""
    main_ = tmp_path / "app"
    main_.mkdir()
    monkeypatch.delenv("ASSAY_NO_INSTALL", raising=False)
    git(main_, "init", "-q", "-b", "main")
    git(main_, "config", "user.email", "t@example.com")
    git(main_, "config", "user.name", "t")
    (main_ / "package.json").write_text(json.dumps({"devDependencies": {"jest": "^29", "assay-evals": "^0.3"}}),
                                        encoding="utf-8")
    (main_ / "package-lock.json").write_text('{"lockfileVersion": 3}\n', encoding="utf-8")
    (main_ / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
    git(main_, "add", ".")
    git(main_, "commit", "-qm", "app")
    bin_ = main_ / "node_modules" / ".bin"
    bin_.mkdir(parents=True)
    (bin_ / "jest").write_text("#!/bin/sh\n", encoding="utf-8")
    git(main_, "worktree", "add", "-q", "-b", "feature", str(tmp_path / "wt"))
    return main_, tmp_path / "wt"


def test_a_worktree_uses_the_main_checkouts_dependencies_when_the_lockfile_is_the_same(jsrepo):
    main_, wt = jsrepo
    assert workspace.main_worktree(wt).resolve() == main_.resolve() and workspace.main_worktree(main_) is None
    ready, note = workspace.dependencies(wt)
    assert ready and "Using the dependencies installed in" in note
    assert (wt / "node_modules").is_symlink() and (wt / "node_modules" / ".bin" / "jest").exists()
    status = subprocess.run(["git", "status", "--porcelain"], cwd=wt, capture_output=True, text=True).stdout
    assert status == ""  # the link stays out of git
    assert workspace.dependencies(wt) == (True, None)  # and the next run just uses it


def test_a_different_lockfile_installs_rather_than_borrowing(jsrepo, monkeypatch):
    main_, wt = jsrepo
    (wt / "package-lock.json").write_text('{"lockfileVersion": 3, "changed": true}\n', encoding="utf-8")
    monkeypatch.setenv("ASSAY_NO_INSTALL", "1")  # here: say what to run instead of running it
    ready, note = workspace.dependencies(wt)
    assert not ready and "Install the dependencies: npm ci" in note and not (wt / "node_modules").exists()


@pytest.mark.skipif(os.name == "nt", reason="a shell-script stand-in for npm")
def test_a_different_lockfile_is_installed_from_on_its_own(jsrepo, tmp_path, monkeypatch, capsys):
    main_, wt = jsrepo
    (wt / "package-lock.json").write_text('{"lockfileVersion": 3, "changed": true}\n', encoding="utf-8")
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "npm").write_text('#!/bin/sh\necho "$@" > npm-args\nmkdir -p node_modules/.bin && touch node_modules/.bin/jest\n',
                              encoding="utf-8")
    (fake / "npm").chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake}{os.pathsep}{os.environ['PATH']}")
    ready, note = workspace.dependencies(wt)
    assert ready and note == "Installed the dependencies (npm ci --prefer-offline --no-audit --no-fund)."
    assert (wt / "npm-args").read_text().strip() == "ci --prefer-offline --no-audit --no-fund"
    assert "jest isn't installed here (the main checkout's don't fit this lockfile): running npm ci" in capsys.readouterr().err


def test_tracked_node_modules_is_never_swapped_for_a_link(jsrepo):
    main_, wt = jsrepo
    (wt / "node_modules").mkdir()
    (wt / "node_modules" / "left.js").write_text("x", encoding="utf-8")
    git(wt, "add", "-f", "node_modules/left.js")
    git(wt, "commit", "-qm", "oops")
    ready, note = workspace.dependencies(wt)
    assert not ready and "tracked in git" in note and "git rm -r --cached node_modules" in note
    assert not (wt / "node_modules").is_symlink()


def test_assay_test_stops_before_npx_would_download_a_runner(jsrepo, monkeypatch, capsys):
    main_, wt = jsrepo
    (wt / "package-lock.json").write_text('{"changed": 1}\n', encoding="utf-8")
    monkeypatch.setenv("ASSAY_NO_INSTALL", "1")
    monkeypatch.chdir(wt)
    monkeypatch.syspath_prepend(SDK)
    assert main(["test"]) == 2
    err = capsys.readouterr().err
    assert "No assay.toml here: running `npx jest assay`" in err and "Install the dependencies: npm ci" in err


def test_the_command_without_an_assay_toml(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"vitest": "^2"}}), encoding="utf-8")
    assert workspace.detected_command(tmp_path) == "npx vitest run assay"
    (tmp_path / "package.json").write_text(json.dumps({"devDependencies": {"jest": "^29"}}), encoding="utf-8")
    assert workspace.detected_command(tmp_path) == "npx jest assay"
    py = tmp_path / "py"
    py.mkdir()
    (py / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    assert workspace.detected_command(py) == "pytest -q"
    assert workspace.detected_command(tmp_path / "nothing-here") is None


def test_assay_test_runs_pytest_with_no_assay_toml(tmp_path, monkeypatch):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_app.py").write_text("def test_ok(assay_case):\n    assay_case.answer('ok')\n",
                                                    encoding="utf-8")
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([SDK, REPO]), "NO_COLOR": "1"}
    for k in ("ASSAY_URL", "ASSAY_PATH", "ASSAY_TEST_RUN", "ASSAY_POLICY", "ASSAY_BASE_REF"):
        env.pop(k, None)
    out = subprocess.run([sys.executable, "-m", "assay", "test"], cwd=tmp_path, env=env, capture_output=True, text=True)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "No assay.toml here: running `pytest -q`" in out.stderr and "1 passed" in out.stdout


def test_the_hook_is_installed_once_for_every_worktree_and_never_over_another(jsrepo, capsys):
    main_, wt = jsrepo
    ok, msg = workspace.install_hook(wt)
    hook = workspace.hooks_dir(main_) / "pre-push"
    assert ok and msg.startswith("Installed the pre-push hook") and os.access(hook, os.X_OK)
    assert hook.read_text(encoding="utf-8").startswith("#!/bin/sh\n" + workspace.HOOK_MARK)
    assert workspace.install_hook(main_)[1].startswith("Updated")  # the same hook, shared by both worktrees
    hook.write_text("#!/bin/sh\n# Assay, before every push: the first version\n", encoding="utf-8")
    assert workspace.install_hook(main_)[0]  # an earlier version of ours is replaced
    hook.write_text("#!/bin/sh\nnpx lint-staged\n", encoding="utf-8")
    ok, msg = workspace.install_hook(main_)
    assert not ok and "another tool's hook" in msg and "lint-staged" in hook.read_text(encoding="utf-8")


def test_init_hook_from_the_command_line(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    git(tmp_path, "init", "-q")
    assert main(["init", "--hook"]) == 0
    assert "Installed the pre-push hook" in capsys.readouterr().out
    assert (tmp_path / ".git" / "hooks" / "pre-push").exists()


def test_doctor_says_what_to_fix(jsrepo, monkeypatch, capsys):
    main_, wt = jsrepo
    monkeypatch.setenv("ASSAY_URL", "http://127.0.0.1:9")  # nothing listens there
    monkeypatch.setattr(workspace, "_assays", lambda: [])  # no other assay to compare with
    code, text = workspace.doctor(wt)
    assert code == 1
    assert "fix  no pre-push hook: pushes aren't checked\n       → assay init --hook" in text
    assert "ok   no assay.toml: `assay test` runs `npx jest assay`" in text
    assert "links" in text and "node_modules (same lockfile)" in text
    assert "no dashboard at http://127.0.0.1:9 (optional" in text
    workspace.install_hook(wt)
    code, text = workspace.doctor(wt)
    assert code == 0 and "Everything's in place." in text


def test_version(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    from assay import __version__
    assert e.value.code == 0 and capsys.readouterr().out.strip() == f"assay {__version__}"
