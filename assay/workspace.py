"""Setting up where the tests run, so nobody has to: the command without an assay.toml, the
dependencies of a git worktree, the pre-push hook, and `assay doctor`.

A check that silently didn't run is worse than none: the push goes out looking checked. So
each of these either does the setup itself, or says exactly what's missing and the one command
that fixes it.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

LOCKFILES = ("package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb", "bun.lock")
HOOK_MARK = "# assay pre-push hook"
OLD_MARKS = ("# Assay, before every push",)  # the hook as first installed by hand, before `assay init --hook`
DASHBOARD = "http://localhost:8400"


def _git(root: Path, *args: str) -> Optional[str]:
    try:
        r = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, timeout=30,
                           encoding="utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


# ---------- the command, without an assay.toml ----------

def detected_command(root: Path) -> Optional[str]:
    """What `assay test` runs when there's no assay.toml: the project's runner, on its Assay tests
    (files with "assay" in their name for Jest and Vitest; pytest's plugin picks out its own)."""
    from assay import local
    js = local.js_project(root)
    if js:
        return {"jest": "npx jest assay", "vitest": "npx vitest run assay",
                "node": "node --test"}[js["runner"]]
    if any((root / f).exists() for f in ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt", "pytest.ini",
                                         "conftest.py", "tests")):
        return "pytest -q"
    return None


# ---------- dependencies in a git worktree ----------

def main_worktree(root: Path) -> Optional[Path]:
    """The main checkout, when root is inside another worktree of the same repository; else None."""
    common, own = _git(root, "rev-parse", "--path-format=absolute", "--git-common-dir"), \
        _git(root, "rev-parse", "--path-format=absolute", "--git-dir")
    if not common or not own or common.strip() == own.strip():
        return None
    listing = _git(root, "worktree", "list", "--porcelain") or ""
    first = next((l[9:] for l in listing.splitlines() if l.startswith("worktree ")), None)
    return Path(first) if first else None


def _runner_bin(folder: Path, runner: str) -> bool:
    return runner == "node" or (folder / "node_modules" / ".bin" / runner).exists() or \
        (folder / "node_modules" / ".bin" / f"{runner}.cmd").exists()


def _lock(folder: Path) -> Optional[Tuple[str, bytes]]:
    for name in LOCKFILES:
        if (folder / name).exists():
            return name, (folder / name).read_bytes()
    return None


