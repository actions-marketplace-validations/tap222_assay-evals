import json
import pytest
from assay import learn, redaction


def test_credentials_redaction_patterns():
    # OpenAI key
    text = "Use key sk-proj-1234567890abcdefghijklmnopqrstuvwxyz123"
    assert learn.redact(text) == "Use key <openai_key>"

    # Anthropic key
    text = "Authorization: sk-ant-api03-abcdefghijklmnopqrstu1234567890"
    assert learn.redact(text) == "Authorization: <anthropic_key>"

    # Google API key
    dummy_google = "AIzaSy" + "D1234567890abcdefghijklmnopqrstuvw"
    text = f"Key is {dummy_google}"
    assert learn.redact(text) == "Key is <google_api_key>"

    # AWS Access Key
    dummy_aws = "AKIA" + "IOSFODNN7EXAMPLE"
    text = f"AWS key {dummy_aws} here"
    assert learn.redact(text) == "AWS key <aws_access_key> here"

    # GitHub token
    dummy_gh = "ghp_" + "1234567890abcdefghijklmnopqrstuvwxyz0123"
    text = f"Token: {dummy_gh}"
    assert learn.redact(text) == "Token: <github_token>"

    # Slack token
    dummy_slack = "xoxb-" + "12345678-test-token-value-sample"
    text = f"Slack token {dummy_slack}"
    assert learn.redact(text) == "Slack token <slack_token>"

    # JWT
    jwt = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4gRG9lIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
    assert learn.redact(f"Bearer {jwt}") == "Bearer <jwt>"

    # Private key block
    pk = ("-----BEGIN RSA PRIVATE KEY-----\n"
          "MIIEowIBAAKCAQEA0Y1u4q\n"
          "-----END RSA PRIVATE KEY-----")
    assert learn.redact(f"Key:\n{pk}") == "Key:\n<private_key>"


def test_secret_named_fields():
    data = {
        "user": "alice",
        "api_key": "some_super_secret_value_12345678",
        "password": "correct_horse_battery_staple_1234",
        "nested": {
            "authToken": "token_abc_1234567890123456789"
        }
    }
    redacted = learn.redact(data)
    assert redacted["user"] == "alice"
    assert redacted["api_key"] == "<secret>"
    assert redacted["password"] == "<secret>"
    assert redacted["nested"]["authToken"] == "<secret>"


def test_allow_list():
    key = "sk-proj-testallowedkey12345678901234"
    text = f"Using {key} for dev"
    # Without allow-list: redacted
    assert learn.redact(text) == "Using <openai_key> for dev"
    # With allow-list: kept intact
    assert learn.redact(text, allow_list={key}) == f"Using {key} for dev"


def test_redact_check_finds_credentials():
    events = [
        {
            "type": "run.start",
            "run_id": "r_sec_1",
            "input": "Call API with sk-ant-api03-abcdefghijklmnopqrstu1234567890"
        },
        {
            "type": "step",
            "run_id": "r_sec_1",
            "kind": "tool",
            "args": {"apiKey": "unredacted_secret_string_123456789"},
            "result": "OK"
        }
    ]
    report = redaction.scan(events)
    assert report["runs_with"] == 1
    found_kinds = {item["kind"] for item in report["found"]}
    assert "anthropic_key" in found_kinds
    assert "secret" in found_kinds

    # Test terminal text output
    txt = redaction.text(report, "test.jsonl")
    assert "Personal data or credentials got through" in txt
    assert "anthropic_key" in txt
    assert "secret" in txt
