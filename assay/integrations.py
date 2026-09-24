"""Outbound connections a non-technical person can set up from the dashboard.

- slack:  paste an incoming-webhook URL; alerts go there, with links back.
- jira:   site, email, API token, project key; a failure pattern becomes a ticket.
- linear: API key and team; same.
- ci:     a ready-to-paste GitHub Actions / GitLab job (or plain script) that blocks a
          release unless Assay's release call is "advance".

Saved secrets (tokens, webhook URLs) are never returned by the API: reads show them
masked, and saving a config with the masked value keeps the old secret.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
from datetime import datetime
from typing import Callable, Dict, List, Optional

from sqlalchemy import and_, or_, select
from sqlalchemy.engine import Engine

from assay import store

log = logging.getLogger(__name__)
KINDS = {
    "slack": {"label": "Slack", "fields": {"webhook_url": "Incoming webhook URL"}, "secret": ["webhook_url"]},
    "jira": {"label": "Jira", "fields": {"site": "Site, e.g. https://acme.atlassian.net", "email": "Your Jira email",
                                         "api_token": "API token", "project": "Project key, e.g. QA",
                                         "issue_type": "Issue type (default Bug)"}, "secret": ["api_token"]},
    "linear": {"label": "Linear", "fields": {"api_key": "Personal API key", "team_id": "Team ID"},
               "secret": ["api_key"]},
}
MASK = "••••••••"


def _mask(kind: str, cfg: dict) -> dict:
    return {k: (MASK if k in KINDS[kind]["secret"] and v else v) for k, v in cfg.items()}


def get(engine: Engine, source: str, kind: str, reveal: bool = False) -> Optional[dict]:
    """A source's own config, else the "*" one."""
    t = store.integrations
    with engine.connect() as conn:
        rows = {r.source: r for r in conn.execute(select(t).where(and_(t.c.kind == kind, or_(
            t.c.source == source, t.c.source == "*"))))}
    r = rows.get(source) or rows.get("*")
    if r is None or not r.enabled:
        return None
    return r.config if reveal else _mask(kind, r.config)


def list_(engine: Engine, source: str) -> Dict[str, Optional[dict]]:
    return {k: get(engine, source, k) for k in KINDS}


def save(engine: Engine, source: str, kind: str, config: dict) -> dict:
    """Save a config; a field sent back masked keeps its stored secret."""
    old = get(engine, source, kind, reveal=True) or {}
    cfg = {k: (old.get(k) if v == MASK else v) for k, v in config.items() if k in KINDS[kind]["fields"]}
    missing = [k for k in KINDS[kind]["fields"] if not cfg.get(k) and k != "issue_type"]
    if missing:
        raise ValueError(f"{KINDS[kind]['label']} needs: {', '.join(KINDS[kind]['fields'][m] for m in missing)}.")
    t = store.integrations
    with engine.begin() as conn:
        conn.execute(t.delete().where(and_(t.c.source == source, t.c.kind == kind)))
        conn.execute(t.insert().values(source=source, kind=kind, config=cfg, enabled=True,
                                       updated_at=datetime.utcnow()))
    return _mask(kind, cfg)


def remove(engine: Engine, source: str, kind: str) -> None:
    t = store.integrations
    with engine.begin() as conn:
        conn.execute(t.delete().where(and_(t.c.source == source, t.c.kind == kind)))


def _post(url: str, body: dict, headers: Optional[dict] = None, timeout: float = 10) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read().decode() or "{}"
            try:
                return json.loads(raw)
            except ValueError:
                return {"text": raw}
    except urllib.error.HTTPError as e:
        detail = e.read().decode()[:300]
        raise RuntimeError(f"{e.code} from {url.split('/')[2]}: {detail or e.reason}") from None
    except urllib.error.URLError as e:
        raise RuntimeError(f"Couldn't reach {url.split('/')[2]}: {e.reason}") from None


# ---------- Slack ----------

def slack_send(cfg: dict, text: str) -> None:
    _post(cfg["webhook_url"], {"text": text})


