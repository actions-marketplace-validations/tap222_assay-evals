"""Assay's own database: measure results, gate decisions and ingested events.

Kept separate from any pipeline database. Defaults to SQLite for local use;
point ASSAY_STORE_URL at Postgres in production.
"""
from __future__ import annotations

from sqlalchemy import (JSON, Boolean, Column, DateTime, Float, ForeignKey, Integer,
                        MetaData, String, Table, Text, UniqueConstraint, create_engine)
from typing import List

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

# Rules about the steps a document may take. See assay/contracts.py.
path_contracts = Table(
    "path_contracts", metadata,
    Column("id", Integer, primary_key=True),
    Column("source", String(64), nullable=False, index=True),  # "*" = every source
    Column("kind", String(32), nullable=False),  # must_include | never | before | only_after | max_runs | allowed_steps
    Column("step", String(64)),
    Column("other", String(64)),  # before / only_after: the other step
    Column("max_runs", Integer),
    Column("steps", JSON),  # allowed_steps
    Column("when", JSON),  # {"segment": [...], ...}: applies only to documents matching all
    Column("unless", JSON),  # documents matching any are exempt
    Column("where", JSON),  # conditions on the step's arguments (tool calls): {"confirmed": {"not": true}}
    Column("same", JSON),  # before / only_after: arguments the other step must share, e.g. ["order_id"]
    Column("identical", Boolean),  # max_runs: count only calls with identical arguments
    Column("severity", String(16), nullable=False, default="critical"),  # critical | warning
    Column("note", String(512)),
    Column("updated_at", DateTime, nullable=False),
)

alerts = Table(
    "alerts", metadata,
    Column("id", Integer, primary_key=True),
    Column("source", String(64), nullable=False, index=True),
    Column("measure_id", String(64), nullable=False),
    Column("dimension", String(64)),
    Column("slice_value", String(256)),
    Column("kind", String(16), nullable=False),  # anomaly | slo | regression | contract
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
    Column("prompt_id", String(128)),
    Column("prompt_version", String(64)),
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
    Column("prompt_id", String(128)),
    Column("prompt_version", String(64)),
)

