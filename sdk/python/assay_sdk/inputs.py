"""What went into a model call before the model did anything: the input side.

Output metrics move when the input does. A system prompt that grew 3,000 tokens, tool definitions
that doubled, half as many video frames: none of it shows in the answer's score, and all of it
changes behavior. So each model call records what its input was made of:

  context   tokens, estimated from the text (four characters a token): system (the system prompt
            and standing instructions), tools (the tool definitions), history (earlier turns and
            tool results), user (the latest message), retrieved (fragments, when known). system +
            tools is the fixed context: paid on every call, whatever was asked.
  media     images and video in the request: how many, their bytes, their size (read from the
            image itself when it's inline), and detail where the provider takes it
  settings  temperature, top_p, max tokens, reasoning effort, thinking, seed: what was asked for

assay.instrument() reads them from each provider's request. run.llm(context=, media=, settings=)
takes them for a call it didn't see.
"""
from __future__ import annotations

import base64
import binascii
import json
import math
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

SETTINGS = ("temperature", "top_p", "top_k", "max_tokens", "max_completion_tokens", "max_output_tokens", "seed",
            "reasoning_effort", "frequency_penalty", "presence_penalty")


def tokens(v: Any) -> int:
    if v is None:
        return 0
    s = v if isinstance(v, str) else json.dumps(v, default=str, ensure_ascii=False)
    return math.ceil(len(s) / 4)


def _get(o: Any, k: str, default=None):
    return o.get(k, default) if isinstance(o, dict) else getattr(o, k, default)


# ---------- images: their size, from their own header ----------

def image_size(data: bytes) -> Optional[str]:
    """"1280x720" for a PNG, JPEG, GIF or WebP, from its header; None otherwise."""
    try:
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return f"{int.from_bytes(data[16:20], 'big')}x{int.from_bytes(data[20:24], 'big')}"
        if data[:6] in (b"GIF87a", b"GIF89a"):
            return f"{int.from_bytes(data[6:8], 'little')}x{int.from_bytes(data[8:10], 'little')}"
        if data[:4] == b"RIFF" and data[8:12] == b"WEBP" and data[12:16] == b"VP8 ":
            return f"{int.from_bytes(data[26:28], 'little') & 0x3FFF}x{int.from_bytes(data[28:30], 'little') & 0x3FFF}"
        if data[:2] == b"\xff\xd8":  # JPEG: walk the segments to a start-of-frame
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xFF:
                    return None
                marker, size = data[i + 1], int.from_bytes(data[i + 2:i + 4], "big")
                if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                    return f"{int.from_bytes(data[i + 7:i + 9], 'big')}x{int.from_bytes(data[i + 5:i + 7], 'big')}"
                i += 2 + size
    except (IndexError, ValueError):
        return None
    return None


def _b64(s: str) -> Optional[bytes]:
    if s.startswith("data:"):
        s = s.split(",", 1)[-1]
    try:
        return base64.b64decode(s[:200_000], validate=False)  # the header is at the start; that's enough
    except (binascii.Error, ValueError):
        return None


class _Media:
    def __init__(self):
        self.images, self.videos, self.bytes, self.sizes, self.detail = 0, 0, 0, Counter(), Counter()

    def add(self, kind: str, data: Optional[str] = None, detail: Optional[str] = None, nbytes: Optional[int] = None):
        if kind == "video":
            self.videos += 1
        else:
            self.images += 1
        if data:
            self.bytes += int(len(data) * 3 / 4) if nbytes is None else nbytes
            raw = _b64(data)
            size = image_size(raw) if raw and kind != "video" else None
            if size:
                self.sizes[size] += 1
        elif nbytes:
            self.bytes += nbytes
        if detail:
            self.detail[detail] += 1

    def out(self) -> Optional[dict]:
        if not (self.images or self.videos):
            return None
        m = {"images": self.images, "videos": self.videos, "bytes": self.bytes}
        if self.sizes:
            m["size"] = self.sizes.most_common(1)[0][0]
        if self.detail:
            m["detail"] = self.detail.most_common(1)[0][0]
        return m


# ---------- each provider's request ----------

