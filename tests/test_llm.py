"""Any provider, one shape (assay_sdk/llm.py): normalize, Judge, tool arguments, and where they're used."""
import json
import sys
import threading
import types
import warnings
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace as N

import pytest

import assay_sdk as assay
from assay_sdk import Judge, Response, evaluate, normalize, normalize_args
from assay_sdk.llm import detect

VERDICT = {"type": "object", "required": ["score", "reason"],
           "properties": {"score": {"type": "number", "minimum": 0, "maximum": 1}, "reason": {"type": "string"}}}


# ---------- one shape, whoever answered ----------

def test_anthropic():
    r = normalize(N(model="claude-opus-5", stop_reason="tool_use",
                    usage=N(input_tokens=900, output_tokens=40, cache_read_input_tokens=800),
                    content=[N(type="thinking", thinking="Check the order first."), N(type="text", text="Looking it up."),
                             N(type="tool_use", id="tu_1", name="get_order", input={"id": "O-17"})]))
    assert (r.provider, r.text, r.finish_reason, r.model) == ("anthropic", "Looking it up.", "tool_call", "claude-opus-5")
    assert r.tool_calls == [{"name": "get_order", "arguments": {"id": "O-17"}, "id": "tu_1"}]
    assert r.usage == {"input": 900, "output": 40, "cached": 800, "reasoning": None}
    assert r.reasoning == {"summary": "Check the order first."}


def test_openai_chat_arguments_come_as_json_strings():
    resp = {"model": "gpt-5", "choices": [{"finish_reason": "tool_calls", "message": {"content": None, "tool_calls": [
        {"id": "c1", "function": {"name": "get_order", "arguments": '{"id": "O-17"}'}},
        {"id": "c2", "function": {"name": "search", "arguments": "not json at all"}}]}}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 9, "prompt_tokens_details": {"cached_tokens": 32},
                      "completion_tokens_details": {"reasoning_tokens": 7}}}
    r = normalize(resp)
    assert detect(resp) == "openai" and r.finish_reason == "tool_call"
    assert [c["arguments"] for c in r.tool_calls] == [{"id": "O-17"}, {"_raw": "not json at all"}]  # never dropped
    assert r.usage == {"input": 50, "output": 9, "cached": 32, "reasoning": 7} and r.reasoning["tokens"] == 7
    cut = normalize({"choices": [{"finish_reason": "length", "message": {"content": '{"score": 0.'}}]})
    assert cut.finish_reason == "length" and cut.structured is None
    no = normalize({"choices": [{"finish_reason": "stop", "message": {"content": None, "refusal": "I can't help."}}]})
    assert no.finish_reason == "refusal" and no.error_kind == "error"


def test_openai_responses_api():
    r = normalize(N(model="gpt-5", status="incomplete", incomplete_details=N(reason="max_output_tokens"), output_text="",
                    output=[N(type="function_call", call_id="fc_1", name="refund", arguments='{"id": "O-17", "amount": 12}')],
                    usage=N(input_tokens=10, output_tokens=5, input_tokens_details=N(cached_tokens=0),
                            output_tokens_details=N(reasoning_tokens=3))), provider="openai")
    assert r.tool_calls == [{"name": "refund", "arguments": {"id": "O-17", "amount": 12}, "id": "fc_1"}]
    assert r.finish_reason == "tool_call" and r.usage["reasoning"] == 3


