"""One shape for every model provider's response, and one way to ask any of them.

    from assay_sdk import Judge, normalize

    judge = Judge(provider="openai", model="gpt-5")          # anthropic, gemini, ollama, openai-compatible
    r = judge.ask("Rate this answer from 1 to 5 …", system=RUBRIC, schema=VERDICT, temperature=0)
    r.text, r.structured, r.tool_calls, r.usage, r.reasoning, r.finish_reason, r.error, r.raw

    r = normalize(response)   # an Anthropic, OpenAI, Gemini, Ollama or LiteLLM response you already have

Whatever answered, a metric reads the same fields:

  text           the answer's text
  structured     the answer parsed as JSON (and checked against `schema` when one was asked for);
                 None when it isn't JSON, never an empty dict standing in for one
  tool_calls     [{"name", "arguments", "id"}]: arguments always a dict. A JSON string is parsed;
                 anything else is kept as {"_raw": value}. Nothing is dropped.
  usage          {"input", "output", "cached", "reasoning"}: tokens, None where the provider doesn't say
  reasoning      {"tokens", "summary"}: what the provider reports about its thinking, if anything
  finish_reason  stop | length | tool_call | refusal | content_filter | error | None
  error          why there's no answer; error_kind: timeout | rate_limited | unavailable | invalid | error
  cost           dollars, when the provider says (LiteLLM, OpenRouter); None otherwise
  provider, model, raw (the provider's own object)

Judge passes every other parameter straight to the provider (temperature, max_tokens,
reasoning effort, anything new): nothing is filtered or renamed. Credentials are the provider
SDK's own: its API-key variables, or a CLI login it supports (the Anthropic SDK reads an
`ant auth login` profile). Ollama and OpenAI-compatible servers (vLLM, LM Studio, a LiteLLM
proxy) are called over HTTP with no SDK at all: base_url, and an api_key if the server wants one.

Inside an EvalRuntime (assay_sdk/runtime.py), ask() is the one place a call is retried: the SDK's
own retries are off, each request waits for the run's rate limit, has the run's timeout, and is
counted with its tokens and cost; a 429 or 5xx is asked again as the runtime allows (Retry-After).
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

PROVIDERS = ("anthropic", "openai", "gemini", "ollama", "openai-compatible")
FINISH = {  # each provider's words for why it stopped, in one vocabulary
    "end_turn": "stop", "stop_sequence": "stop", "stop": "stop", "STOP": "stop", "pause_turn": "stop",
    "max_tokens": "length", "length": "length", "MAX_TOKENS": "length", "model_context_window_exceeded": "length",
    "tool_use": "tool_call", "tool_calls": "tool_call", "function_call": "tool_call",
    "refusal": "refusal", "SAFETY": "content_filter", "content_filter": "content_filter", "RECITATION": "content_filter",
    "PROHIBITED_CONTENT": "content_filter", "BLOCKLIST": "content_filter", "SPII": "content_filter",
    "MALFORMED_FUNCTION_CALL": "error", "OTHER": None, "FINISH_REASON_UNSPECIFIED": None,
}


@dataclass
class Response:
    text: Optional[str] = None
    structured: Any = None
    tool_calls: List[dict] = field(default_factory=list)
    usage: Dict[str, Optional[int]] = field(default_factory=lambda: {"input": None, "output": None, "cached": None,
                                                                     "reasoning": None})
    reasoning: Dict[str, Any] = field(default_factory=dict)
    finish_reason: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    error: Optional[str] = None
    error_kind: Optional[str] = None
    raw: Any = None
    exception: Optional[BaseException] = None  # what the provider raised, when it did
    cost: Optional[float] = None  # dollars, when the provider reports it

    @property
    def ok(self) -> bool:
        return self.error is None


# ---------- tool arguments ----------

def normalize_args(value: Any) -> dict:
    """Tool arguments as a dict, whatever shape they came in: a dict as it is, a JSON string parsed,
    anything else (a list, a number, text that isn't JSON) kept under "_raw". Never dropped."""
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    if isinstance(value, (str, bytes)):
        try:
            parsed = json.loads(value)
        except ValueError:
            return {"_raw": value if isinstance(value, str) else value.decode("utf-8", "replace")}
        return parsed if isinstance(parsed, dict) else {"_raw": parsed}
    if hasattr(value, "model_dump"):
        try:
            return normalize_args(value.model_dump())
        except Exception:
            pass
    if hasattr(value, "items"):  # a mapping that isn't a dict (e.g. a protobuf Struct)
        try:
            return dict(value.items())
        except Exception:
            pass
    return {"_raw": value}


# ---------- reading a response ----------

def _get(obj: Any, *path: str, default=None):
    """obj.a.b or obj["a"]["b"], whichever it is."""
    for p in path:
        if obj is None:
            return default
        obj = obj.get(p) if isinstance(obj, dict) else getattr(obj, p, None)
    return default if obj is None else obj


def _json(text: Optional[str]) -> Any:
    if not text:
        return None
    t = text.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", t, re.S)
    try:
        return json.loads(fenced.group(1) if fenced else t)
    except ValueError:
        return None


def detect(resp: Any) -> Optional[str]:
    """Which provider's response this is, from its shape."""
    if isinstance(resp, dict):
        if "message" in resp and ("done" in resp or "eval_count" in resp):
            return "ollama"
        if "choices" in resp:
            return "openai"
        if "candidates" in resp:
            return "gemini"
        if "content" in resp and "stop_reason" in resp:
            return "anthropic"
        return None
    mod = type(resp).__module__ or ""
    if mod.startswith("anthropic") or (hasattr(resp, "stop_reason") and hasattr(resp, "content")):
        return "anthropic"
    if mod.startswith("google") or hasattr(resp, "candidates"):
        return "gemini"
    if mod.startswith("ollama") or (hasattr(resp, "done_reason") and hasattr(resp, "message")):
        return "ollama"
    if hasattr(resp, "choices") or hasattr(resp, "output_text") or mod.startswith(("openai", "litellm")):
        return "openai"
    return None


def _anthropic(resp) -> Response:
    blocks = _get(resp, "content", default=[]) or []
    text = "".join(_get(b, "text", default="") for b in blocks if _get(b, "type") == "text")
    calls = [{"name": _get(b, "name"), "arguments": normalize_args(_get(b, "input")), "id": _get(b, "id")}
             for b in blocks if _get(b, "type") == "tool_use"]
    thinking = [_get(b, "thinking", default="") for b in blocks if _get(b, "type") == "thinking"]
    u = _get(resp, "usage")
    stop = _get(resp, "stop_reason")
    return Response(text=text or None, tool_calls=calls, finish_reason=FINISH.get(stop, stop),
                    usage={"input": _get(u, "input_tokens"), "output": _get(u, "output_tokens"),
                           "cached": _get(u, "cache_read_input_tokens"), "reasoning": None},
                    reasoning={"summary": "\n".join(t for t in thinking if t) or None} if thinking else {},
                    provider="anthropic", model=_get(resp, "model"), raw=resp)


def _openai(resp) -> Response:
    if _get(resp, "output") is not None and _get(resp, "choices") is None:  # the Responses API
        items = _get(resp, "output", default=[]) or []
        text = _get(resp, "output_text") or "".join(
            _get(c, "text", default="") for i in items if _get(i, "type") == "message"
            for c in (_get(i, "content", default=[]) or []) if _get(c, "type") in ("output_text", "text"))
        calls = [{"name": _get(i, "name"), "arguments": normalize_args(_get(i, "arguments")),
                  "id": _get(i, "call_id") or _get(i, "id")} for i in items if _get(i, "type") == "function_call"]
        u = _get(resp, "usage")
        status, incomplete = _get(resp, "status"), _get(resp, "incomplete_details", "reason")
        finish = "tool_call" if calls else "length" if incomplete == "max_output_tokens" else \
            "content_filter" if incomplete == "content_filter" else "stop" if status == "completed" else None
        refusal = any(_get(c, "type") == "refusal" for i in items for c in (_get(i, "content", default=[]) or []))
        return Response(text=text or None, tool_calls=calls, finish_reason="refusal" if refusal else finish,
                        usage={"input": _get(u, "input_tokens"), "output": _get(u, "output_tokens"),
                               "cached": _get(u, "input_tokens_details", "cached_tokens"),
                               "reasoning": _get(u, "output_tokens_details", "reasoning_tokens")},
                        reasoning={"tokens": _get(u, "output_tokens_details", "reasoning_tokens")}
                        if _get(u, "output_tokens_details", "reasoning_tokens") else {},
                        provider="openai", model=_get(resp, "model"), raw=resp)
    choice = (_get(resp, "choices", default=[]) or [None])[0]
    msg = _get(choice, "message")
    calls = [{"name": _get(c, "function", "name"), "arguments": normalize_args(_get(c, "function", "arguments")),
              "id": _get(c, "id")} for c in (_get(msg, "tool_calls", default=[]) or [])]
    u = _get(resp, "usage")
    stop = _get(choice, "finish_reason")
    finish = "refusal" if _get(msg, "refusal") else FINISH.get(stop, stop)
    rtok = _get(u, "completion_tokens_details", "reasoning_tokens")
    return Response(text=_get(msg, "content") or None, tool_calls=calls, finish_reason=finish,
                    usage={"input": _get(u, "prompt_tokens"), "output": _get(u, "completion_tokens"),
                           "cached": _get(u, "prompt_tokens_details", "cached_tokens"), "reasoning": rtok},
                    reasoning={"tokens": rtok, **({"summary": _get(msg, "reasoning_content")}
                                                  if _get(msg, "reasoning_content") else {})} if rtok or _get(msg, "reasoning_content") else {},
                    provider="openai", model=_get(resp, "model"), raw=resp)


def _gemini(resp) -> Response:
    cand = (_get(resp, "candidates", default=[]) or [None])[0]
    parts = _get(cand, "content", "parts", default=[]) or []
    text = "".join(_get(p, "text", default="") for p in parts if _get(p, "text") and not _get(p, "thought"))
    thoughts = [_get(p, "text") for p in parts if _get(p, "thought") and _get(p, "text")]
    calls = [{"name": _get(p, "function_call", "name"), "arguments": normalize_args(_get(p, "function_call", "args")),
              "id": _get(p, "function_call", "id")} for p in parts if _get(p, "function_call")]
    u = _get(resp, "usage_metadata")
    stop = _get(cand, "finish_reason")
    stop = getattr(stop, "name", stop)  # an enum in the SDK
    finish = "tool_call" if calls and FINISH.get(stop, stop) == "stop" else FINISH.get(stop, stop)
    rtok = _get(u, "thoughts_token_count")
    return Response(text=text or None, tool_calls=calls, finish_reason=finish,
                    usage={"input": _get(u, "prompt_token_count"), "output": _get(u, "candidates_token_count"),
                           "cached": _get(u, "cached_content_token_count"), "reasoning": rtok},
                    reasoning={k: v for k, v in (("tokens", rtok), ("summary", "\n".join(thoughts) or None)) if v},
                    provider="gemini", model=_get(resp, "model_version"), raw=resp)


def _ollama(resp) -> Response:
    msg = _get(resp, "message")
    calls = [{"name": _get(c, "function", "name"), "arguments": normalize_args(_get(c, "function", "arguments")),
              "id": _get(c, "id")} for c in (_get(msg, "tool_calls", default=[]) or [])]
    stop = _get(resp, "done_reason")
    return Response(text=_get(msg, "content") or None, tool_calls=calls,
                    finish_reason="tool_call" if calls else FINISH.get(stop, stop),
                    usage={"input": _get(resp, "prompt_eval_count"), "output": _get(resp, "eval_count"),
                           "cached": None, "reasoning": None},
                    reasoning={"summary": _get(msg, "thinking")} if _get(msg, "thinking") else {},
                    provider="ollama", model=_get(resp, "model"), raw=resp)


READERS = {"anthropic": _anthropic, "openai": _openai, "openai-compatible": _openai, "gemini": _gemini,
           "ollama": _ollama}


def normalize(resp: Any, provider: Optional[str] = None, schema: Optional[dict] = None) -> Response:
    """Any supported provider's response as a Response. With a schema, `structured` is the parsed
    answer only if it fits (else None, with error_kind invalid)."""
    provider = provider or detect(resp)
    if provider not in READERS:
        raise ValueError(f"Can't tell which provider this response is from: {type(resp).__name__}. "
                         f"Pass provider= one of {', '.join(PROVIDERS)}.")
    r = READERS[provider](resp)
    r.structured = _json(r.text)
    cost = _get(resp, "_hidden_params", "response_cost") or _get(resp, "usage", "cost")  # LiteLLM, OpenRouter
    if isinstance(cost, (int, float)) and not isinstance(cost, bool):
        r.cost = float(cost)
    if schema is not None:
        from assay_sdk.evaluation import _check_schema
        problem = "the answer isn't JSON" if r.structured is None else _check_schema(r.structured, schema)
        if problem and r.finish_reason not in ("refusal", "content_filter"):
            r.structured, r.error, r.error_kind = None, f"the answer doesn't fit its schema: {problem}", "invalid"
    if r.finish_reason == "length" and r.error is None and schema is not None:
        r.error, r.error_kind = "the answer was cut off (the provider's token limit)", "invalid"
    if r.finish_reason in ("refusal", "content_filter") and r.error is None:
        r.error, r.error_kind = f"the model declined ({r.finish_reason})", "error"
    return r


# ---------- asking ----------

def _post(url: str, body: dict, headers: dict, timeout: float) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        err = RuntimeError(f"HTTP {e.code} from {url}: {detail}")
        err.status_code, err.headers = e.code, e.headers  # the status, and Retry-After, for a retry
        raise err


class Judge:
    """Ask any provider, get a Response. provider: anthropic | openai | gemini | ollama | openai-compatible."""

    def __init__(self, provider: str, model: str, *, client: Any = None, base_url: Optional[str] = None,
                 api_key: Optional[str] = None, timeout: float = 120.0, **defaults):
        if provider not in PROVIDERS:
            raise ValueError(f"Unknown provider '{provider}'. Providers: {', '.join(PROVIDERS)}.")
        self.provider, self.model, self.timeout, self.defaults = provider, model, timeout, defaults
        self.base_url = (base_url or {"ollama": os.environ.get("OLLAMA_HOST", "http://localhost:11434"),
                                      "openai-compatible": os.environ.get("OPENAI_BASE_URL")}.get(provider) or "").rstrip("/")
        self.api_key = api_key
        self._client = client

    def client(self):
        if self._client is not None:
            return self._client
        if self.provider == "anthropic":
            import anthropic  # its own credentials: ANTHROPIC_API_KEY, or an `ant auth login` profile
            self._client = anthropic.Anthropic(timeout=self.timeout, max_retries=2)
        elif self.provider == "openai":
            import openai
            self._client = openai.OpenAI(timeout=self.timeout, max_retries=2)
        elif self.provider == "gemini":
            from google import genai  # GEMINI_API_KEY / GOOGLE_API_KEY, or Vertex AI's gcloud login
            self._client = genai.Client()
        return self._client

    def _messages(self, prompt, system):
        msgs = prompt if isinstance(prompt, list) else [{"role": "user", "content": prompt}]
        return msgs, system

    def ask(self, prompt: Any, *, system: Optional[str] = None, schema: Optional[dict] = None, check: bool = True,
            **params) -> Response:
        """prompt: text, or a list of {"role", "content"} messages. schema: the JSON Schema the answer
        must fit (asked of the provider where it supports it, and checked here unless check=False,
        for a caller with checks of its own). Every other parameter goes to the provider as it is."""
        messages, system = self._messages(prompt, system)
        from assay_sdk import runtime
        scope = runtime.current()
        if scope is None:
            return self._once(messages, system, schema, check, {**self.defaults, **params}, None)
        token = runtime._in_judge.set(True)  # what instrument() sees of this call is counted here
        try:
            i = 0
            while True:
                why = scope.gate()  # the run's rate limit
                if why:
                    return Response(provider=self.provider, model=self.model, error=why, error_kind="timeout",
                                    finish_reason="error")
                r = self._once(messages, system, schema, check, {**self.defaults, **params}, scope.call_timeout())
                scope.counted(r, judge=True)
                if r.ok or not scope.retry_call(r, i):
                    return r
                i += 1
        finally:
            runtime._in_judge.reset(token)

    __call__ = ask

    def _once(self, messages, system, schema, check, params, timeout: Optional[float]) -> Response:
        """One request. timeout set: inside a runtime, with the SDK's own retries off."""
        try:
            raw = getattr(self, f"_ask_{self.provider.replace('-', '_')}")(messages, system, schema, params, timeout)
        except Exception as exc:
            from assay_sdk.evaluation import classify_exception
            kind, text = classify_exception(exc)
            return Response(provider=self.provider, model=self.model, error=text, error_kind=kind, exception=exc,
                            finish_reason="error")
        return normalize(raw, "openai" if self.provider == "openai-compatible" else self.provider,
                         schema if check else None)

    def _sdk(self, timeout: Optional[float]):
        """The client; inside a runtime, one that doesn't retry and has the run's timeout."""
        c = self.client()
        if timeout is not None and hasattr(c, "with_options"):  # anthropic and openai clients
            return c.with_options(max_retries=0, timeout=timeout)
        return c

    def _ask_anthropic(self, messages, system, schema, params, timeout=None):
        req = {"model": self.model, "max_tokens": params.pop("max_tokens", 16000), "messages": messages, **params}
        if system:
            req.setdefault("system", [{"type": "text", "text": system}])
        if schema:
            req.setdefault("output_config", {"format": {"type": "json_schema", "schema": schema}})
        return self._sdk(timeout).messages.create(**req)

    def _ask_openai(self, messages, system, schema, params, timeout=None):
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        req = {"model": self.model, "messages": msgs, **params}
        if schema:
            req.setdefault("response_format", {"type": "json_schema",
                                               "json_schema": {"name": "answer", "schema": schema, "strict": True}})
        return self._sdk(timeout).chat.completions.create(**req)

    def _ask_gemini(self, messages, system, schema, params, timeout=None):
        contents = [{"role": "model" if m["role"] == "assistant" else "user", "parts": [{"text": m["content"]}]}
                    for m in messages]
        config = dict(params.pop("config", {}) or {})
        if system:
            config.setdefault("system_instruction", system)
        if schema:
            config.setdefault("response_mime_type", "application/json")
            config.setdefault("response_json_schema", schema)
        if timeout is not None:
            config.setdefault("http_options", {"timeout": int(timeout * 1000)})  # milliseconds
        return self.client().models.generate_content(model=self.model, contents=contents, config=config or None, **params)

    def _ask_ollama(self, messages, system, schema, params, timeout=None):
        body = {"model": self.model, "stream": False,
                "messages": ([{"role": "system", "content": system}] if system else []) + messages, **params}
        if schema:
            body.setdefault("format", schema)
        if self._client is not None:
            return self._client.chat(**body)
        return _post(f"{self.base_url}/api/chat", body, {}, timeout or self.timeout)

    def _ask_openai_compatible(self, messages, system, schema, params, timeout=None):
        if not self.base_url:
            raise ValueError("openai-compatible needs base_url (or OPENAI_BASE_URL), e.g. http://localhost:8000/v1")
        body = {"model": self.model, "messages": ([{"role": "system", "content": system}] if system else []) + messages,
                **params}
        if schema:
            body.setdefault("response_format", {"type": "json_schema",
                                                "json_schema": {"name": "answer", "schema": schema}})
        key = self.api_key or os.environ.get("OPENAI_API_KEY")
        return _post(f"{self.base_url}/chat/completions", body, {"Authorization": f"Bearer {key}"} if key else {},
                     timeout or self.timeout)
