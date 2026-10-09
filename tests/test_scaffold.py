"""`assay connect evals` (assay/scaffold.py): a proposed, skipped test for every model call in the code."""
import py_compile
import subprocess
import sys
from pathlib import Path

import pytest

from assay import scaffold
from assay.__main__ import main

APP = '''"""Support app."""
import anthropic

client = anthropic.Anthropic()
CLASSIFY = "You classify support messages as refund, delivery or other."


def classify(message):
    """Label a support message."""
    r = client.messages.create(model="claude-opus-5", max_tokens=100, system=CLASSIFY,
                               messages=[{"role": "user", "content": message}],
                               output_config={"format": {"type": "json_schema", "schema": {
                                   "type": "object", "required": ["label", "confidence"]}}})
    return r.content[0].text


def reply(message, history):
    return client.messages.create(model="claude-opus-5", max_tokens=500, messages=history,
                                  tools=[{"name": "get_order", "input_schema": {}},
                                         {"name": "refund", "input_schema": {}}])


def _helper():
    return client.messages.create(model="m", messages=[])
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "app" / "support.py").write_text(APP, encoding="utf-8")
    return tmp_path


def test_every_model_call_gets_a_proposed_test(project, capsys):
    got = {s.function: s for s in scaffold.sites(Path("app"))}
    assert set(got) == {"classify", "reply"}  # _helper is private: not a test's to call
    c = got["classify"]
    assert (c.model, c.structured, c.required, c.params) == ("claude-opus-5", True, ["label", "confidence"], ["message"])
    assert c.system.startswith("You classify support messages")
    assert got["reply"].tools == ["get_order", "refund"] and got["reply"].params == ["message", "history"]

    assert main(["connect", "evals", "app"]) == 0
    out = capsys.readouterr().out
    assert "+ tests/ai/test_support_classify.py" in out and "Nothing is written yet" in out
    assert not Path("tests").exists()
    assert main(["connect", "evals", "app", "--apply"]) == 0
    written = Path("tests/ai/test_support_classify.py").read_text(encoding="utf-8")
    assert "proposed by assay connect evals" in written and "pytestmark = pytest.mark.skip(" in written
    assert "for key in ['label', 'confidence']:" in written and 'system prompt "You classify support' in written
    assert "Label a support message." in written  # the docstring starts the spec
    assert "expect(assay_case).must_call('get_order').max_tools_exposed(2)" in \
        Path("tests/ai/test_support_reply.py").read_text(encoding="utf-8")
    for f in Path("tests/ai").glob("*.py"):
        py_compile.compile(str(f), doraise=True)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "tests/ai", "-p", "no:cacheprovider"],
                       capture_output=True, text=True, cwd=project, encoding="utf-8", errors="replace")
    assert "6 skipped" in r.stdout, r.stdout + r.stderr  # 3 cases each: nothing runs until a person writes the spec
    capsys.readouterr()
    assert main(["connect", "evals", "app"]) == 0
    assert "(there already)" in capsys.readouterr().out


def test_a_function_that_has_a_test_is_left_alone(project, capsys):
    (project / "tests").mkdir()
    (project / "tests" / "test_mine.py").write_text("from app.support import classify\n\ndef test_it():\n    classify('x')\n", encoding="utf-8")
    main(["connect", "evals", "app"])
    out = capsys.readouterr().out
    assert "classify (app/support.py:" in out and "has a test already" in out and "+ tests/ai/test_support_reply.py" in out