def _parts(content: Any, media: _Media) -> str:
    """The text of a message's content, counting its images and video on the way."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        content = [content]
    text = []
    for p in content:
        t = _get(p, "type")
        if t in ("text", "input_text", "output_text"):
            text.append(_get(p, "text") or "")
        elif t == "image":  # Anthropic
            src = _get(p, "source") or {}
            media.add("image", _get(src, "data"))
        elif t in ("image_url", "input_image"):  # OpenAI
            iu = _get(p, "image_url")
            url = _get(iu, "url") if isinstance(iu, dict) or hasattr(iu, "url") else iu
            media.add("image", url if isinstance(url, str) and url.startswith("data:") else None,
                      _get(iu, "detail") if isinstance(iu, dict) else _get(p, "detail"))
        elif t in ("tool_result", "tool_use", "function_call", "function_call_output"):
            text.append(json.dumps(p if isinstance(p, dict) else str(p), default=str))
        elif _get(p, "inline_data") is not None or _get(p, "file_data") is not None:  # Gemini
            blob = _get(p, "inline_data") or _get(p, "file_data")
            mime = str(_get(blob, "mime_type") or "")
            data = _get(blob, "data")
            media.add("video" if mime.startswith("video") else "image",
                      data if isinstance(data, str) else None, nbytes=len(data) if isinstance(data, bytes) else None)
        elif _get(p, "text") is not None:
            text.append(_get(p, "text"))
        else:
            text.append(json.dumps(p, default=str) if isinstance(p, dict) else str(p))
    return "\n".join(text)


def _split(messages: List[Any], media: _Media) -> Tuple[int, int, int]:
    """(system, history, user) tokens of a chat's messages: the last user message is the user's."""
    system = history = user = 0
    last_user = max((i for i, m in enumerate(messages) if _get(m, "role") == "user"), default=None)
    for i, m in enumerate(messages):
        role = _get(m, "role")
        text = _parts(_get(m, "content"), media)
        for img in _get(m, "images") or []:  # Ollama
            media.add("image", img if isinstance(img, str) else None)
        n = tokens(text) + tokens(_get(m, "tool_calls"))
        if role in ("system", "developer"):
            system += n
        elif i == last_user:
            user += n
        else:
            history += n
    return system, history, user


def settings_of(kw: dict) -> Optional[dict]:
    out = {k: kw[k] for k in SETTINGS if isinstance(kw.get(k), (int, float, str, bool))}
    reasoning = kw.get("reasoning")
    if isinstance(reasoning, dict) and reasoning.get("effort"):
        out["reasoning_effort"] = reasoning["effort"]
    thinking = kw.get("thinking")
    if isinstance(thinking, dict) and thinking.get("type"):
        out["thinking"] = thinking.get("type") + (f":{thinking['budget_tokens']}" if thinking.get("budget_tokens") else "")
    cfg = kw.get("config")
    if cfg is not None:  # Gemini keeps them in config
        for k in ("temperature", "top_p", "top_k", "max_output_tokens", "seed"):
            v = _get(cfg, k)
            if isinstance(v, (int, float)):
                out[k] = v
    return out or None


def describe(provider: str, kw: dict) -> Dict[str, Optional[dict]]:
    """{"context", "media", "settings"} for one request, as `provider` takes it."""
    media = _Media()
    ctx = {"system": 0, "tools": tokens(kw.get("tools")) if kw.get("tools") else 0, "history": 0, "user": 0}
    if provider == "gemini":
        cfg = kw.get("config")
        ctx["system"] += tokens(_parts(_get(cfg, "system_instruction"), media)) if cfg is not None else 0
        ctx["tools"] += tokens(_get(cfg, "tools")) if cfg is not None and _get(cfg, "tools") else 0
        contents = kw.get("contents")
        items = contents if isinstance(contents, list) else [contents] if contents is not None else []
        for i, c in enumerate(items):
            text = _parts(_get(c, "parts") if not isinstance(c, str) else c, media)
            ctx["user" if i == len(items) - 1 else "history"] += tokens(text)
    elif "input" in kw and "messages" not in kw:  # OpenAI Responses
        ctx["system"] += tokens(kw.get("instructions"))
        inp = kw["input"]
        if isinstance(inp, str):
            ctx["user"] += tokens(inp)
        else:
            s, h, u = _split(list(inp or []), media)
            ctx["system"] += s
            ctx["history"] += h
            ctx["user"] += u
    else:  # Anthropic, OpenAI chat, Ollama, LiteLLM
        system = kw.get("system")
        ctx["system"] += tokens(_parts(system, media)) if system else 0
        s, h, u = _split(list(kw.get("messages") or []), media)
        ctx["system"] += s
        ctx["history"] += h
        ctx["user"] += u
    return {"context": {k: v for k, v in ctx.items() if v} or None, "media": media.out(),
            "settings": settings_of(kw)}


def fixed(context: Optional[dict]) -> int:
    """The fixed context of a call: system prompt and tool definitions, paid whatever was asked."""
    c = context or {}
    return (c.get("system") or 0) + (c.get("tools") or 0)
