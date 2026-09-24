"""Measures that need labelled ground truth.

Registered now so they appear in the catalog and on the dashboard as
"unmeasured" with the reason, rather than being absent. Each one gets a real
compute() once its ground truth lands (see the roadmap epic in roadmap_ref).
"""
from __future__ import annotations

from assay.measures.base import Measure, MeasureOutput, unmeasured
from assay.models import Window


class _AwaitingTruth(Measure):
    waiting_on: str = ""

    def compute(self, source, window: Window) -> MeasureOutput:
        return unmeasured(self.id, f"Waiting on ground truth: {self.waiting_on}")


class SplitStraightThrough(_AwaitingTruth):
    id = "split_stp"
    roadmap_ref = "Measure 1 · DEV-NEW-2"
    name = "Record-split straight-through"
    question = "What share of multi-record streams split correctly with no human touch?"
    waiting_on = "labelled stream corpus with confirmed split boundaries (DEV-NEW-2)."


class FieldAccuracy(_AwaitingTruth):
    id = "field_accuracy"
    roadmap_ref = "Measure 6 · DEV-NEW-1"
    name = "Severity-weighted field accuracy"
    question = "How often is each indexed field right, weighted by what an error costs?"
    dimensions = ("county", "instrument_type")
    waiting_on = "indexing_gt.v1 schema and a certification split (DEV-NEW-1)."


class SupersededValues(_AwaitingTruth):
    id = "superseded_value_rate"
    roadmap_ref = "DEV-NEW-7"
    name = "Superseded values reaching output"
    question = "How often does a value a later instrument superseded reach output unflagged?"
    higher_is_better = False
    dimensions = ("county",)
    waiting_on = "instrument-chain join logic (DEV-NEW-6/7)."


class EscapeRate(_AwaitingTruth):
    id = "escape_rate"
    roadmap_ref = "DEV-NEW-8"
    name = "Escape rate"
    question = "How often does a wrong value clear both automation and human review?"
    higher_is_better = False
    dimensions = ("county",)
    waiting_on = "retained spot-check sample of published records (DEV-NEW-8)."
