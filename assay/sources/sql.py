"""Read-only adapter over any SQL database a document pipeline writes to.

Assay needs four kinds of records: documents, stage runs, model calls and
extraction rows, plus, optionally, review time (for people cost). A *mapping* says where each lives in your schema: the FROM
clause and one SQL expression per field. Nothing else about your schema is
assumed, so any pipeline that records these things can be connected.

Without a mapping file, the reference schema below is used: tables named
documents, stage_runs, model_calls and extractions whose columns already use
Assay's field names. Supply ASSAY_SOURCE_MAPPING (JSON, same shape as
DEFAULT_MAPPING, see mappings/example.json) to point at your own tables. Any
key you leave out keeps its default; set a field to "NULL" if your schema
doesn't record it, and the measures that need it report "unmeasured".

Run `python -m assay check-source` to test every mapped field against the
live database.
"""
from __future__ import annotations

import copy
import json
import os
from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

from assay.models import CallRecord, DocumentRecord, ErrorReport, IndexedRecord, ReviewRecord, StageRun, Window

DEFAULT_MAPPING: Dict = {
    "documents": {
        "from": "documents d",
        "columns": {
            "document_id": "d.document_id",
            "received_at": "d.received_at",
            "completed_at": "d.completed_at",
            "status": "d.status",
            "processing_mode": "d.processing_mode",
            "file_hash": "d.file_hash",
            "segment": "d.segment",
            "document_type": "d.document_type",
            "page_count": "d.page_count",
        },
    },
    "stage_runs": {
        "from": "stage_runs s",
        "columns": {
            "document_id": "s.document_id",
            "stage": "s.stage",
            "status": "s.status",
            "started_at": "s.started_at",
            "finished_at": "s.finished_at",
            # True when the stage actually processed the document. If your
            # schema can't tell, leave it NULL and use "noop_stages" instead.
            "did_work": "s.did_work",
            # Optional, for error analysis: a JSON column of what the step
            # produced, and the step's position in the pipeline.
            "outputs": "NULL",
            "sequence": "NULL",
        },
    },
    "calls": {
        "from": "model_calls c LEFT JOIN documents d ON d.document_id = c.document_id",
        "columns": {
            "call_id": "c.call_id",
            "stage": "c.stage",
            "ts": "c.ts",
            "document_id": "c.document_id",
            "model_declared": "c.model_declared",
            "model_served": "c.model_served",
            "resolving_layer": "c.resolving_layer",
            "gate_reason": "c.gate_reason",
            "cost_usd": "c.cost_usd",
            "code_revision": "c.code_revision",
            "latency_ms": "c.latency_ms",
            "status": "c.status",
            "segment": "d.segment",
            "document_type": "d.document_type",
        },
    },
    "indexed": {
        "from": "extractions x LEFT JOIN documents d ON d.document_id = x.document_id",
        "columns": {
            "document_id": "x.document_id",
            "has_positions": "(x.source_positions IS NOT NULL)",
            "segment": "d.segment",
            "document_type": "d.document_type",
        },
    },
    # Time people spent on documents. Off by default: set it to
    # {"from": ..., "columns": {review_id, document_id, ts, kind, minutes,
    # cost_usd, reviewer, stage}} to include review and rework in cost.
    "reviews": None,
    # Reported wrong outputs, for error analysis. Off by default: set it to
    # {"from": ..., "columns": {error_id, document_id, field, reported_at,
    # expected, observed, kind, reporter, source}}.
    "errors": None,
    # Stages known to report success without processing anything (placeholders
    # wired into the pipeline but not implemented). Marks them did_work = false.
    "noop_stages": [],
}

# Field each record type is filtered on for a time window.
TIME_FIELD = {"calls": "ts", "documents": "received_at", "stage_runs": "started_at", "indexed": None,
              "reviews": "ts", "errors": "reported_at"}
RECORD_TYPES = ("documents", "stage_runs", "calls", "indexed", "reviews", "errors")


def load_mapping(path: Optional[str] = None) -> Dict:
    """Default mapping with the JSON file at `path` (or ASSAY_SOURCE_MAPPING) layered on top."""
    mapping = copy.deepcopy(DEFAULT_MAPPING)
    path = path or os.environ.get("ASSAY_SOURCE_MAPPING")
    if path:
        with open(path) as f:
            override = json.load(f)
        for key, spec in override.items():
            if key == "noop_stages":
                mapping[key] = list(spec)
            elif key in RECORD_TYPES:
                if spec is None or mapping[key] is None:
                    mapping[key] = copy.deepcopy(spec)  # switching an optional record type on or off
                else:
                    mapping[key]["from"] = spec.get("from", mapping[key]["from"])
                    mapping[key]["columns"].update(spec.get("columns", {}))
            else:
                raise ValueError(f"Unknown mapping key '{key}'. Expected one of "
                                 f"{', '.join(RECORD_TYPES + ('noop_stages',))}.")
    return mapping


