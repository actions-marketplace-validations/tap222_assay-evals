"""Source over events pushed to Assay's ingest API (the multi-tenant path)."""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Set, Tuple

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store
from assay.models import CallRecord, DocumentRecord, ErrorReport, IndexedRecord, ReviewRecord, StageRun, Window


class EventsSource:
    def __init__(self, engine: Engine, tenant: str):
        self.engine = engine
        self.tenant = tenant
        self.name = f"events:{tenant}"

    def _rows(self, table, time_col=None, window: Optional[Window] = None):
        cond = [table.c.tenant == self.tenant]
        if time_col is not None and window is not None:
            cond += [time_col >= window.start, time_col < window.end]
        with self.engine.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(select(table).where(and_(*cond)))]

    def calls(self, window: Window) -> Iterable[CallRecord]:
        t = store.event_calls
        return [CallRecord(**{k: v for k, v in r.items() if k != "tenant"})
                for r in self._rows(t, t.c.ts, window)]

    def documents(self, window: Window) -> Iterable[DocumentRecord]:
        t = store.event_documents
        return [DocumentRecord(**{k: v for k, v in r.items() if k not in ("tenant", "delivered_downstream")})
                for r in self._rows(t, t.c.received_at, window)]

    def stage_runs(self, window: Window) -> Iterable[StageRun]:
        t = store.event_stage_runs
        return [StageRun(**{k: v for k, v in r.items() if k not in ("tenant", "run_id")})
                for r in self._rows(t, t.c.started_at, window)]

    def indexed(self, window: Window) -> Iterable[IndexedRecord]:
        t = store.event_indexed
        return [IndexedRecord(**{k: v for k, v in r.items() if k not in ("tenant", "extraction_id", "field")})
                for r in self._rows(t)]

    def reviews(self, window: Window) -> Optional[Iterable[ReviewRecord]]:
        """None if the tenant has never sent a review, so people cost reads as
        "not recorded" rather than "zero"."""
        t = store.event_reviews
        with self.engine.connect() as conn:
            if conn.execute(select(t.c.review_id).where(t.c.tenant == self.tenant).limit(1)).first() is None:
                return None
        return [ReviewRecord(**{k: v for k, v in r.items() if k != "tenant"})
                for r in self._rows(t, t.c.ts, window)]

    def errors(self, window: Optional[Window], document_id: Optional[str] = None) -> Optional[List[ErrorReport]]:
        """Reported wrong outputs. None if this tenant has never reported one."""
        t = store.event_errors
        with self.engine.connect() as conn:
            if conn.execute(select(t.c.error_id).where(t.c.tenant == self.tenant).limit(1)).first() is None:
                return None
            cond = [t.c.tenant == self.tenant]
            if window is not None:
                cond += [t.c.reported_at >= window.start, t.c.reported_at < window.end]
            if document_id is not None:
                cond.append(t.c.document_id == document_id)
            rows = conn.execute(select(t).where(and_(*cond)).order_by(t.c.reported_at)).all()
        return [ErrorReport(**{k: v for k, v in r._mapping.items() if k != "tenant"}) for r in rows]

    def document_detail(self, document_id: str) -> Optional[Tuple[DocumentRecord, List[StageRun], List[CallRecord]]]:
        d, r, c = store.event_documents, store.event_stage_runs, store.event_calls
        with self.engine.connect() as conn:
            doc = conn.execute(select(d).where(and_(d.c.tenant == self.tenant, d.c.document_id == document_id))).first()
            if not doc:
                return None
            runs = conn.execute(select(r).where(and_(r.c.tenant == self.tenant, r.c.document_id == document_id))
                                .order_by(r.c.started_at)).all()
            calls = conn.execute(select(c).where(and_(c.c.tenant == self.tenant, c.c.document_id == document_id))
                                 .order_by(c.c.ts)).all()
        drop = ("tenant", "run_id", "delivered_downstream")
        clean = lambda row: {k: v for k, v in row._mapping.items() if k not in drop}
        return (DocumentRecord(**clean(doc)), [StageRun(**clean(x)) for x in runs],
                [CallRecord(**clean(x)) for x in calls])

    def document_details(self, document_ids: List[str]) -> Dict[str, Tuple[DocumentRecord, List[StageRun], List[CallRecord]]]:
        """document_detail for many documents in three queries."""
        if not document_ids:
            return {}
        d, r, c = store.event_documents, store.event_stage_runs, store.event_calls
        drop = ("tenant", "run_id", "delivered_downstream")
        clean = lambda row: {k: v for k, v in row._mapping.items() if k not in drop}
        out: Dict[str, list] = {}
        with self.engine.connect() as conn:
            for i in range(0, len(document_ids), 500):
                ids = document_ids[i:i + 500]
                for row in conn.execute(select(d).where(and_(d.c.tenant == self.tenant, d.c.document_id.in_(ids)))):
                    out[row.document_id] = [DocumentRecord(**clean(row)), [], []]
                for row in conn.execute(select(r).where(and_(r.c.tenant == self.tenant, r.c.document_id.in_(ids)))
                                        .order_by(r.c.started_at)):
                    if row.document_id in out:
                        out[row.document_id][1].append(StageRun(**clean(row)))
                for row in conn.execute(select(c).where(and_(c.c.tenant == self.tenant, c.c.document_id.in_(ids)))
                                        .order_by(c.c.ts)):
                    if row.document_id in out:
                        out[row.document_id][2].append(CallRecord(**clean(row)))
        return {k: tuple(v) for k, v in out.items()}

    def downstream_hashes(self) -> Optional[Set[str]]:
        """Hashes of documents the tenant reported as delivered downstream.

        None if the tenant has never reported delivery at all, so the handoff
        measure says "unmeasured" instead of "everything was lost".
        """
        rows = self._rows(store.event_documents)
        reported = [r for r in rows if r["delivered_downstream"] is not None]
        if not reported:
            return None
        return {r["file_hash"] for r in reported if r["delivered_downstream"] and r["file_hash"]}
