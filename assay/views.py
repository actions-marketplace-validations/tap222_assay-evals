"""What the dashboard shows for a source: its views, their tabs, and the measure groups.

Two views. Agents: test runs, agent runs and production review. Documents: a document
pipeline's measures and traces. A source starts with each view's core tabs, and people turn
on the rest one by one in Settings; the choice is saved for everyone who opens that source.

A view left unset is decided by the dashboard: agents is on, documents is on when the source
has document data. On an open server (no keys), the demo sources can't be changed, so a
visitor to a public demo can't rearrange it for everyone else.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Optional

from sqlalchemy import select
from sqlalchemy.engine import Engine

from assay import store
from assay.auth import Principal
from assay.measures import GROUPS

VIEWS = {
    "agents": {"label": "Agents",
               "tabs": ["overview", "diff", "failures", "gates", "prompts", "agents", "learn", "review", "alerts"],
               "core": ["overview", "diff", "failures", "agents", "alerts"]},
    "documents": {"label": "Documents",
                  "tabs": ["summary", "workflow", "measures", "cost", "errors", "trace", "alerts"],
                  "core": ["summary", "workflow", "measures", "errors"]},
}
DEMO_SOURCES = {"events:demo", "events:demo-agent", "events:demo-eval"}  # what `assay demo` loads


def catalog() -> dict:
    return {"views": VIEWS, "measure_groups": list(GROUPS)}


def defaults() -> dict:
    return {"views": {k: {"enabled": None, "tabs": list(v["core"])} for k, v in VIEWS.items()},
            "measure_groups": list(GROUPS)}


def normalize(config: Dict[str, Any]) -> dict:
    """A config as saved: known views and tabs only, in the dashboard's order. Raises ValueError."""
    out = defaults()
    views = config.get("views") or {}
    unknown = set(views) - set(VIEWS)
    if unknown:
        raise ValueError(f"Unknown view: {', '.join(sorted(unknown))}. Views: {', '.join(VIEWS)}.")
    for name, v in views.items():
        enabled = v.get("enabled")
        if enabled is not None and not isinstance(enabled, bool):
            raise ValueError(f"{name}.enabled is true, false or null (decided by the data).")
        tabs = v.get("tabs", VIEWS[name]["core"])
        bad = [t for t in tabs if t not in VIEWS[name]["tabs"]]
        if bad:
            raise ValueError(f"The {name} view has no tab {', '.join(bad)}. Its tabs: {', '.join(VIEWS[name]['tabs'])}.")
        if enabled is not False and not tabs:
            raise ValueError(f"Turn on at least one tab of the {name} view, or turn the view off.")
        out["views"][name] = {"enabled": enabled, "tabs": [t for t in VIEWS[name]["tabs"] if t in tabs]}
    if all(v["enabled"] is False for v in out["views"].values()):
        raise ValueError("Keep at least one view on.")
    if "measure_groups" in config:
        groups = config["measure_groups"] or []
        bad = [g for g in groups if g not in GROUPS]
        if bad:
            raise ValueError(f"Unknown measure group: {', '.join(bad)}.")
        out["measure_groups"] = [g for g in GROUPS if g in groups]
    return out


def get(engine: Engine, source: str) -> dict:
    t = store.dashboard_views
    with engine.connect() as conn:
        row = conn.execute(select(t).where(t.c.source == source)).first()
    if row is None:
        return {"saved": False, "config": defaults(), "updated_at": None}
    return {"saved": True, "config": normalize(row.config), "updated_at": row.updated_at.isoformat()}


def save(engine: Engine, source: str, config: Dict[str, Any]) -> dict:
    cfg = normalize(config)
    t = store.dashboard_views
    with engine.begin() as conn:
        conn.execute(t.delete().where(t.c.source == source))
        conn.execute(t.insert().values(source=source, config=cfg, updated_at=datetime.utcnow()))
    return get(engine, source)


def reset(engine: Engine, source: str) -> dict:
    t = store.dashboard_views
    with engine.begin() as conn:
        conn.execute(t.delete().where(t.c.source == source))
    return get(engine, source)


def why_not_saved(p: Principal, source: str) -> Optional[str]:
    """Why this caller can't change the source's settings for everyone, or None if they can."""
    if not p.can("manage"):
        return "Changing what the dashboard shows needs a key with the manage scope."
    if p.mode == "open" and source in DEMO_SOURCES:
        return "This is an open demo, so changes stay in your browser and don't change it for anyone else."
    return None