def notifier(engine: Engine, source: str, public_url: Optional[str] = None,
             fallback: Optional[Callable[[str, dict], None]] = None) -> Optional[Callable[[str, dict], None]]:
    """Alerts to the Slack channel set up in the dashboard, and to the server's webhook if configured."""
    cfg = get(engine, source, "slack", reveal=True)
    if cfg is None:
        return fallback
    from assay.alerts import webhook_notifier
    slack = webhook_notifier(cfg["webhook_url"], "slack", public_url)

    def both(event: str, alert: dict) -> None:
        slack(event, alert)
        if fallback:
            fallback(event, alert)
    return both


# ---------- tickets ----------

def ticket_text(pattern: dict, base_url: Optional[str]) -> tuple:
    title = f"[Assay] {pattern['name']}"[:250]
    lines = [f"{pattern['traces']} production traces show this pattern ({pattern.get('kind')}).",
             f"Example: {pattern.get('example')}",
             f"First seen {pattern.get('first', '')[:16].replace('T', ' ')}, last seen "
             f"{pattern.get('last', '')[:16].replace('T', ' ')}."]
    if pattern.get("onset"):
        lines.append(f"Started around {pattern['onset']['at'][:16].replace('T', ' ')}.")
    if pattern.get("distinguishing"):
        lines.append("Sets these apart: " + ", ".join(f"{d['feature']}" for d in pattern["distinguishing"][:4]) + ".")
    lines.append("Example traces: " + ", ".join(pattern.get("trace_ids", [])[:5]))
    if base_url:
        lines.append(f"Open in Assay: {base_url.rstrip('/')}/#learn")
    return title, "\n".join(lines)