def test_gemini():
    r = normalize(N(model_version="gemini-3-pro", candidates=[N(finish_reason=N(name="STOP"), content=N(parts=[
        N(text="Thinking about the refund policy.", thought=True, function_call=None),
        N(text=None, thought=None, function_call=N(name="get_order", args={"id": "O-17"}, id=None)),
        N(text='{"score": 0.9, "reason": "grounded"}', thought=None, function_call=None)]))],
        usage_metadata=N(prompt_token_count=120, candidates_token_count=15, thoughts_token_count=40,
                         cached_content_token_count=None)), schema=VERDICT)
    assert (r.provider, r.finish_reason) == ("gemini", "tool_call")
    assert r.structured == {"score": 0.9, "reason": "grounded"} and r.error is None
    assert r.tool_calls[0]["arguments"] == {"id": "O-17"}
    assert r.usage == {"input": 120, "output": 15, "cached": None, "reasoning": 40}
    assert r.reasoning == {"tokens": 40, "summary": "Thinking about the refund policy."}
    blocked = normalize(N(candidates=[N(finish_reason="SAFETY", content=N(parts=[]))], usage_metadata=None))
    assert blocked.finish_reason == "content_filter" and blocked.error_kind == "error"


def test_ollama():
    resp = {"model": "llama3.1", "done": True, "done_reason": "stop", "prompt_eval_count": 30, "eval_count": 12,
            "message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "lookup",
                                                                                          "arguments": {"q": "refunds"}}}]}}
    r = normalize(resp)
    assert (detect(resp), r.finish_reason, r.usage["input"], r.tool_calls[0]["arguments"]) == \
        ("ollama", "tool_call", 30, {"q": "refunds"})


def test_a_schema_decides_what_counts_as_structured():
    r = normalize({"choices": [{"finish_reason": "stop", "message": {"content": '{"score": 1.4, "reason": "x"}'}}]},
                  schema=VERDICT)
    assert r.structured is None and r.error_kind == "invalid" and "above 1" in r.error


def test_tool_arguments_in_any_shape():
    assert normalize_args({"a": 1}) == {"a": 1} and normalize_args('{"a": 1}') == {"a": 1}
    assert normalize_args("[1, 2]") == {"_raw": [1, 2]} and normalize_args("O-17") == {"_raw": "O-17"}
    assert normalize_args(None) == {} and normalize_args(42) == {"_raw": 42}
    assert normalize_args(N(model_dump=lambda: {"id": 3})) == {"id": 3}


# ---------- asking any provider ----------

class Recorder:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def __call__(self, **kw):
        self.calls.append(kw)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_every_parameter_reaches_the_provider_unchanged():
    create = Recorder({"choices": [{"finish_reason": "stop", "message": {"content": '{"score": 0.8, "reason": "ok"}'}}]})
    client = N(chat=N(completions=N(create=create)))
    r = Judge("openai", "gpt-5", client=client).ask("Rate it", system="You grade answers.", schema=VERDICT,
                                                    temperature=0, seed=7, reasoning_effort="low", my_new_param=True)
    req = create.calls[0]
    assert (req["temperature"], req["seed"], req["reasoning_effort"], req["my_new_param"]) == (0, 7, "low", True)
    assert req["messages"][0] == {"role": "system", "content": "You grade answers."}
    assert req["response_format"]["json_schema"]["schema"] == VERDICT
    assert r.structured == {"score": 0.8, "reason": "ok"} and r.ok

    msgs = Recorder(N(model="claude-opus-5", stop_reason="end_turn", usage=N(input_tokens=1, output_tokens=1),
                      content=[N(type="text", text='{"score": 0.2, "reason": "off"}')]))
    r = Judge("anthropic", "claude-opus-5", client=N(messages=N(create=msgs))).ask("Rate", schema=VERDICT, max_tokens=500)
    assert msgs.calls[0]["max_tokens"] == 500 and msgs.calls[0]["output_config"]["format"]["schema"] == VERDICT
    assert r.structured["score"] == 0.2

    gen = Recorder(N(model_version="gemini-3", candidates=[N(finish_reason="STOP", content=N(parts=[
        N(text='{"score": 1, "reason": "yes"}', thought=None, function_call=None)]))], usage_metadata=None))
    r = Judge("gemini", "gemini-3", client=N(models=N(generate_content=gen))).ask("Rate", system="Grade.", schema=VERDICT)
    cfg = gen.calls[0]["config"]
    assert cfg["system_instruction"] == "Grade." and cfg["response_json_schema"] == VERDICT and r.structured["score"] == 1