def dependencies(root: Path) -> Tuple[bool, Optional[str]]:
    """Make sure a JavaScript project's test runner is installed before it runs. (ready, what was
    done or why it can't run). Inside a git worktree with nothing installed, the main checkout's
    node_modules is linked when the lockfiles are the same. Never runs a command that would
    download a different runner (npx with nothing installed does)."""
    from assay import local
    js = local.js_project(root)
    if not js or js["runner"] == "node" or _runner_bin(root, js["runner"]) or (root / ".pnp.cjs").exists():
        return True, None
    runner, here = js["runner"], root / "node_modules"
    main = main_worktree(root)
    install = "npm ci" if (root / "package-lock.json").exists() else "pnpm install" if (root / "pnpm-lock.yaml").exists() \
        else "yarn install" if (root / "yarn.lock").exists() else "npm install"
    if main is None or not _runner_bin(main, runner) or _lock(root) != _lock(main):
        if not here.exists() and not here.is_symlink():
            return _install(root, runner, "the main checkout's don't fit this lockfile" if main else None)
        return False, f"{runner} isn't installed here, so the tests can't run. Install the dependencies: {install}"
    if here.exists() or here.is_symlink():
        tracked = bool((_git(root, "ls-files", "--", "node_modules") or "").strip())
        why = ("node_modules is tracked in git here, so it can't be swapped for a link without changing tracked "
               "files. Stop tracking it (git rm -r --cached node_modules, with node_modules/ in .gitignore)"
               if tracked else "node_modules is here but incomplete. Install the dependencies")
        return False, f"{runner} isn't installed in this worktree: {why}, or run {install}."
    try:
        here.symlink_to(main / "node_modules", target_is_directory=True)
    except OSError as exc:
        return False, f"{runner} isn't installed here and node_modules couldn't be linked ({exc}). Run {install}."
    if _git(root, "check-ignore", "-q", "node_modules") is None:  # keep the link out of git status
        common = Path((_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir") or "").strip())
        exclude = common / "info" / "exclude"
        try:
            exclude.parent.mkdir(parents=True, exist_ok=True)
            with open(exclude, "a", encoding="utf-8") as f:
                f.write("\n/node_modules\n")
        except OSError:
            pass
    return True, f"Using the dependencies installed in {main} (same lockfile): node_modules is linked there."


INSTALL = {"package-lock.json": ["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"],
           "npm-shrinkwrap.json": ["npm", "ci", "--prefer-offline", "--no-audit", "--no-fund"],
           "pnpm-lock.yaml": ["pnpm", "install", "--frozen-lockfile", "--prefer-offline"],
           "yarn.lock": ["yarn", "install", "--frozen-lockfile", "--prefer-offline"]}


def _install(root: Path, runner: str, why: Optional[str]) -> Tuple[bool, Optional[str]]:
    """Install the dependencies from the lockfile, the way a developer would, where nothing is installed
    yet and node_modules isn't in git. ASSAY_NO_INSTALL=1 leaves it to you."""
    lock = _lock(root)
    cmd = INSTALL.get(lock[0]) if lock else None
    if not cmd:
        return False, f"{runner} isn't installed here, and there's no lockfile to install from: npm install"
    if os.environ.get("ASSAY_NO_INSTALL") or not shutil.which(cmd[0]):
        return False, f"{runner} isn't installed here, so the tests can't run. Install the dependencies: {' '.join(cmd[:2])}"
    print(f"{runner} isn't installed here{f' ({why})' if why else ''}: running {' '.join(cmd)}...", flush=True,
          file=__import__("sys").stderr)
    try:
        r = subprocess.run(cmd, cwd=root, timeout=900)
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{' '.join(cmd)} didn't finish ({exc}); install the dependencies, then run the tests again."
    if r.returncode != 0 or not _runner_bin(root, runner):
        return False, f"{' '.join(cmd)} failed (exit {r.returncode}); install the dependencies, then run the tests again."
    return True, f"Installed the dependencies ({' '.join(cmd)})."


# ---------- the pre-push hook ----------

HOOK_TEMPLATE = HOOK_MARK + r"""
# Before every push: no test weakened against the base branch, and the AI tests still pass.
# The run goes to the dashboard (ASSAY_URL, default http://localhost:8400) when one is running.
# Installed by `assay init --hook`; skip it once with: git push --no-verify

# The pipx install first: another `assay` earlier on PATH (a Python environment's) may be older.
ASSAY=${ASSAY_BIN:-$([ -x "$HOME/.local/bin/assay" ] && echo "$HOME/.local/bin/assay" || command -v assay)}
if [ -z "$ASSAY" ] || [ ! -x "$ASSAY" ]; then
  echo "Assay isn't installed (pipx install assay-server); pushing without its checks." >&2
  exit 0
fi
BASE=$(git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>/dev/null || echo origin/main)
git rev-parse --verify --quiet "$BASE" >/dev/null || { echo "Assay: no $BASE to compare with; skipping." >&2; exit 0; }

# Only branch pushes are checked, not tag pushes or deletions.
checked=0
while read -r local_ref local_sha remote_ref remote_sha; do
  case "$local_ref" in refs/heads/*) [ "$local_sha" != 0000000000000000000000000000000000000000 ] && checked=1 ;; esac
done
[ "$checked" = 1 ] || exit 0

echo "Assay: checking the tests against $BASE..." >&2
"$ASSAY" guard --base "$BASE" >&2
if [ $? = 1 ]; then
  echo "Assay: a test was weakened. Fix it, or push anyway with: git push --no-verify" >&2
  exit 1
fi

UPLOAD=
URL=${ASSAY_URL:-http://localhost:8400}
curl -s -o /dev/null -m 2 "$URL/healthz" 2>/dev/null && UPLOAD=--upload
ASSAY_URL=$URL "$ASSAY" test --base "$BASE" $UPLOAD >&2
code=$?
case $code in
  0) ;;
  1) echo "Assay: the tests regressed (see the report above, or the dashboard). Push anyway with: git push --no-verify" >&2; exit 1 ;;
  3) echo "Assay: some results were inconclusive; pushing. More attempts tell: assay test --repeat 3" >&2 ;;
  *) echo "Assay: no Assay tests ran here (see above); pushing with the weakened-test check only." >&2 ;;
esac
exit 0
"""


def hooks_dir(root: Path) -> Optional[Path]:
    """Where git looks for this repository's hooks (core.hooksPath, else the shared .git/hooks)."""
    custom = (_git(root, "config", "--get", "core.hooksPath") or "").strip()
    if custom:
        top = (_git(root, "rev-parse", "--show-toplevel") or "").strip()
        return (Path(top) / custom) if top and not os.path.isabs(custom) else Path(custom)
    common = (_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir") or "").strip()
    return Path(common) / "hooks" if common else None


def _ours(hook: Path) -> bool:
    text = hook.read_text(encoding="utf-8", errors="replace")
    return HOOK_MARK in text or any(m in text for m in OLD_MARKS)


def install_hook(root: Path) -> Tuple[bool, str]:
    """Write the pre-push hook (every worktree of the repository shares it). Another hook already
    there is left alone. (installed, message)."""
    folder = hooks_dir(root)
    if folder is None:
        return False, "This isn't a git repository, so there's nowhere to put a pre-push hook."
    hook = folder / "pre-push"
    if hook.exists() and not _ours(hook):
        return False, (f"{hook} is another tool's hook, so it was left alone. To run Assay's checks too, add this "
                       f"line to it:\n  assay guard && assay test --base origin/main")
    existed = hook.exists()
    folder.mkdir(parents=True, exist_ok=True)
    hook.write_text("#!/bin/sh\n" + HOOK_TEMPLATE.lstrip("\n"), encoding="utf-8", newline="\n")
    hook.chmod(0o755)
    return True, (f"{'Updated' if existed else 'Installed'} the pre-push hook ({hook}). Every push from this "
                  "repository, in every worktree, is checked: a weakened test or a regression stops it.")


# ---------- assay doctor ----------

def _version_of(exe: str) -> Optional[str]:
    try:
        r = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"\d+\.\d+\.\d+", r.stdout + r.stderr) if r.returncode == 0 else None
    return m.group(0) if m else None


def _assays() -> List[str]:
    """Every `assay` executable on PATH, in order, plus the pipx one."""
    seen, out = set(), []
    for d in os.environ.get("PATH", "").split(os.pathsep) + [str(Path.home() / ".local" / "bin")]:
        p = Path(d) / ("assay.exe" if os.name == "nt" else "assay")
        if p.exists() and os.access(p, os.X_OK) and str(p.resolve()) not in seen:
            seen.add(str(p.resolve()))
            out.append(str(p))
    return out


def doctor(root: Path) -> Tuple[int, str]:
    """`assay doctor`: is everything in place for the checks to run? (exit code, report)."""
    import assay
    from assay import local, weakening
    lines, problems = [], 0

    def ok(text):
        lines.append(f"  ok   {text}")

    def bad(text, fix):
        nonlocal problems
        problems += 1
        lines.append(f"  fix  {text}\n       → {fix}")

    lines.append(f"Assay {assay.__version__}, in {root}")
    stale = [(exe, _version_of(exe)) for exe in _assays()]
    stale = [(exe, v) for exe, v in stale if v != assay.__version__]
    for exe, v in stale:
        bad(f"{exe} is assay {v or 'older than 0.5.0'}, not {assay.__version__}",
            "upgrade it (pip install -U assay-server, in the Python it belongs to), or remove it")
    if not stale:
        ok("every assay on PATH is this version")
    try:
        import assay_sdk
        ok(f"the Python SDK (assay-evals {assay_sdk.__version__})")
    except ImportError:
        bad("the Python SDK isn't installed in this Python", "pip install assay-evals")

    if _git(root, "rev-parse", "--git-dir") is None:
        bad("this isn't a git repository", "the weakened-test check and the hook need git")
    else:
        base = weakening.base_ref(root) or weakening.default_base(root)
        ok(f"base branch to compare with: {base}") if base else \
            bad("no base branch to compare with (origin/main, main)", "git fetch origin")
        folder = hooks_dir(root)
        hook = folder / "pre-push" if folder else None
        if hook and hook.exists() and HOOK_MARK in hook.read_text(encoding="utf-8", errors="replace"):
            ok(f"the pre-push hook ({hook})")
        elif hook and hook.exists() and _ours(hook):
            bad("the pre-push hook is an earlier version", "assay init --hook")
        elif hook and hook.exists():
            bad(f"{hook} is another tool's hook, without Assay's checks",
                "add `assay guard && assay test --base origin/main` to it")
        else:
            bad("no pre-push hook: pushes aren't checked", "assay init --hook")

    if (root / local.CONFIG).exists():
        ok(f"{local.CONFIG}")
    else:
        cmd = detected_command(root)
        ok(f"no {local.CONFIG}: `assay test` runs `{cmd}`") if cmd else \
            bad(f"no {local.CONFIG}, and no test runner found", "assay init")
    js = local.js_project(root)
    if js and js["runner"] != "node":
        if _runner_bin(root, js["runner"]):
            ok(f"{js['runner']} is installed")
        else:
            main = main_worktree(root)
            same = main is not None and _runner_bin(main, js["runner"]) and _lock(root) == _lock(main)
            tracked = bool((_git(root, "ls-files", "--", "node_modules") or "").strip())
            if same and not tracked and not (root / "node_modules").exists():
                ok(f"{js['runner']} isn't installed here; `assay test` links {main}'s node_modules (same lockfile)")
            else:
                bad(f"{js['runner']} isn't installed here, so the tests can't run",
                    "node_modules is tracked in git here: git rm -r --cached node_modules, with node_modules/ in "
                    ".gitignore" if tracked else "npm ci")
        if js and not js["installed"]:
            bad("assay-evals isn't a dependency, so the tests can't record", "npm install --save-dev assay-evals")

    url = os.environ.get("ASSAY_URL") or DASHBOARD
    try:
        import urllib.request
        with urllib.request.urlopen(f"{url}/healthz", timeout=3) as r:
            ok(f"the dashboard at {url}") if r.status == 200 else bad(f"{url} answered {r.status}", "check the server")
    except Exception:
        lines.append(f"  --   no dashboard at {url} (optional: runs are kept here; `assay serve` shows them)")
    lines.append("")
    lines.append("Everything's in place." if not problems else
                 f"{problems} thing{'s' * (problems != 1)} to fix, above.")
    return (1 if problems else 0), "\n".join(lines)