# Every prompt version seen in traffic or registered from CI.
prompt_versions = Table(
    "prompt_versions", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("prompt_id", String(128), primary_key=True),
    Column("version", String(64), primary_key=True),
    Column("content_hash", String(64)),  # sha256 of the template, when registered
    Column("template", Text),  # optional; enables diffs between versions
    Column("note", String(1024)),  # what changed
    Column("author", String(128)),
    Column("registered_at", DateTime),  # when CI registered it (None if only seen in traffic)
    Column("first_seen", DateTime),  # first call or step that ran it
    Column("last_seen", DateTime),
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

# One test outcome from an evaluation run: a case (usually a document), what
# was expected, what the pipeline produced, and what the evaluator decided.
eval_results = Table(
    "eval_results", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("result_id", String(128), primary_key=True),
    Column("run_id", String(128), nullable=False, index=True),  # the evaluation run
    Column("case_id", String(128), nullable=False),  # the test case, stable across runs
    Column("document_id", String(128), index=True),  # the pipeline's trace of this case, if sent
    Column("field", String(256)),  # None for a whole-case check
    Column("expected", String(4096)),
    Column("actual", String(4096)),
    Column("status", String(16), nullable=False),  # pass | fail | error (the check itself couldn't run)
    Column("evaluator", String(128)),  # e.g. exact_match@2, llm_judge@1
    Column("score", Float),
    Column("reason", String(2048)),  # the evaluator's explanation or error
    Column("ts", DateTime, nullable=False, index=True),
    Column("attempt", Integer),  # repeated judgements of the same output
    Column("lineage", JSON),  # {"prompt": "extract_fields@v13", "model": ..., "build": ...}
    Column("inputs", JSON),  # what the evaluator saw: {"query", "output", "context", ...}; see assay/audit.py
)

# --- Agents: one trajectory per run of an agent on a task, its steps, and what a case expects ---

agent_trajectories = Table(
    "agent_trajectories", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("trajectory_id", String(128), primary_key=True),  # also its document_id
    Column("run_id", String(128), index=True),  # the evaluation run, if this is a test case
    Column("case_id", String(128)),
    Column("attempt", Integer),
    Column("task", String(128)),  # the kind of task, e.g. refund_request
    Column("started_at", DateTime, nullable=False, index=True),
    Column("finished_at", DateTime),
    Column("answer", Text),  # the final answer
    Column("status", String(32)),  # running | completed | failed | abandoned | …
    Column("lineage", JSON),
    Column("updated_at", DateTime, index=True),  # server time of the latest event; see assay/lifecycle.py
    Column("outcome", String(16)),  # resolved | unresolved | escalated
)

# How each test-case run behaved (assay/behavior.py): cost, latency, steps, context, tools offered,
# outcome, approvals. Compared with the case's baseline, like its checks.
run_metrics = Table(
    "run_metrics", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("metric_id", String(128), primary_key=True),
    Column("run_id", String(128), nullable=False, index=True),  # the test run
    Column("case_id", String(128), nullable=False),
    Column("attempt", Integer),
    Column("trajectory_id", String(128)),
    Column("metrics", JSON, nullable=False),
)

# The latest evaluation of each agent run, made when the run ended (assay/lifecycle.py).
run_checks = Table(
    "run_checks", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("trajectory_id", String(128), primary_key=True),
    Column("evaluated_at", DateTime, nullable=False, index=True),
    Column("status", String(32)),  # the run's status when evaluated: completed | failed | abandoned
    Column("checks", JSON),  # [{"check", "status": pass | fail, "reason"}]
    Column("failed", Integer, nullable=False),  # how many checks failed
)

agent_steps = Table(
    "agent_steps", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("trajectory_id", String(128), primary_key=True),
    Column("seq", Integer, primary_key=True),
    Column("kind", String(16), nullable=False),  # reason | tool | state | answer
    Column("name", String(128)),  # the tool, or the object a state change touched ("order:1001")
    Column("args", JSON),  # tool arguments; for a state change {"op": "create" | "update" | "delete"}
    Column("result", JSON),  # tool result; for a state change, the object after it
    Column("error", String(1024)),
    Column("text", Text),  # reasoning or answer text
    Column("model", String(128)),
    Column("tokens", Integer),
    Column("cost_usd", Float),
    Column("started_at", DateTime),
    Column("finished_at", DateTime),
    Column("parent_seq", Integer),  # a step nested inside another
    Column("tokens_in", Integer),  # a model call's input: how big the context has grown
    Column("tools", JSON),  # the tools a model call was offered
)

# Runs as the v1 event schema describes them (assay/schema.py): what run.start and run.end
# said, kept so steps and outcomes arriving in later batches land in the right place.
runs = Table(
    "runs", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("run_id", String(128), primary_key=True),
    Column("kind", String(16), nullable=False),  # agent | pipeline
    Column("task", String(128)),
    Column("segment", String(128)),
    Column("started_at", DateTime, nullable=False, index=True),
    Column("ended_at", DateTime),
    Column("status", String(16)),  # running | completed | failed | abandoned
    Column("answer", Text),
    Column("error", String(2048)),
    Column("version", JSON),
    Column("test_run", String(128), index=True),
    Column("test_case", String(128)),
    Column("attempt", Integer),
    Column("parent_run_id", String(128)),
    Column("tags", JSON),
    Column("outcome", String(16)),  # resolved | unresolved | escalated
)

# What a test case expects of a trajectory: the tool calls, the answer, and the end state.
agent_references = Table(
    "agent_references", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("case_id", String(128), primary_key=True),
    Column("calls", JSON),  # [{"tool", "args" (partial match), "optional", "any_order"}]
    Column("allow_extra", JSON),  # tools that may be called beyond the expected ones (read-only lookups)
    Column("answer", Text),  # expected answer, or a value it must contain
    Column("answer_match", String(16)),  # contains | equals
    Column("state", JSON),  # [{"object", "exists" | "field" + "equals"}], objects may use * wildcards
    Column("max_steps", Integer),
    Column("updated_at", DateTime, nullable=False),
)

# --- Learning from production: inputs, feedback, proposed test cases, suites ---

# What a trace was given, so a failure can be replayed as a test.
trace_inputs = Table(
    "trace_inputs", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("trace_id", String(128), primary_key=True),  # a document or trajectory id
    Column("input", JSON),  # the request itself (text or structured)
    Column("input_ref", String(1024)),  # or where to fetch it: s3://…/file.pdf
    Column("captured_at", DateTime, nullable=False),
)

# What users did about a trace: thumbs down, a retry, an escalation.
trace_feedback = Table(
    "trace_feedback", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("feedback_id", String(128), primary_key=True),
    Column("trace_id", String(128), nullable=False, index=True),
    Column("kind", String(32), nullable=False),  # thumbs_down | thumbs_up | retry | escalation | complaint
    Column("ts", DateTime, nullable=False, index=True),
    Column("note", String(1024)),
)

# A test case drafted from a production failure, waiting for a developer.
regression_candidates = Table(
    "regression_candidates", metadata,
    Column("id", Integer, primary_key=True),
    Column("source", String(64), nullable=False, index=True),
    Column("pattern", String(512), nullable=False),  # the failure pattern (cluster) it came from
    Column("trace_id", String(128), nullable=False),
    Column("task", String(128)),
    Column("status", String(16), nullable=False),  # proposed | approved | rejected
    Column("case", JSON, nullable=False),  # {"case_id", "input", "input_ref", "reference", "properties"}
    Column("provenance", JSON, nullable=False),  # where each expectation came from
    Column("pii", JSON),  # what personal data the input seems to hold
    Column("created_at", DateTime, nullable=False),
    Column("decided_at", DateTime),
    Column("decided_by", String(128)),
    Column("note", String(1024)),
    UniqueConstraint("source", "trace_id", name="uq_candidate_trace"),
)

# The permanent regression suite: approved cases, each linked to the failure it guards against.
suite_cases = Table(
    "suite_cases", metadata,
    Column("source", String(64), primary_key=True),
    Column("case_id", String(128), primary_key=True),
    Column("suite", String(128), nullable=False, index=True),
    Column("candidate_id", Integer),
    Column("pattern", String(512)),
    Column("origin_trace", String(128)),
    Column("task", String(128)),
    Column("input", JSON),
    Column("input_ref", String(1024)),
    Column("reference", JSON),
    Column("properties", JSON),
    Column("added_at", DateTime, nullable=False),
    Column("added_by", String(128)),
)

# Every production failure pattern seen, and where it is in the loop.
pattern_log = Table(
    "pattern_log", metadata,
    Column("source", String(64), primary_key=True),
    Column("key", String(512), primary_key=True),
    Column("name", String(512)),
    Column("kind", String(16)),  # failure | infrastructure | unusual: only failures become tests
    Column("first_seen", DateTime, nullable=False),
    Column("last_seen", DateTime, nullable=False),
    Column("traces", Integer, nullable=False, default=0),
    Column("status", String(16), nullable=False),  # open | protected | fixed | recurred | dismissed
    Column("protected_at", DateTime),
    Column("fixed_at", DateTime),
    Column("recurred_at", DateTime),
    Column("ticket_id", String(64)),  # the Jira / Linear ticket opened for it
    Column("ticket_url", String(512)),
)

# A person's call on a group of failures: accepted as intended, not a problem, or confirmed.
failure_decisions = Table(
    "failure_decisions", metadata,
    Column("id", Integer, primary_key=True),
    Column("source", String(64), nullable=False, index=True),
    Column("key", String(512), nullable=False),  # the group's signature
    Column("decision", String(32), nullable=False),  # accepted_change | not_a_problem | confirmed
    Column("note", String(512)),
    Column("decided_by", String(128)),
    Column("decided_at", DateTime, nullable=False),
    UniqueConstraint("source", "key", name="uq_failure_decision"),
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


# How long an agent's run can go quiet before it's marked abandoned (assay/lifecycle.py).
# task "*" is the source's default; with neither, the server's ASSAY_ABANDON_MINUTES.
agent_limits = Table(
    "agent_limits", metadata,
    Column("tenant", String(64), primary_key=True),
    Column("task", String(128), primary_key=True),
    Column("abandon_minutes", Float, nullable=False),
    Column("updated_at", DateTime, nullable=False),
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


# Outbound connections set up from the dashboard: Slack, Jira, Linear. Secrets are never
# returned by the API once saved (see assay/integrations.py).
integrations = Table(
    "integrations", metadata,
    Column("source", String(64), primary_key=True),  # "*" = every source
    Column("kind", String(32), primary_key=True),  # slack | jira | linear
    Column("config", JSON, nullable=False),
    Column("enabled", Boolean, nullable=False, default=True),
    Column("updated_at", DateTime, nullable=False),
)


def upgrade(engine: Engine) -> List[str]:
    """Bring an existing database up to this version: create missing tables, and add
    columns that newer versions introduced. Only ever adds (nullable) columns, so it's
    safe to run on every start and never loses data."""
    from sqlalchemy import inspect, text
    metadata.create_all(engine)
    added = []
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in have or col.primary_key:
                    continue
                ddl = col.type.compile(dialect=engine.dialect)
                conn.execute(text(f'ALTER TABLE {table.name} ADD COLUMN "{col.name}" {ddl}'))
                added.append(f"{table.name}.{col.name}")
    return added


def make_engine(url: str) -> Engine:
    kwargs = {"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}
    engine = create_engine(url, **kwargs)
    upgrade(engine)
    return engine