class RateLimitError(Exception):
    status_code = 429


def test_a_provider_that_fails_is_a_classified_error_not_a_crash():
    r = Judge("openai", "gpt-5", client=N(chat=N(completions=N(create=Recorder(RateLimitError("slow down")))))).ask("x")
    assert (r.ok, r.error_kind, r.finish_reason) == (False, "rate_limited", "error") and isinstance(r.exception, RateLimitError)


@pytest.fixture
def server():
    """A local OpenAI-compatible and Ollama endpoint, recording what it was sent."""
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((self.path, dict(self.headers), body))
            if self.path.endswith("/api/chat"):
                out = {"model": body["model"], "done": True, "done_reason": "stop", "prompt_eval_count": 5, "eval_count": 3,
                       "message": {"role": "assistant", "content": '{"score": 0.6, "reason": "fine"}'}}
            else:
                out = {"model": body["model"], "choices": [{"finish_reason": "stop", "message": {"content": "hi"}}],
                       "usage": {"prompt_tokens": 2, "completion_tokens": 1}}
            data = json.dumps(out).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}", seen
    srv.shutdown()


def test_ollama_and_openai_compatible_servers_need_no_sdk(server):
    url, seen = server
    r = Judge("ollama", "llama3.1", base_url=url).ask("Rate", schema=VERDICT, options={"temperature": 0})
    path, _, body = seen[-1]
    assert path == "/api/chat" and body["format"] == VERDICT and body["options"] == {"temperature": 0} and not body["stream"]
    assert r.structured == {"score": 0.6, "reason": "fine"} and r.usage["input"] == 5
    r = Judge("openai-compatible", "qwen", base_url=f"{url}/v1", api_key="k").ask("hi", top_k=20)
    path, headers, body = seen[-1]
    assert path == "/v1/chat/completions" and headers["Authorization"] == "Bearer k" and body["top_k"] == 20
    assert (r.text, r.provider) == ("hi", "openai")


# ---------- evaluate() reads a Judge's answer, and its own keywords stay its own ----------

def test_evaluate_reads_a_response_and_keeps_its_error_kind():
    ok = Response(text='{"score": 0.9, "reason": "grounded"}', structured={"score": 0.9, "reason": "grounded"})
    assert evaluate(lambda: ok, schema=VERDICT).status == "PASS"
    limited = Response(error="rate limited (429)", error_kind="rate_limited")
    r = evaluate(lambda: limited, retries=0)
    assert (r.status, r.score) == ("RATE_LIMITED", None)


def test_judge_kwargs_reach_the_judge_and_a_clash_warns():
    got = {}

    def judge(q, schema=None, field=None):
        got.update(q=q, schema=schema, field=field)
        return 0.9
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        evaluate(judge, "q", field="helpful")
    assert got["field"] is None and "use judge_kwargs" in str(w[0].message)  # evaluate() kept it, and said so
    evaluate(judge, "q", judge_kwargs={"schema": VERDICT, "field": "helpful"})
    assert got == {"q": "q", "schema": VERDICT, "field": "helpful"}


# ---------- recorded the same way ----------

def test_tool_arguments_of_any_shape_are_ingested_not_rejected(tmp_path):
    from fastapi.testclient import TestClient
    from assay.api import create_app
    from assay.config import Settings
    c = TestClient(create_app(Settings(store_url=f"sqlite:///{tmp_path / 'l.db'}")))
    ev = lambda i, **k: {"v": 1, "id": f"e{i}", "ts": "2026-09-25T10:00:00Z", "run_id": "r", **k}
    r = c.post("/v1/ingest", headers={"X-Tenant": "t"}, json=[
        ev(0, type="run.start"), ev(1, type="step", seq=0, kind="tool", name="get_order", args='{"id": "O-17"}'),
        ev(2, type="step", seq=1, kind="tool", name="batch", args=["O-1", "O-2"]),
        ev(3, type="step", seq=2, kind="llm", model="gpt-5", finish_reason="tool_call", tokens_cached=12,
           tool_calls=[{"name": "refund", "arguments": '{"id": "O-17"}'}]),
        ev(4, type="run.end")])
    assert r.status_code == 200, r.text
    steps = c.get("/v1/agents/trajectories/r", params={"source": "events:t"}).json()["steps"]
    assert steps[0]["args"] == {"id": "O-17"} and steps[1]["args"] == {"_raw": ["O-1", "O-2"]}
    assert steps[2]["finish_reason"] == "tool_call" and steps[2]["tool_calls"][0]["arguments"] == {"id": "O-17"}


