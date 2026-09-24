from assay.measures.base import Measure, MeasureOutput, SliceResult
from assay.measures.ground_truth import EscapeRate, FieldAccuracy, SplitStraightThrough, SupersededValues
from assay.measures.operations import (CallErrorRate, CallLatencyP95, DocumentVolume, InputMixDrift,
                                       StageFailureRate)
from assay.measures.pipeline import (CostCoverage, FallbackAttribution, HandoffLoss, ModelMismatch,
                                     NoOpStages, RevisionCoverage, SourcePositions, TimeToComplete)

REGISTRY = {m.id: m for m in [
    # operational health
    DocumentVolume(), StageFailureRate(), CallErrorRate(), CallLatencyP95(), TimeToComplete(),
    InputMixDrift(),
    # pipeline integrity
    FallbackAttribution(), ModelMismatch(), CostCoverage(), RevisionCoverage(),
    NoOpStages(), SourcePositions(), HandoffLoss(),
    # needs ground truth
    SplitStraightThrough(), FieldAccuracy(), SupersededValues(), EscapeRate(),
]}

GROUPS = {
    "Operational health": ["document_volume", "stage_failure_rate", "call_error_rate",
                           "call_latency_p95", "time_to_complete_p90", "input_mix_drift"],
    "Pipeline integrity": ["fallback_attribution", "model_mismatch", "cost_coverage", "revision_coverage",
                           "noop_stage_rate", "source_positions", "handoff_loss"],
    "Accuracy (needs ground truth)": ["split_stp", "field_accuracy", "superseded_value_rate", "escape_rate"],
}

__all__ = ["REGISTRY", "GROUPS", "Measure", "MeasureOutput", "SliceResult"]
