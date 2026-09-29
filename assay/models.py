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


def prompt_label(prompt_id: Optional[str], version: Optional[str]) -> Optional[str]:
    if not prompt_id and not version:
        return None
    return f"{prompt_id or '(unnamed)'}@{version or '(unversioned)'}"


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
    prompt_id: Optional[str] = None  # which prompt, e.g. "extract_invoice_fields"
    prompt_version: Optional[str] = None  # a label ("v13") or a content hash

    @property
    def prompt(self) -> Optional[str]:
        """"prompt_id@version", the slice used to compare prompt versions."""
        return prompt_label(self.prompt_id, self.prompt_version)


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
    facets: Optional[dict] = None  # what the document is like, to slice robustness by (source, language, ...)


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
    # What the step produced, as named values: {"document_type": "invoice",
    # "total": "1,240.00", "_text": "<OCR text>"}. Long strings count as text
    # evidence (was the right value available here?). Used for error analysis.
    outputs: Optional[dict] = None
    # Position of the step in the pipeline, if known; otherwise start time orders steps.
    sequence: Optional[int] = None
    # The prompt this step ran, if it's an LLM step (also taken from its calls).
    prompt_id: Optional[str] = None
    prompt_version: Optional[str] = None

    @property
    def prompt(self) -> Optional[str]:
        return prompt_label(self.prompt_id, self.prompt_version)


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


@dataclass
class ErrorReport:
    """Someone found an output value wrong: a reviewer, QA, or a customer."""
    error_id: str
    document_id: str
    field: str
    reported_at: datetime
    expected: Optional[str] = None  # the correct value; None if the value shouldn't exist at all
    observed: Optional[str] = None  # what the pipeline output
    kind: str = "wrong"  # wrong | missing | extra
    reporter: Optional[str] = None
    source: Optional[str] = None  # review | qa | customer | …


@dataclass(frozen=True)
class Window:
    start: datetime
    end: datetime

    def previous(self) -> "Window":
        """The window of equal length immediately before this one."""
        return Window(self.start - (self.end - self.start), self.start)


# Statuses that count as a failure, whichever source they come from.
FAILED_STATUSES = {"failed", "error", "permanently_failed", "timeout"}