class SQLSource:
    name = "sql"

    def __init__(self, url: str, downstream_url: Optional[str] = None,
                 downstream_sql: Optional[str] = None, engine: Optional[Engine] = None,
                 mapping: Optional[Dict] = None):
        self.engine = engine or create_engine(url, pool_pre_ping=True)
        self.downstream_url = downstream_url
        self.downstream_sql = downstream_sql
        self.mapping = mapping or load_mapping()
        self.noop_stages = set(self.mapping.get("noop_stages", []))

    def _select(self, table: str, window: Optional[Window], document_id: Optional[str] = None) -> List[dict]:
        spec = self.mapping[table]
        cols = spec["columns"]
        select = ", ".join(f"{expr} AS {name}" for name, expr in cols.items())
        sql = f"SELECT {select} FROM {spec['from']}"
        where, params = [], {}
        tcol = TIME_FIELD[table]
        if window and tcol and cols.get(tcol, "NULL").upper() != "NULL":
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

    def _run(self, r: dict) -> StageRun:
        did = r.get("did_work")
        if r["stage"] in self.noop_stages:
            did = False
        outputs = r.get("outputs")
        if isinstance(outputs, (str, bytes)):  # JSON stored as text
            try:
                outputs = json.loads(outputs)
            except ValueError:
                outputs = {"_text": outputs if isinstance(outputs, str) else outputs.decode(errors="replace")}
        return StageRun(**{**r, "document_id": str(r["document_id"]), "outputs": outputs,
                           "did_work": None if did is None else bool(did)})

    def errors(self, window: Optional[Window], document_id: Optional[str] = None) -> Optional[List[ErrorReport]]:
        if not self.mapping.get("errors"):
            return None
        return [ErrorReport(**{**r, "error_id": str(r["error_id"]), "document_id": str(r["document_id"])})
                for r in self._select("errors", window, document_id)]

    def calls(self, window: Window) -> Iterable[CallRecord]:
        return [self._call(r) for r in self._select("calls", window)]

    def documents(self, window: Window) -> Iterable[DocumentRecord]:
        return [DocumentRecord(**{**r, "document_id": str(r["document_id"])})
                for r in self._select("documents", window)]

    def stage_runs(self, window: Window) -> Iterable[StageRun]:
        return [self._run(r) for r in self._select("stage_runs", window)]

    def indexed(self, window: Window) -> Iterable[IndexedRecord]:
        return [IndexedRecord(**{**r, "document_id": str(r["document_id"]),
                                 "has_positions": bool(r["has_positions"])})
                for r in self._select("indexed", None)]

    def reviews(self, window: Window) -> Optional[Iterable[ReviewRecord]]:
        if not self.mapping.get("reviews"):
            return None
        num = lambda v: None if v is None else float(v)
        return [ReviewRecord(**{**r, "review_id": str(r["review_id"]), "document_id": str(r["document_id"]),
                                "minutes": num(r.get("minutes")), "cost_usd": num(r.get("cost_usd"))})
                for r in self._select("reviews", window)]

    def document_detail(self, document_id: str) -> Optional[Tuple[DocumentRecord, List[StageRun], List[CallRecord]]]:
        docs = self._select("documents", None, document_id)
        if not docs:
            return None
        doc = DocumentRecord(**{**docs[0], "document_id": str(docs[0]["document_id"])})
        return (doc, [self._run(r) for r in self._select("stage_runs", None, document_id)],
                [self._call(r) for r in self._select("calls", None, document_id)])

    def downstream_hashes(self) -> Optional[Set[str]]:
        """file_hash values present in the downstream record system.

        Pipeline and downstream systems rarely share IDs, so the handoff check
        joins on file_hash. Returns None when no downstream database is configured.
        """
        if not (self.downstream_url and self.downstream_sql):
            return None
        eng = create_engine(self.downstream_url)
        with eng.connect() as conn:
            return {str(r[0]) for r in conn.execute(text(self.downstream_sql)) if r[0]}

    def check(self) -> Dict[str, Dict[str, Optional[str]]]:
        """Try every mapped field against the live database.

        Returns {record_type: {field: None if it works, else the error}}. Works on
        any database because it runs each expression rather than reading the catalog.
        """
        report: Dict[str, Dict[str, Optional[str]]] = {}
        for table in RECORD_TYPES:
            spec = self.mapping[table]
            if not spec:
                continue  # optional record type, not configured
            report[table] = {}
            for name, expr in spec["columns"].items():
                try:
                    with self.engine.connect() as conn:
                        conn.execute(text(f"SELECT {expr} FROM {spec['from']} LIMIT 0"))
                    report[table][name] = None
                except Exception as exc:
                    report[table][name] = str(getattr(exc, "orig", exc)).splitlines()[0]
        return report
