import pytest
from assay.failures import name_cause_with_llm, name_causes


def test_name_cause_with_llm_updates_name_only():
    group = {
        "key": "ai|introduced|ocr|date",
        "name": "OCR gets date wrong",
        "kind": "ai",
        "kind_label": "AI / pipeline error",
        "mechanism": "introduced",
        "fields": {"date": 5},
        "evidence": ["date parsed as year 1900 instead of 2026"],
        "examples": [
            {"expected": "2026-03-01", "actual": "1900-03-01", "field": "date"}
        ],
        "member_ids": [0, 1, 2],
    }

    def mock_llm_client(prompt):
        assert "Affected fields/stages: date" in prompt
        assert "expected '2026-03-01' but got '1900-03-01'" in prompt
        return "Two-digit year misparsed as 1900s"

    new_name = name_cause_with_llm(group, client=mock_llm_client)
    assert new_name == "Two-digit year misparsed as 1900s"

    # Now run name_causes
    groups = [dict(group)]
    res = name_causes(groups, client=mock_llm_client)
    assert len(res) == 1
    # Name is updated
    assert res[0]["name"] == "Two-digit year misparsed as 1900s"
    # Template name is preserved
    assert res[0]["template_name"] == "OCR gets date wrong"
    # Kind and mechanism are NEVER changed
    assert res[0]["kind"] == "ai"
    assert res[0]["mechanism"] == "introduced"


def test_name_cause_fallback_on_failure():
    group = {
        "key": "infrastructure|step_failed|api|order",
        "name": "API failed, so order is missing or wrong",
        "kind": "infrastructure",
        "mechanism": "step_failed",
        "fields": {"order": 3},
        "evidence": ["Connection timeout"],
        "examples": [],
    }

    def failing_llm(prompt):
        raise RuntimeError("API timeout")

    # When LLM raises, returns None and keeps template name
    res = name_cause_with_llm(group, client=failing_llm)
    assert res is None

    groups = [dict(group)]
    updated = name_causes(groups, client=failing_llm)
    assert updated[0]["name"] == "API failed, so order is missing or wrong"
    assert updated[0]["kind"] == "infrastructure"
