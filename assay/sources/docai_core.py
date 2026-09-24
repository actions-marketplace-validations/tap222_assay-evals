"""Read-only adapter over the DocAI Core Postgres schema.

Table names come from the DocAI Core architecture reference (documents,
document_stage_executions, ai_api_calls, indexed_data). Column names that the
reference does not spell out are best guesses, so every one lives in MAPPING
below and can be overridden with a JSON file (ASSAY_DOCAI_MAPPING). Run
`python -m assay check-source` to see which mapped columns actually exist.

A mapping value is a SQL expression evaluated against the aliases in the
query (c = ai_api_calls, d = documents, e = document_stage_executions,
i = indexed_data). Use "NULL" for anything the schema does not record; the
measure that needs it then reports "unmeasured".
"""
from __future__ import annotations

import json
import os
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from assay.models import CallRecord, DocumentRecord, IndexedRecord, StageRun, Window

SCHEMA = "docai"

# Stages wired with stage_processor_func=None: they report success and do no
# work (see the roadmap's Measure 7 and Measure 8 findings).
STUB_STAGES = {"recordability_checks", "highlighting", "redaction"}

MAPPING: Dict[str, Dict[str, str]] = {
    "calls": {
        "call_id": "c.id",
        "stage": "c.stage_name",
        "ts": "c.created_at",
        "document_id": "c.document_id",
        "model_declared": "c.model_requested",
        "model_served": "c.model_used",
        "resolving_layer": "c.resolving_layer",
        "gate_reason": "c.gate_reason",
        "cost_usd": "c.estimated_cost",
        "code_revision": "c.code_revision",
        "county": "d.county",
        "instrument_type": "d.document_type",
        "latency_ms": "c.duration_ms",
        "status": "c.status",
    },
    "documents": {
        "document_id": "d.id",
        "received_at": "d.created_at",
        "completed_at": "d.completed_at",
        "status": "d.status",
        "processing_mode": "d.processing_mode",
        "file_hash": "d.file_hash",
        "county": "d.county",
        "instrument_type": "d.document_type",
    },
    "stage_runs": {
        "document_id": "e.document_id",
        "stage": "e.stage_name",
        "status": "e.status",
        "started_at": "e.started_at",
        "finished_at": "e.completed_at",
    },
    "indexed": {
        "document_id": "i.document_id",
        "has_positions": "(i.indexed_positions IS NOT NULL)",
        "county": "d.county",
        "instrument_type": "d.document_type",
    },
}

FROM = {
    "calls": f"{SCHEMA}.ai_api_calls c LEFT JOIN {SCHEMA}.documents d ON d.id = c.document_id",
    "documents": f"{SCHEMA}.documents d",
    "stage_runs": f"{SCHEMA}.document_stage_executions e",
    "indexed": f"{SCHEMA}.indexed_data i LEFT JOIN {SCHEMA}.documents d ON d.id = i.document_id",
}

TIME_COLUMN = {"calls": "ts", "documents": "received_at", "stage_runs": "started_at", "indexed": None}


def load_mapping() -> Dict[str, Dict[str, str]]:
    path = os.environ.get("ASSAY_DOCAI_MAPPING")
    mapping = {k: dict(v) for k, v in MAPPING.items()}
    if path:
        with open(path) as f:
            for table, cols in json.load(f).items():
                mapping.setdefault(table, {}).update(cols)
    return mapping


