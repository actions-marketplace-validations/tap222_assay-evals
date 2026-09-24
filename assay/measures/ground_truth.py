"""Measures that need labelled ground truth.

Registered now so they appear in the catalog and on the dashboard as
"unmeasured" with the reason, rather than being absent. Each one gets a real
compute() once the labels it needs can be ingested.
"""
from __future__ import annotations

from assay.measures.base import Measure, MeasureOutput, unmeasured
from assay.models import Window


class _AwaitingTruth(Measure):
    tag = "Accuracy"
    waiting_on: str = ""

    def compute(self, source, window: Window) -> MeasureOutput:
        return unmeasured(self.id, f"Needs ground truth: {self.waiting_on}")


class SplitStraightThrough(_AwaitingTruth):
    id = "split_stp"
    name = "Document splitting straight-through"
    question = "What share of multi-document files split correctly with no human touch?"
    waiting_on = "files with human-confirmed document boundaries."


class FieldAccuracy(_AwaitingTruth):
    id = "field_accuracy"
    name = "Severity-weighted field accuracy"
    question = "How often is each extracted field right, weighted by what an error costs?"
    dimensions = ("segment", "document_type")
    waiting_on = "a labelled evaluation set with correct values per field."


class SupersededValues(_AwaitingTruth):
    id = "superseded_value_rate"
    name = "Superseded values reaching output"
    question = "How often does a value that a later document replaced reach output unflagged?"
    higher_is_better = False
    dimensions = ("segment",)
    waiting_on = "links between documents that amend or replace each other."


class EscapeRate(_AwaitingTruth):
    id = "escape_rate"
    name = "Escape rate"
    question = "How often does a wrong value clear both automation and human review?"
    higher_is_better = False
    dimensions = ("segment",)
    waiting_on = "a re-verified spot-check sample of published output."
