from datetime import datetime, timedelta

from assay.models import CallRecord, ErrorReport, StageRun
from assay.rootcause import localize, lookup, normalize

T0 = datetime(2026, 9, 1)
TEXT = ("INVOICE INV-2291  Northwind Traders  Date: 01 Sep 2026  Bill to: Contoso  "
        "Line items ... Subtotal 1,100.00  Tax 140.00  Total due $1,240.00  Thank you for your business")


def step(i, stage, outputs=None, status="success", **kw):
    return StageRun("d1", stage, status, started_at=T0 + timedelta(seconds=i), outputs=outputs, **kw)


def err(field="total", expected="1240.00", kind="wrong", observed=None):
    return ErrorReport("e1", "d1", field, T0, expected=expected, observed=observed, kind=kind)


PIPE = lambda extract_total, validate_total=None, text=TEXT: [
    step(0, "ocr", {"_text": text}),
    step(1, "classification", {"document_type": "invoice"}),
    step(2, "field_extraction", {"total": extract_total, "vendor": "Northwind Traders"}),
    step(3, "validation", {"total": validate_total if validate_total is not None else extract_total}),
    step(4, "publish", {"published": True}),
]


def test_normalize_ignores_formatting_not_meaning():
    assert normalize("$1,240.00") == normalize("1240") == normalize(" 1240.0 USD")
    assert normalize("2026-09-01") == normalize("09/01/2026") == normalize("1 Sep 2026") == normalize("Sep 1, 2026")
    assert normalize("Acme  Corp") == normalize("acme corp")
    assert normalize("1240") != normalize("1.24")


def test_lookup_dotted_paths():
    assert lookup({"items": [{"total": 5}]}, "items.0.total") == (True, 5)
    assert lookup({"items": []}, "items.0.total") == (False, None)


def test_introduced_when_the_value_was_in_the_text():
    r = localize(err(), PIPE("1,420.00"))
    assert r["verdict"] == "introduced" and r["origin_stage"] == "field_extraction"
    states = [t["state"] for t in r["timeline"]]
    assert states == ["absent", "absent", "wrong", "wrong", "absent"] and r["timeline"][0]["evidence"]


def test_corrupted_when_a_later_step_breaks_a_correct_value():
    r = localize(err(), PIPE("1,240.00", validate_total="1.24"))
    assert r["verdict"] == "corrupted" and r["origin_stage"] == "validation"
    assert "field_extraction" in r["explanation"] and "'1.24'" in r["explanation"]


def test_upstream_when_the_text_never_had_it():
    r = localize(err(), PIPE("1,100.00", text=TEXT.replace("Total due $1,240.00", "Total due $1,2?0.0?")))
    assert r["verdict"] == "upstream" and r["origin_stage"] == "ocr"


def test_after_the_pipeline_when_every_step_was_right():
    r = localize(err(observed="0"), PIPE("1,240.00"))
    assert r["verdict"] == "after" and r["origin_stage"] is None


def test_dropped_only_when_reported_missing():
    runs = [step(0, "ocr", {"_text": TEXT}), step(1, "field_extraction", {"total": "1240"}),
            step(2, "validation", {"vendor": "x"})]
    assert localize(err(kind="missing"), runs)["verdict"] == "dropped"
    assert localize(err(kind="missing"), runs)["origin_stage"] == "validation"


def test_missing_value_never_extracted_but_in_text():
    runs = [step(0, "ocr", {"_text": TEXT}), step(1, "field_extraction", {"vendor": "x"})]
    r = localize(err(kind="missing"), runs)
    assert r["verdict"] == "introduced" and r["origin_stage"] == "field_extraction"


def test_extra_value_that_should_not_exist():
    r = localize(err(field="po_number", expected=None, kind="extra"),
                 [step(0, "ocr", {"_text": TEXT}), step(1, "field_extraction", {"po_number": "PO-1"})])
    assert r["verdict"] == "introduced" and r["origin_stage"] == "field_extraction"


def test_unlocalized_without_outputs():
    r = localize(err(), [step(0, "ocr"), step(1, "field_extraction")])
    assert r["verdict"] == "unlocalized" and r["origin_stage"] is None


def test_caused_by_an_earlier_error_on_the_same_document():
    runs = PIPE("1,420.00")
    runs[1].outputs = {"document_type": "receipt"}
    r = localize(err(), runs, other_origins={"document_type": (1, True), "total": (2, False)})
    assert r["verdict"] == "caused_by"
    assert r["caused_by"] == {"field": "document_type", "stage": "classification", "index": 1}
    assert r["origin_stage"] == "field_extraction"


def test_sequence_beats_start_time_for_ordering():
    runs = [step(5, "field_extraction", {"total": "1420"}, sequence=2), step(9, "ocr", {"_text": TEXT}, sequence=1)]
    r = localize(err(), runs)
    assert [t["stage"] for t in r["timeline"]] == ["ocr", "field_extraction"] and r["verdict"] == "introduced"


def test_signals_at_the_origin_step():
    calls = [CallRecord("c", "field_extraction", T0, document_id="d1", model_declared="claude-sonnet-5",
                        model_served="claude-haiku-4-5", resolving_layer="fallback_1", gate_reason="timeout")]
    r = localize(err(), PIPE("1,420.00"), calls)
    assert any("served by claude-haiku-4-5" in s for s in r["signals"])
    assert any("fallback tier answered (fallback_1: timeout)" in s for s in r["signals"])


def test_a_wrong_amount_earlier_does_not_explain_a_later_label():
    runs = PIPE("1,420.00")
    r = localize(err(field="vendor", expected="Northwind Traders"), runs, other_origins={"total": (0, False)})
    assert r["verdict"] != "caused_by"


def test_labels_are_judged_where_they_were_decided_not_against_text():
    runs = PIPE("1,240.00")
    runs[1].outputs = {"document_type": "receipt"}
    r = localize(err(field="document_type", expected="bank_statement"), runs)
    assert r["verdict"] == "introduced" and r["origin_stage"] == "classification"


def test_dates_are_found_in_text_however_they_are_written():
    runs = [step(0, "ocr", {"_text": TEXT}), step(1, "field_extraction", {"date": "2026-01-09"})]
    r = localize(err(field="date", expected="2026-09-01"), runs)
    assert r["verdict"] == "introduced" and r["timeline"][0]["evidence"]
