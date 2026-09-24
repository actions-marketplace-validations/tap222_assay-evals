"""Source over events pushed to Assay's ingest API (the multi-tenant path)."""
from __future__ import annotations

from typing import Iterable, List, Optional, Set, Tuple

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store
from assay.models import CallRecord, DocumentRecord, IndexedRecord, StageRun, Window


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
        return [StageRun(**{k: v for k, v in r.items() if k not in ("tenant", "id")})
                for r in self._rows(t, t.c.started_at, window)]

    def indexed(self, window: Window) -> Iterable[IndexedRecord]:
        t = store.event_indexed
        return [IndexedRecord(**{k: v for k, v in r.items() if k not in ("tenant", "id")})
                for r in self._rows(t)]

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
        drop = ("tenant", "id", "delivered_downstream")
        clean = lambda row: {k: v for k, v in row._mapping.items() if k not in drop}
        return (DocumentRecord(**clean(doc)), [StageRun(**clean(x)) for x in runs],
                [CallRecord(**clean(x)) for x in calls])

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
