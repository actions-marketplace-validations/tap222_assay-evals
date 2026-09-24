from __future__ import annotations

from typing import Iterable, List, Optional, Protocol, Set, Tuple

from assay.models import CallRecord, DocumentRecord, IndexedRecord, ReviewRecord, StageRun, Window


class Source(Protocol):
    """Anything that can hand measures canonical records for a time window.

    A method returning None means the source cannot provide that data at all,
    which measures report as "unmeasured" rather than as zero.
    """

    name: str

    def calls(self, window: Window) -> Optional[Iterable[CallRecord]]: ...

    def documents(self, window: Window) -> Optional[Iterable[DocumentRecord]]: ...

    def stage_runs(self, window: Window) -> Optional[Iterable[StageRun]]: ...

    def indexed(self, window: Window) -> Optional[Iterable[IndexedRecord]]: ...

    def downstream_hashes(self) -> Optional[Set[str]]: ...

    def reviews(self, window: Window) -> Optional[Iterable[ReviewRecord]]:
        """Human review and rework time. None if the source doesn't record it."""
        ...

    def document_detail(self, document_id: str) -> Optional[Tuple[DocumentRecord, List[StageRun], List[CallRecord]]]:
        """One document with its stage runs and AI calls, for tracing. None if unknown."""
        ...
