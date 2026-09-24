"""Canonical records every source adapter produces.

Measures only ever see these types, never a source's own tables. That is what
lets the same measure run against a pipeline's own database or a
customer's pushed events tomorrow.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Slice value used when a source does not record a dimension. Kept as a real
# slice so "we don't know the segment" is visible instead of silently dropped.
UNRECORDED = "(unrecorded)"


@dataclass
class CallRecord:
    """One AI model call made by a pipeline stage."""
    call_id: str
    stage: str
    ts: datetime
    document_id: Optional[str] = None
    model_declared: Optional[str] = None
    model_served: Optional[str] = None
    resolving_layer: Optional[str] = None
    gate_reason: Optional[str] = None
    cost_usd: Optional[float] = None
    code_revision: Optional[str] = None
    segment: Optional[str] = None
    document_type: Optional[str] = None
    latency_ms: Optional[float] = None
    status: Optional[str] = None  # e.g. success / error / timeout


@dataclass
class DocumentRecord:
    """One document's trip through the pipeline."""
    document_id: str
    received_at: datetime
    completed_at: Optional[datetime] = None
    status: Optional[str] = None
    processing_mode: Optional[str] = None  # e.g. realtime / batch
    file_hash: Optional[str] = None
    segment: Optional[str] = None
    document_type: Optional[str] = None
    page_count: Optional[int] = None


@dataclass
class StageRun:
    """One execution of one pipeline stage on one document."""
    document_id: str
    stage: str
    status: str
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    # False when the stage reported success without doing any work (a stub).
    # None when the source cannot tell.
    did_work: Optional[bool] = None


@dataclass
class IndexedRecord:
    """One indexed (structured-field) output row."""
    document_id: str
    has_positions: bool
    segment: Optional[str] = None
    document_type: Optional[str] = None


@dataclass
class ReviewRecord:
    """Time a person spent on a document: a review, or rework after an error."""
    review_id: str
    document_id: str
    ts: datetime
    kind: str = "review"  # review | rework
    minutes: Optional[float] = None
    cost_usd: Optional[float] = None  # if set, used as-is instead of minutes x rate
    reviewer: Optional[str] = None
    stage: Optional[str] = None


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def previous(self) -> "Window":
        """The window of equal length immediately before this one."""
        return Window(self.start - (self.end - self.start), self.start)


# Statuses that count as a failure, whichever source they come from.
FAILED_STATUSES = {"failed", "error", "permanently_failed", "timeout"}
