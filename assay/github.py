"""The run's summary as a pull request comment: one comment per PR, updated on every push.

`assay pr-comment` posts .assay/summary.md (written by `pytest --assay` and `assay test`). It
finds its earlier comment by a hidden marker and edits it, so a PR doesn't collect one comment
per push. In GitHub Actions everything it needs is in the environment: GITHUB_TOKEN (with
`pull-requests: write`), GITHUB_REPOSITORY, and the PR number from the event.
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from typing import Callable, Optional, Tuple

API = "https://api.github.com"
Http = Callable[[str, str, Optional[dict], dict], Tuple[int, object]]


def _http(method: str, url: str, body: Optional[dict], headers: dict) -> Tuple[int, object]:
    req = urllib.request.Request(url, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Accept": "application/vnd.github+json", "Content-Type": "application/json",
                                          "X-GitHub-Api-Version": "2022-11-28", **headers}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"null")
        except ValueError:
            return e.code, None


def pr_number(env=os.environ) -> Optional[int]:
    """The pull request this job runs for: from the event payload, else refs/pull/<n>/merge."""
    path = env.get("GITHUB_EVENT_PATH")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            event = json.load(f)
        n = (event.get("pull_request") or {}).get("number") or \
            ((event.get("issue") or {}).get("pull_request") and event["issue"].get("number"))
        if n:
            return int(n)
    m = re.match(r"refs/pull/(\d+)/", env.get("GITHUB_REF", ""))
    return int(m.group(1)) if m else None


def comment(body: str, repo: str, pr: int, token: str, marker: str, http: Http = _http) -> str:
    """Create the PR's Assay comment, or update the one already there. Returns "created" or "updated"."""
    headers = {"Authorization": f"Bearer {token}"}
    url = f"{API}/repos/{repo}/issues/{pr}/comments"
    page = 1
    while True:
        code, items = http("GET", f"{url}?per_page=100&page={page}", None, headers)
        if code != 200:
            raise RuntimeError(f"GitHub refused listing the PR's comments ({code}): {items}")
        mine = next((c for c in items if marker in (c.get("body") or "")), None)
        if mine:
            code, out = http("PATCH", f"{API}/repos/{repo}/issues/comments/{mine['id']}", {"body": body}, headers)
            if code != 200:
                raise RuntimeError(f"GitHub refused updating the comment ({code}): {out}")
            return "updated"
        if len(items) < 100:
            break
        page += 1
    code, out = http("POST", url, {"body": body}, headers)
    if code != 201:
        raise RuntimeError(f"GitHub refused the comment ({code}): {out}. The job needs `permissions: "
                           f"pull-requests: write`.")
    return "created"