def test_instrument_records_gemini_ollama_and_litellm_the_same_way(tmp_path, monkeypatch):
    class Models:
        def generate_content(self, **kw):
            return N(model_version="gemini-3", candidates=[N(finish_reason="MAX_TOKENS", content=N(parts=[
                N(text="partial", thought=None, function_call=None)]))],
                     usage_metadata=N(prompt_token_count=9, candidates_token_count=4, thoughts_token_count=2,
                                      cached_content_token_count=None))
    ollama_mod = types.SimpleNamespace(__name__="ollama", chat=lambda **kw: {
        "model": "llama3.1", "done": True, "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 2,
        "message": {"content": "", "tool_calls": [{"function": {"name": "lookup", "arguments": {"q": "x"}}}]}})
    litellm_mod = types.SimpleNamespace(__name__="litellm", completion=lambda **kw: {
        "model": "claude", "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    for name, mod in (("google", types.SimpleNamespace()), ("google.genai", types.SimpleNamespace()),
                      ("google.genai.models", types.SimpleNamespace(Models=Models)), ("ollama", ollama_mod),
                      ("litellm", litellm_mod)):
        monkeypatch.setitem(sys.modules, name, mod)
    for k in ("anthropic", "anthropic.resources", "anthropic.resources.messages", "openai"):
        monkeypatch.delitem(sys.modules, k, raising=False)
    done = assay.instrument()
    assert {"google Models.generate_content", "ollama chat", "litellm completion"} <= set(done)
    path = tmp_path / "e.jsonl"
    monkeypatch.delenv("ASSAY_URL", raising=False)
    assay.init(path=str(path))
    with assay.run("x"):
        Models().generate_content(model="gemini-3", contents="hi")
        sys.modules["ollama"].chat(model="llama3.1", messages=[])
        sys.modules["litellm"].completion(model="claude", messages=[])
    assay.shutdown()
    llm = [json.loads(x) for x in path.read_text().splitlines() if '"llm"' in x]
    assert [(e["model"], e["finish_reason"]) for e in llm] == [("gemini-3", "length"), ("llama3.1", "tool_call"),
                                                               ("claude", "stop")]
    assert llm[0]["tokens_reasoning"] == 2 and llm[1]["tool_calls"][0]["arguments"] == {"q": "x"}


def test_the_built_in_judge_with_another_provider():
    from assay import judge
    create = Recorder({"model": "gpt-5", "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(
        {"consistency": {"applicable": True, "score": 4, "reason": "Agrees with step 1."},
         "plan_quality": {"applicable": False, "score": 1, "reason": "no plan"}})}}]})
    traj = {"answer": "Done.", "status": "completed", "steps": [{"seq": 0, "kind": "answer", "text": "Done."}]}
    out = judge.judge(traj, "Do it", model="gpt-5", provider="openai", client=N(chat=N(completions=N(create=create))))
    req = create.calls[0]
    assert req["model"] == "gpt-5" and req["response_format"]["json_schema"]["schema"] == judge.SCHEMA
    assert "fallbacks" not in json.dumps(req, default=str)  # Anthropic's, not sent elsewhere
    assert (out["consistency"]["status"], out["consistency"]["score"]) == ("pass", 4)
