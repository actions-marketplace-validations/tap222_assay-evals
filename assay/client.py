"""Send pipeline events to Assay from your own code. Standard library only.

    from assay.client import Assay   # or copy this file and: from client import Assay

    assay = Assay("https://assay.example.com", tenant="acme", api_key="...")
    assay.document("inv-123", received_at=start, document_type="invoice", segment="Northwind", page_count=2)
    with assay.stage("inv-123", "field_extraction") as run:   # records timing, status, failures
        result = extract(doc)
    assay.call("inv-123", stage="field_extraction", model_declared="claude-sonnet-5",
               model_served=resp.model, latency_ms=elapsed_ms, cost_usd=price, status="success")
    assay.document("inv-123", received_at=start, completed_at=datetime.utcnow())  # upserts by id
    assay.review("inv-123", minutes=4.5, reviewer="sam")
    assay.flush()   # also happens automatically every `batch_size` records and at exit

Records are buffered and sent in batches. A failed send is retried on the next
flush and never raises into your pipeline unless you pass strict=True.
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

PATHS = {"documents": "/v1/events/documents", "stage_runs": "/v1/events/stage-runs",
         "calls": "/v1/events/calls", "reviews": "/v1/events/reviews", "indexed": "/v1/events/indexed"}


def _jsonable(v):
    return v.isoformat() if isinstance(v, datetime) else v


class Assay:
    def __init__(self, url: str, tenant: str = "default", api_key: Optional[str] = None,
                 batch_size: int = 200, timeout: float = 10.0, strict: bool = False,
                 transport: Optional[Callable[[str, list], None]] = None):
        self.url, self.tenant, self.api_key = url.rstrip("/"), tenant, api_key
        self.batch_size, self.timeout, self.strict = batch_size, timeout, strict
        self._buf: Dict[str, List[dict]] = {k: [] for k in PATHS}
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

    def extraction(self, document_id: str, has_positions: bool, **fields) -> None:
        """One extracted value; has_positions if it carries a source location."""
        self._add("indexed", dict(document_id=document_id, has_positions=has_positions, **fields))

    # ---------- sending ----------

    def _add(self, kind: str, record: dict) -> None:
        with self._lock:
            self._buf[kind].append({k: _jsonable(v) for k, v in record.items()})
            full = len(self._buf[kind]) >= self.batch_size
        if full:
            self.flush()

    def flush(self) -> None:
        # Documents first so calls and reviews always have something to attach to.
        for kind in ("documents", "stage_runs", "calls", "reviews", "indexed"):
            with self._lock:
                batch, self._buf[kind] = self._buf[kind], []
            if not batch:
                continue
            try:
                self._send(PATHS[kind], batch)
            except Exception:
                with self._lock:
                    self._buf[kind] = batch + self._buf[kind]  # keep for the next flush
                if self.strict:
                    raise
                log.warning("Assay: couldn't send %d %s; will retry on next flush", len(batch), kind,
                            exc_info=True)

    def _http(self, path: str, batch: list) -> None:
        headers = {"Content-Type": "application/json", "X-Tenant": self.tenant}
        if self.api_key:
            headers["X-API-Key"] = self.api_key
        req = urllib.request.Request(self.url + path, data=json.dumps(batch).encode(), headers=headers,
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
