"""The input side (assay_sdk/inputs.py): what went into each call, the fixed context it started with,
and the input changes next to a regression."""
import base64
import json
import struct
import sys
from pathlib import Path
from types import SimpleNamespace as N

import pytest

from assay.__main__ import main
from assay_sdk.inputs import describe, image_size

SDK = str(Path(__file__).resolve().parents[1] / "sdk" / "python")


def png(w, h):
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", w, h) + b"\x08\x02\x00\x00\x00"


def jpeg(w, h):
    return b"\xff\xd8" + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00" + b"\x00" * 9 + \
        b"\xff\xc0" + struct.pack(">HBHH", 17, 8, h, w) + b"\x03" + b"\x00" * 9


def b64(data):
    return base64.b64encode(data).decode()


def test_an_anthropic_request_by_part():
    kw = {"model": "claude-opus-5", "system": "x" * 4000, "max_tokens": 1024, "temperature": 0.2,
          "thinking": {"type": "enabled", "budget_tokens": 2000},
          "tools": [{"name": "refund", "description": "d" * 700, "input_schema": {"type": "object"}}],
          "messages": [{"role": "user", "content": "earlier " * 50}, {"role": "assistant", "content": "ok"},
                       {"role": "user", "content": [
                           {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": b64(png(1280, 720))}},
                           {"type": "text", "text": "What's in this frame?"}]}]}
    d = describe("anthropic", kw)
    assert d["context"]["system"] == 1000 and d["context"]["tools"] > 175 and d["context"]["history"] > 90
    assert d["media"]["images"] == 1 and d["media"]["size"] == "1280x720"
    assert d["settings"] == {"temperature": 0.2, "max_tokens": 1024, "thinking": "enabled:2000"}


def test_openai_gemini_and_responses_requests():
    d = describe("openai", {"messages": [{"role": "system", "content": "s" * 400}, {"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + b64(jpeg(640, 480)), "detail": "high"}},
        {"type": "text", "text": "hi"}]}], "reasoning_effort": "low"})
    assert d["context"]["system"] == 100 and d["media"] == {"images": 1, "videos": 0, "bytes": d["media"]["bytes"],
                                                            "size": "640x480", "detail": "high"}
    assert d["settings"] == {"reasoning_effort": "low"}
    g = describe("gemini", {"contents": [N(parts=[N(text="describe it", inline_data=None, file_data=None),
                                                  N(text=None, inline_data=N(mime_type="video/mp4", data=b"\x00" * 900),
                                                    file_data=None)])],
                            "config": N(system_instruction="be brief", tools=None, temperature=0.4, top_p=None,
                                        top_k=None, max_output_tokens=None, seed=None)})
    assert g["media"]["videos"] == 1 and g["media"]["bytes"] == 900 and g["settings"] == {"temperature": 0.4}
    r = describe("openai", {"instructions": "i" * 800, "input": "what's my refund status?"})
    assert r["context"]["system"] == 200 and r["context"]["user"] > 0
    assert image_size(png(3, 4)) == "3x4" and image_size(jpeg(1920, 1080)) == "1920x1080" and image_size(b"nope") is None


def test_instrument_records_the_input_side():
    from assay_sdk import auto
    resp = N(model="claude-opus-5", stop_reason="end_turn", content=[N(type="text", text="ok")],
             usage=N(input_tokens=1500, output_tokens=5, cache_read_input_tokens=0))
    got = auto._reading("anthropic")({"model": "claude-opus-5", "system": "s" * 800, "temperature": 0,
                                      "messages": [{"role": "user", "content": "hi there you"}]}, resp)
    assert got["context"]["system"] == 200 and got["settings"] == {"temperature": 0}


AGENT = '''
import os
import assay_sdk as assay
assay.init()
after = os.environ["MODE"] == "after"
for i in range(3):
    grew, frames, hot = after and i == 0, after and i == 1, after and i == 2
    with assay.run("support", test=f"q{i}") as r:
        r.llm(model="claude-opus-5", tokens_in=2000, tokens_out=20,
              context={"system": 4300 if grew else 1200, "tools": 400, "user": 80},
              media={"images": 4 if frames else 8, "videos": 0, "bytes": 1, "size": "1920x1080" if frames else "1280x720"},
              settings={"temperature": 0.7 if hot else 0.2})
        r.answer("ok")
        r.check("faithfulness", "fail" if after else "pass", score=0.6 if after else 0.95)
'''


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PYTHONPATH", SDK)
    monkeypatch.syspath_prepend(SDK)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("ASSAY_URL", "ASSAY_TEST_RUN", "ASSAY_PATH", "ASSAY_PYTEST_SESSION", "ASSAY_POLICY", "GITHUB_STEP_SUMMARY"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "agent.py").write_text(AGENT)
    (tmp_path / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n')
    monkeypatch.setenv("MODE", "before")
    assert main(["test"]) == 0
    return tmp_path


def test_a_faithfulness_dip_is_shown_next_to_the_input_that_moved(project, monkeypatch, capsys):
    capsys.readouterr()
    monkeypatch.setenv("MODE", "after")
    assert main(["test"]) == 1
    out = capsys.readouterr().out
    assert "Fixed context per call\n  1,600 tokens (system prompt 1,200, tool definitions 400)" in out
    assert "context system prompt 1,200 → 4,300 tokens (+3,100) per call" in out
    assert "Fixed context: 1,600 tokens → 4,700 tokens (2.9×)" in out  # a behavior regression of its own
    assert "media   8 per call at 1280x720 → 4 per call at 1920x1080" in out
    assert "settings temperature 0.2 → 0.7" in out


def test_a_limit_on_fixed_context(project, monkeypatch, capsys):
    (project / "assay.toml").write_text(f'[test]\ncommand = "{sys.executable} agent.py"\n[behavior]\n'
                                        'max_fixed_context_tokens = 3000\n')
    capsys.readouterr()
    monkeypatch.setenv("MODE", "after")
    main(["test"])
    out = capsys.readouterr().out
    assert "started with 4,700 tokens of fixed context (system prompt 4,300, tool definitions 400), over the limit " \
           "of 3,000" in out
