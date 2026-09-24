"""Send pipeline events to Assay from your own code. Standard library only.

    from assay.client import Assay   # or copy this file and: from client import Assay

    assay = Assay("https://assay.example.com", api_key="ak_...")   # an `ingest` key for your tenant
    assay.document("inv-123", received_at=start, document_type="invoice", segment="Northwind", page_count=2)
    with assay.stage("inv-123", "field_extraction") as run:   # records timing, status, failures
        result = extract(doc)
    assay.call("inv-123", stage="field_extraction", model_declared="claude-sonnet-5",
               model_served=resp.model, latency_ms=elapsed_ms, cost_usd=price, status="success")
    assay.document("inv-123", received_at=start, completed_at=datetime.utcnow())  # upserts by id
    assay.review("inv-123", minutes=4.5, reviewer="sam")
    assay.flush()   # also happens automatically every `batch_size` records and at exit

Records are buffered and sent together to POST /v1/events, one request per
flush. Every record has an id, so a retried batch never duplicates anything.
A failed send is kept and retried on the next flush, and never raises into
your pipeline unless you pass strict=True. The key decides the tenant; pass
`tenant` only with a platform key.
"""
from __future__ import annotations

import atexit
import json
import logging
import threading
import time
import urllib.request
import uuid
from contextlib import contextmanager
from datetime import datetime
from typing import Callable, Dict, List, Optional

log = logging.getLogger("assay.client")

KINDS = ("documents", "stage_runs", "calls", "reviews", "extractions")
MAX_PER_REQUEST = 5000


def _jsonable(v):
    return v.isoformat() if isinstance(v, datetime) else v


class Assay:
    def __init__(self, url: str, api_key: Optional[str] = None, tenant: Optional[str] = None,
                 batch_size: int = 500, timeout: float = 10.0, strict: bool = False,
                 transport: Optional[Callable[[str, dict], None]] = None):
        self.url, self.tenant, self.api_key = url.rstrip("/"), tenant, api_key
        self.batch_size, self.timeout, self.strict = batch_size, timeout, strict
        self._buf: Dict[str, List[dict]] = {k: [] for k in KINDS}
        self._lock = threading.Lock()
        self._send = transport or self._http
        atexit.register(self.flush)

    # ---------- records ----------

    def document(self, document_id: str, received_at: datetime, **fields) -> None:
        """completed_at, status, processing_mode, file_hash, segment, document_type,
        page_count, delivered_downstream. Sending the same id again updates it."""
        self._add("documents", dict(document_id=document_id, received_at=received_at, **fields))

    def stage_run(self, document_id: str, stage: str, status: str, **fields) -> None:
        """started_at, finished_at, did_work."""
        fields.setdefault("run_id", uuid.uuid4().hex)
        self._add("stage_runs", dict(document_id=document_id, stage=stage, status=status, **fields))

    @contextmanager
    def stage(self, document_id: str, stage: str, did_work: Optional[bool] = True):
        """Time a stage; records "failed" (and re-raises) if the block raises."""
        started, status = datetime.utcnow(), "success"
        try:
            yield
        except Exception:
            status = "failed"
            raise
        finally:
            self.stage_run(document_id, stage, status, started_at=started, finished_at=datetime.utcnow(),
                           did_work=did_work)

    def call(self, document_id: Optional[str], stage: str, call_id: Optional[str] = None,
             ts: Optional[datetime] = None, **fields) -> None:
        """model_declared, model_served, resolving_layer, gate_reason, cost_usd,
        latency_ms, status, code_revision, segment, document_type."""
        self._add("calls", dict(call_id=call_id or uuid.uuid4().hex, document_id=document_id, stage=stage,
                                ts=ts or datetime.utcnow(), **fields))

    def review(self, document_id: str, minutes: Optional[float] = None, kind: str = "review",
               review_id: Optional[str] = None, ts: Optional[datetime] = None, **fields) -> None:
        """kind is review or rework; cost_usd, reviewer, stage."""
        self._add("reviews", dict(review_id=review_id or uuid.uuid4().hex, document_id=document_id, kind=kind,
                                  minutes=minutes, ts=ts or datetime.utcnow(), **fields))

    def extraction(self, document_id: str, has_positions: bool, field: Optional[str] = None, **fields) -> None:
        """One extracted value; has_positions if it carries a source location."""
        self._add("extractions", dict(document_id=document_id, has_positions=has_positions, field=field, **fields))

    # ---------- sending ----------

    def _add(self, kind: str, record: dict) -> None:
        with self._lock:
            self._buf[kind].append({k: _jsonable(v) for k, v in record.items()})
            full = sum(len(b) for b in self._buf.values()) >= self.batch_size
        if full:
            self.flush()

    def flush(self) -> None:
        with self._lock:
            pending, self._buf = self._buf, {k: [] for k in KINDS}
        # Send in chunks under the server's per-request limit; documents go first
        # because the server writes each request's documents before its other records.
        while any(pending.values()):
            payload, room = {}, MAX_PER_REQUEST
            for k in KINDS:
                take, pending[k] = pending[k][:room], pending[k][room:]
                if take:
                    payload[k] = take
                    room -= len(take)
            try:
                self._send("/v1/events", payload)
            except Exception:
                with self._lock:  # keep everything unsent for the next flush
                    for k in KINDS:
                        self._buf[k] = payload.get(k, []) + pending[k] + self._buf[k]
                if self.strict:
                    raise
                log.warning("Assay: couldn't send %d records; will retry on next flush",
                            sum(len(v) for v in payload.values()), exc_info=True)
                return

    def _http(self, path: str, payload: dict) -> None:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        if self.tenant:
            headers["X-Tenant"] = self.tenant
        req = urllib.request.Request(self.url + path, data=json.dumps(payload).encode(), headers=headers,
                                     method="POST")
        for attempt in range(3):
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    resp.read()
                return
            except urllib.error.HTTPError as exc:
                if exc.code < 500:
                    raise  # a bad record won't get better by retrying
                if attempt == 2:
                    raise
            except OSError:
                if attempt == 2:
                    raise
            time.sleep(0.5 * 2 ** attempt)
