"""Database schema migration engine supporting migrations beyond simple column additions.

Tracks applied versions with checksums, timestamps, and forward/backward (up/down)
migrations including column renames, drops, and structural transformations across
PostgreSQL and SQLite.
"""
from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Dict, List, Optional

from sqlalchemy import Column, DateTime, Integer, String, Table, inspect, select, text
from sqlalchemy.engine import Engine

log = logging.getLogger(__name__)


@dataclass
class Migration:
    version: str
    name: str
    up: Callable[[Any], None]
    down: Optional[Callable[[Any], None]] = None

    @property
    def checksum(self) -> str:
        s = f"{self.version}:{self.name}"
        return hashlib.sha256(s.encode()).hexdigest()[:16]


# Registry of ordered schema migrations
MIGRATIONS: List[Migration] = []


def register_migration(version: str, name: str, up: Callable[[Any], None],
                       down: Optional[Callable[[Any], None]] = None) -> Migration:
    m = Migration(version=version, name=name, up=up, down=down)
    MIGRATIONS.append(m)
    MIGRATIONS.sort(key=lambda x: x.version)
    return m


def ensure_migrations_table(engine: Engine) -> None:
    """Ensure the schema_migrations tracking table exists."""
    insp = inspect(engine)
    if not insp.has_table("schema_migrations"):
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE TABLE schema_migrations ("
                "version VARCHAR(64) PRIMARY KEY, "
                "name VARCHAR(256) NOT NULL, "
                "applied_at TIMESTAMP NOT NULL, "
                "checksum VARCHAR(64) NOT NULL"
                ")"
            ))


def get_applied_versions(engine: Engine) -> List[str]:
    ensure_migrations_table(engine)
    with engine.connect() as conn:
        rows = conn.execute(text("SELECT version FROM schema_migrations ORDER BY version")).fetchall()
        return [r[0] for r in rows]


def apply_migration(engine: Engine, migration: Migration) -> bool:
    """Apply a single forward migration within a transaction."""
    ensure_migrations_table(engine)
    applied = get_applied_versions(engine)
    if migration.version in applied:
        return False

    now = datetime.utcnow()
    with engine.begin() as conn:
        log.info("Applying migration %s: %s", migration.version, migration.name)
        migration.up(conn)
        conn.execute(
            text("INSERT INTO schema_migrations (version, name, applied_at, checksum) VALUES (:v, :n, :at, :c)"),
            {"v": migration.version, "n": migration.name, "at": now, "c": migration.checksum}
        )
    return True


def rollback_migration(engine: Engine, migration: Migration) -> bool:
    """Roll back a single migration within a transaction."""
    ensure_migrations_table(engine)
    applied = get_applied_versions(engine)
    if migration.version not in applied:
        return False
    if not migration.down:
        raise RuntimeError(f"Migration {migration.version} ({migration.name}) has no down() rollback logic")

    with engine.begin() as conn:
        log.info("Rolling back migration %s: %s", migration.version, migration.name)
        migration.down(conn)
        conn.execute(
            text("DELETE FROM schema_migrations WHERE version = :v"),
            {"v": migration.version}
        )
    return True


def migrate(engine: Engine, target_version: Optional[str] = None) -> List[str]:
    """Run all pending registered migrations up to target_version (or all if None)."""
    ensure_migrations_table(engine)
    applied = set(get_applied_versions(engine))
    executed: List[str] = []

    for m in MIGRATIONS:
        if m.version in applied:
            continue
        if target_version and m.version > target_version:
            break
        if apply_migration(engine, m):
            executed.append(m.version)
    return executed


# ---------- Standard Helper Operations ----------

def rename_column(conn, table: str, old_col: str, new_col: str) -> None:
    """Dialect-compatible column rename (supported on PostgreSQL and SQLite >= 3.25)."""
    conn.execute(text(f'ALTER TABLE {table} RENAME COLUMN "{old_col}" TO "{new_col}"'))


def drop_column(conn, table: str, col: str) -> None:
    """Drop column (PostgreSQL & SQLite >= 3.35)."""
    conn.execute(text(f'ALTER TABLE {table} DROP COLUMN "{col}"'))


# Example migration registering base schema markers
register_migration(
    version="20260901_00_initial_schema",
    name="Initial Assay baseline",
    up=lambda conn: None,
    down=lambda conn: None,
)
