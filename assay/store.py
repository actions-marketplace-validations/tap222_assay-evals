"""Assay's own database: measure results, gate decisions and ingested events.

Kept separate from any pipeline database. Defaults to SQLite for local use;
point ASSAY_STORE_URL at Postgres in production.
"""
from __future__ import annotations

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        MetaData, String, Table, UniqueConstraint, create_engine)
from sqlalchemy.engine import Engine

metadata = MetaData()

measure_runs = Table(
    "measure_runs", metadata,
    Column("id", Integer, primary_key=True),
    Column("started_at", DateTime, nullable=False),
    Column("source", String(64), nullable=False),
    Column("window_start", DateTime, nullable=False),
    Column("window_end", DateTime, nullable=False),
)

measure_results = Table(
    "measure_results", metadata,
    Column("id", Integer, primary_key=True),
    Column("run_id", Integer, ForeignKey("measure_runs.id"), nullable=False, index=True),
    Column("measure_id", String(64), nullable=False, index=True),
    Column("status", String(16), nullable=False),  # measured | unmeasured
    Column("reason", String(512)),
    Column("dimension", String(64)),  # None for the overall row
    Column("slice_value", String(256)),
    Column("value", Float),
    Column("numerator", Float),
    Column("denominator", Float),
    Column("n", Integer),
    Column("note", String(512)),
    Column("stderr", Float),
)

gate_decisions = Table(
    "gate_decisions", metadata,
    Column("id", Integer, primary_key=True),
    Column("tenant", String(64), nullable=False, default="*", index=True),
    Column("created_at", DateTime, nullable=False),
    Column("outcome", String(16), nullable=False),  # advance | hold | rollback
    Column("lineage", JSON, nullable=False),
    Column("detail", JSON, nullable=False),
)

# --- Alerting ---

slos = Table(
    "slos", metadata,
    Column("id", Integer, primary_key=True),
    Column("source", String(64), nullable=False),  # "*" = every source
    Column("measure_id", String(64), nullable=False),
    # dimension set + slice_value None = every slice of that dimension must meet it
    Column("dimension", String(64)),
    Column("slice_value", String(256)),
    Column("target", Float, nullable=False),
    Column("note", String(256)),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint("source", "measure_id", "dimension", "slice_value", name="uq_slo_scope"),
)

alerts = Table(
    "alerts", metadata,
    Column("id", Integer, primary_key=True),
    Column("source", String(64), nullable=False, index=True),
    Column("measure_id", String(64), nullable=False),
    Column("dimension", String(64)),
    Column("slice_value", String(256)),
    Column("kind", String(16), nullable=False),  # anomaly | slo
    Column("state", String(16), nullable=False, index=True),  # pending | open | resolved
    Column("streak", Integer, nullable=False, default=1),  # consecutive runs the condition held
    Column("clear_streak", Integer, nullable=False, default=0),  # consecutive runs it hasn't, while open
    Column("opened_at", DateTime, nullable=False),
    Column("last_seen_at", DateTime, nullable=False),
    Column("resolved_at", DateTime),
    Column("run_id", Integer),
    Column("value", Float),
    Column("expected_low", Float),
    Column("expected_high", Float),
    Column("target", Float),
    Column("n", Integer),
    Column("message", String(512), nullable=False),
)

# --- Event ingest, for pipelines that push records instead of exposing a database ---

event_calls = Table(
    "event_calls", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("call_id", String(128), primary_key=True),
    Column("stage", String(64), nullable=False),
    Column("ts", DateTime, nullable=False, index=True),
    Column("document_id", String(128)),
    Column("model_declared", String(128)),
    Column("model_served", String(128)),
    Column("resolving_layer", String(64)),
    Column("gate_reason", String(128)),
    Column("cost_usd", Float),
    Column("code_revision", String(64)),
    Column("segment", String(128)),
    Column("document_type", String(128)),
    Column("latency_ms", Float),
    Column("status", String(32)),
)

event_documents = Table(
    "event_documents", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("document_id", String(128), primary_key=True),
    Column("received_at", DateTime, nullable=False, index=True),
    Column("completed_at", DateTime),
    Column("status", String(64)),
    Column("processing_mode", String(32)),
    Column("file_hash", String(128)),
    Column("segment", String(128)),
    Column("document_type", String(128)),
    Column("delivered_downstream", Boolean),
    Column("page_count", Integer),
)

event_stage_runs = Table(
    "event_stage_runs", metadata,
    Column("tenant", String(64), primary_key=True),
    # Sent by the caller, or derived from document, stage and start time, so a
    # retried batch updates rather than duplicates.
    Column("run_id", String(128), primary_key=True),
    Column("document_id", String(128), nullable=False),
    Column("stage", String(64), nullable=False),
    Column("status", String(32), nullable=False),
    Column("started_at", DateTime, index=True),
    Column("finished_at", DateTime),
    Column("did_work", Boolean),
    Column("outputs", JSON),
    Column("sequence", Integer),
)

event_errors = Table(
    "event_errors", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("error_id", String(128), primary_key=True),
    Column("document_id", String(128), nullable=False, index=True),
    Column("field", String(256), nullable=False),
    Column("reported_at", DateTime, nullable=False, index=True),
    Column("expected", String(4096)),
    Column("observed", String(4096)),
    Column("kind", String(16), nullable=False),
    Column("reporter", String(128)),
    Column("source", String(64)),
)

event_indexed = Table(
    "event_indexed", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("extraction_id", String(128), primary_key=True),
    Column("field", String(128)),
    Column("document_id", String(128), nullable=False),
    Column("has_positions", Boolean, nullable=False),
    Column("segment", String(128)),
    Column("document_type", String(128)),
)


event_reviews = Table(
    "event_reviews", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("review_id", String(128), primary_key=True),
    Column("document_id", String(128), nullable=False, index=True),
    Column("ts", DateTime, nullable=False, index=True),
    Column("kind", String(16), nullable=False),
    Column("minutes", Float),
    Column("cost_usd", Float),
    Column("reviewer", String(128)),
    Column("stage", String(64)),
)

# Prices Assay can't read from the pipeline: people time and platform overhead.
cost_rates = Table(
    "cost_rates", metadata,
    Column("id", Integer, primary_key=True),
    Column("source", String(64), nullable=False),  # "*" = every source
    Column("key", String(64), nullable=False),
    Column("value", Float, nullable=False),
    Column("updated_at", DateTime, nullable=False),
    UniqueConstraint("source", "key", name="uq_cost_rate"),
)


api_keys = Table(
    "api_keys", metadata,
    Column("id", Integer, primary_key=True),
    Column("key_hash", String(64), nullable=False, unique=True),  # sha256; the key itself is never stored
    Column("prefix", String(16), nullable=False),  # first characters, to recognise a key in lists
    Column("name", String(128), nullable=False),
    Column("tenant", String(64), nullable=False, index=True),
    Column("scopes", String(128), nullable=False),
    Column("created_at", DateTime, nullable=False),
    Column("expires_at", DateTime),
    Column("last_used_at", DateTime),
    Column("revoked_at", DateTime),
)


def make_engine(url: str) -> Engine:
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = create_engine(url, **kwargs)
    metadata.create_all(engine)
    return engine