class DocAICoreSource:
    name = "docai_core"

    def __init__(self, url: str, downstream_url: Optional[str] = None,
                 downstream_sql: Optional[str] = None, engine: Optional[Engine] = None):
        self.engine = engine or create_engine(url, pool_pre_ping=True)
        self.downstream_url = downstream_url
        self.downstream_sql = downstream_sql
        self.mapping = load_mapping()

    def _select(self, table: str, window: Optional[Window], document_id: Optional[str] = None) -> List[dict]:
        cols = self.mapping[table]
        select = ", ".join(f"{expr} AS {name}" for name, expr in cols.items())
        sql = f"SELECT {select} FROM {FROM[table]}"
        where, params = [], {}
        tcol = TIME_COLUMN[table]
        if window and tcol and cols.get(tcol, "NULL") != "NULL":
            where.append(f"{cols[tcol]} >= :start AND {cols[tcol]} < :end")
            params.update(start=window.start, end=window.end)
        if document_id is not None:
            where.append(f"CAST({cols['document_id']} AS TEXT) = :doc")
            params["doc"] = document_id
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "postgresql":
                # Belt and braces: this service must never write to the pipeline DB.
                conn.execute(text("SET TRANSACTION READ ONLY"))
            return [dict(r._mapping) for r in conn.execute(text(sql), params)]

    @staticmethod
    def _call(r: dict) -> CallRecord:
        num = lambda v: None if v is None else float(v)
        return CallRecord(**{**r, "call_id": str(r["call_id"]),
                             "document_id": None if r["document_id"] is None else str(r["document_id"]),
                             "cost_usd": num(r["cost_usd"]), "latency_ms": num(r["latency_ms"])})

    @staticmethod
    def _run(r: dict) -> StageRun:
        return StageRun(**{**r, "document_id": str(r["document_id"])}, did_work=r["stage"] not in STUB_STAGES)

    def calls(self, window: Window) -> Iterable[CallRecord]:
        return [self._call(r) for r in self._select("calls", window)]

    def documents(self, window: Window) -> Iterable[DocumentRecord]:
        return [DocumentRecord(**{**r, "document_id": str(r["document_id"])})
                for r in self._select("documents", window)]

    def stage_runs(self, window: Window) -> Iterable[StageRun]:
        return [self._run(r) for r in self._select("stage_runs", window)]

    def document_detail(self, document_id: str) -> Optional[Tuple[DocumentRecord, List[StageRun], List[CallRecord]]]:
        docs = self._select("documents", None, document_id)
        if not docs:
            return None
        doc = DocumentRecord(**{**docs[0], "document_id": str(docs[0]["document_id"])})
        return (doc, [self._run(r) for r in self._select("stage_runs", None, document_id)],
                [self._call(r) for r in self._select("calls", None, document_id)])

    def indexed(self, window: Window) -> Iterable[IndexedRecord]:
        return [IndexedRecord(**{**r, "document_id": str(r["document_id"]),
                                 "has_positions": bool(r["has_positions"])})
                for r in self._select("indexed", None)]

    def downstream_hashes(self) -> Optional[Set[str]]:
        """file_hash values present in the downstream record system.

        The two systems don't share IDs, so the handoff check joins on
        file_hash. Returns None when no downstream database is configured.
        """
        if not (self.downstream_url and self.downstream_sql):
            return None
        eng = create_engine(self.downstream_url)
        with eng.connect() as conn:
            return {str(r[0]) for r in conn.execute(text(self.downstream_sql)) if r[0]}

    def check(self) -> Dict[str, Dict[str, bool]]:
        """Report which mapped columns exist in the live schema."""
        insp = inspect(self.engine)
        tables = {
            "c": "ai_api_calls", "d": "documents",
            "e": "document_stage_executions", "i": "indexed_data",
        }
        existing = {}
        for alias, t in tables.items():
            try:
                existing[alias] = {col["name"] for col in insp.get_columns(t, schema=SCHEMA)}
            except Exception:
                existing[alias] = set()
        report: Dict[str, Dict[str, bool]] = {}
        for table, cols in self.mapping.items():
            report[table] = {}
            for name, expr in cols.items():
                refs = [tok for tok in expr.replace("(", " ").replace(")", " ").split() if "." in tok]
                report[table][name] = all(
                    tok.split(".", 1)[1] in existing.get(tok.split(".", 1)[0], set()) for tok in refs
                ) if refs else expr.upper() != "NULL"
        return report
