"""Error analysis: how often outputs are reported wrong, and which step it started at."""
from __future__ import annotations

from assay.measures.base import Measure, MeasureOutput, SliceResult, ratio_by_slice, unmeasured
from assay.models import UNRECORDED, Window
from assay.rootcause import VERDICTS, summarize


class ReportedErrorRate(Measure):
    id = "reported_error_rate"
    tag = "Errors"
    name = "Reported error rate"
    question = "What share of documents had an output reported wrong?"
    higher_is_better = False
    dimensions = ("document_type", "segment")

    def compute(self, source, window: Window) -> MeasureOutput:
        errors = source.errors(window) if hasattr(source, "errors") else None
        if errors is None:
            return unmeasured(self.id, "No error reports from this source yet.")
        docs = source.documents(window)
        if docs is None:
            return unmeasured(self.id, "Source does not provide documents.")
        wrong = {e.document_id for e in errors}
        out = ratio_by_slice(self.id, docs, hit=lambda d: d.document_id in wrong, dimensions=self.dimensions)
        if out.overall:
            out.overall.note = "Reports arrive after processing, so the latest window undercounts."
        return out


class ErrorsByOrigin(Measure):
    id = "errors_by_origin"
    tag = "Errors"
    name = "Errors by origin step"
    question = "Which pipeline step do reported errors start at?"
    unit = "count"
    higher_is_better = False
    dimensions = ("origin_stage", "origin_prompt", "verdict", "field")

    def compute(self, source, window: Window) -> MeasureOutput:
        s = summarize(source, window)
        if s is None:
            return unmeasured(self.id, "No error reports from this source yet.")
        # Every known step and verdict gets a row, zero included: a step with no
        # errors so far needs that history for a sudden jump to raise an alert.
        origin = {st: 0 for st in s["stages"]} | dict(s["by_origin_stage"])
        verdict = {label: 0 for label in VERDICTS.values()} | {VERDICTS.get(k, k): v for k, v in s["by_verdict"]}
        counts = {"origin_stage": origin, "field": dict(s["by_field"]), "verdict": verdict,
                  "origin_prompt": {k: v for k, v in s["by_origin_prompt"] if k != "(none)"}}
        results = [SliceResult(None, None, float(s["errors"]), s["errors"],
                               note=f"{s['documents_with_errors']:,} documents with a reported error")]
        for dim in self.dimensions:
            for val, n in sorted(counts[dim].items()):
                results.append(SliceResult(dim, UNRECORDED if val == "(none)" and dim != "origin_stage"
                                           else ("(not localized)" if val == "(none)" else val), float(n), n))
        return MeasureOutput(self.id, "measured", results)


class PromptErrorRate(Measure):
    id = "prompt_error_rate"
    tag = "Prompts"
    name = "Error rate by prompt version"
    question = "Of the documents each prompt version handled, how many had an error that started at its step?"
    higher_is_better = False
    dimensions = ("prompt",)

    def compute(self, source, window: Window) -> MeasureOutput:
        from assay.prompts import analyze  # local: prompts imports measures
        s = summarize(source, window, include_all=True) if hasattr(source, "errors") else None
        if s is None:
            return unmeasured(self.id, "No error reports from this source yet.")
        versions = [v for p in analyze(source, window, error_summary=s)["prompts"] for v in p["versions"]]
        if not versions:
            return unmeasured(self.id, "No calls or steps record a prompt version.")
        docs = sum(v["documents"] for v in versions)
        bad = sum(v["error_documents"] for v in versions)
        results = [SliceResult(None, None, bad / docs if docs else None, docs, bad, docs,
                               note=f"{len(versions)} prompt versions active")]
        results += [SliceResult("prompt", v["prompt"], v["error_rate"], v["documents"], v["error_documents"],
                                v["documents"], note=", ".join(v["stages"])) for v in versions]
        return MeasureOutput(self.id, "measured", results)