def create_ticket(engine: Engine, source: str, pattern: dict, base_url: Optional[str] = None) -> dict:
    """A ticket in whichever tracker is set up (Jira first, then Linear)."""
    title, body = ticket_text(pattern, base_url)
    jira = get(engine, source, "jira", reveal=True)
    if jira:
        auth = base64.b64encode(f"{jira['email']}:{jira['api_token']}".encode()).decode()
        doc = {"type": "doc", "version": 1, "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in body.split("\n")]}
        out = _post(f"{jira['site'].rstrip('/')}/rest/api/3/issue",
                    {"fields": {"project": {"key": jira["project"]}, "summary": title, "description": doc,
                                "issuetype": {"name": jira.get("issue_type") or "Bug"}}},
                    {"Authorization": f"Basic {auth}", "Accept": "application/json"})
        key = out.get("key")
        return {"tracker": "jira", "id": key, "url": f"{jira['site'].rstrip('/')}/browse/{key}" if key else None}
    linear = get(engine, source, "linear", reveal=True)
    if linear:
        q = ("mutation($i: IssueCreateInput!) { issueCreate(input: $i) { success issue { identifier url } } }")
        out = _post("https://api.linear.app/graphql", {"query": q, "variables": {"i": {
            "teamId": linear["team_id"], "title": title, "description": body}}},
            {"Authorization": linear["api_key"]})
        issue = ((out.get("data") or {}).get("issueCreate") or {}).get("issue") or {}
        if not issue:
            raise RuntimeError(f"Linear didn't create the issue: {json.dumps(out.get('errors') or out)[:300]}")
        return {"tracker": "linear", "id": issue.get("identifier"), "url": issue.get("url")}
    raise ValueError("No ticket tracker is set up. Add Jira or Linear under Connect → Integrations.")


def record_ticket(engine: Engine, source: str, key: str, ticket: dict) -> None:
    t = store.pattern_log
    with engine.begin() as conn:
        conn.execute(t.update().where(and_(t.c.source == source, t.c.key == key))
                     .values(ticket_url=ticket.get("url"), ticket_id=ticket.get("id")))


def test(engine: Engine, source: str, kind: str) -> str:
    cfg = get(engine, source, kind, reveal=True)
    if cfg is None:
        raise ValueError(f"{KINDS[kind]['label']} isn't set up.")
    if kind == "slack":
        slack_send(cfg, "✅ Assay is connected to this channel. Alerts will appear here.")
        return "Sent a test message to the channel."
    if kind == "jira":
        req = urllib.request.Request(f"{cfg['site'].rstrip('/')}/rest/api/3/project/{cfg['project']}", headers={
            "Authorization": "Basic " + base64.b64encode(f"{cfg['email']}:{cfg['api_token']}".encode()).decode(),
            "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                name = json.loads(r.read().decode()).get("name")
        except urllib.error.HTTPError as e:
            raise RuntimeError(f"Jira said {e.code}: check the email, token and project key.") from None
        except urllib.error.URLError as e:
            raise RuntimeError(f"Couldn't reach Jira: {e.reason}") from None
        return f"Connected to Jira project {name or cfg['project']}."
    out = _post("https://api.linear.app/graphql", {"query": "{ viewer { name } }"}, {"Authorization": cfg["api_key"]})
    if not (out.get("data") or {}).get("viewer"):
        raise RuntimeError("Linear refused the key.")
    return f"Connected to Linear as {out['data']['viewer']['name']}."


# ---------- CI ----------

def ci_config(system: str, base_url: str, source: str, tolerance: float = 0.01) -> str:
    """A job that asks Assay for the release call on the latest eval run and fails unless it's advance."""
    base = base_url.rstrip("/")
    script = (f'RUN_ID="${{ASSAY_RUN_ID:?set ASSAY_RUN_ID to the evaluation run your tests just sent}}"\n'
              f'RESULT=$(curl -sf -X POST "{base}/v1/evals/runs/$RUN_ID/gate" \\\n'
              f'  -H "Authorization: Bearer $ASSAY_KEY" -H "Content-Type: application/json" \\\n'
              f'  -d \'{{"source": "{source}", "tolerance": {tolerance}}}\')\n'
              'OUTCOME=$(echo "$RESULT" | python3 -c "import json,sys; print(json.load(sys.stdin)[\'outcome\'])")\n'
              'echo "$RESULT" | python3 -c "import json,sys; [print(\'-\', r) for r in json.load(sys.stdin)[\'reasons\']]"\n'
              f'echo "Assay says: $OUTCOME  ({base}/#failures)"\n'
              'test "$OUTCOME" = "advance"')
    if system == "github":
        body = "\n".join("          " + line for line in script.split("\n"))
        return ("# .github/workflows/assay-gate.yml\n"
                "# Add ASSAY_KEY (a key with the manage scope) under Settings → Secrets → Actions.\n"
                "name: Assay release check\non:\n  workflow_dispatch:\n    inputs:\n"
                "      run_id: { description: 'Evaluation run id', required: true }\n"
                "  workflow_call:\n    inputs:\n      run_id: { type: string, required: true }\n"
                "    secrets:\n      ASSAY_KEY: { required: true }\n"
                "jobs:\n  gate:\n    runs-on: ubuntu-latest\n    steps:\n"
                "      - name: Ask Assay whether this release can go out\n"
                "        env:\n          ASSAY_KEY: ${{ secrets.ASSAY_KEY }}\n"
                "          ASSAY_RUN_ID: ${{ inputs.run_id }}\n"
                f"        run: |\n{body}\n")
    if system == "gitlab":
        body = "\n".join("      " + line for line in script.split("\n"))
        return ("# .gitlab-ci.yml: add ASSAY_KEY (manage scope) and ASSAY_RUN_ID as CI/CD variables\n"
                "assay-gate:\n  stage: test\n  image: python:3.12-slim\n"
                "  before_script:\n    - apt-get update -qq && apt-get install -y -qq curl\n"
                f"  script:\n    - |\n{body}\n")
    return "#!/bin/sh\n# Needs ASSAY_KEY and ASSAY_RUN_ID in the environment. Exits 1 unless Assay says advance.\nset -e\n" + script + "\n"
