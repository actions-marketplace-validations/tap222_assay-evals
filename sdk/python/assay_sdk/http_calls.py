"""Outbound HTTP calls, recorded into the run in progress: what the code did, not only what it returned.

    from assay_sdk import http_calls
    http_calls.record()  # pytest --assay --assay-all turns it on for you

Every request made through http.client (so urllib and requests) or httpx, inside a run, becomes a
tool step named after its method, host and path ("HTTP POST api.stripe.com/v1/charges"), with the
status as its result. The behavior diff then shows a call that appeared, went away or changed
between runs, like any other tool call.

What changes on every run is left out, so the same call is the same step each time: ids in the path
become {id} (numbers, UUIDs, long hex), the query keeps its keys and drops their values, and no
headers or bodies are kept (they can hold secrets). Calls to the Assay server, and to model APIs
(instrument() records those as model calls), are skipped. Recording never changes or breaks a
request: anything it can't read is left out.
"""
from __future__ import annotations

import re
import threading
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import parse_qsl, urlsplit

import assay_sdk as assay

# Model APIs: instrument() records those calls with their tokens and tools, so they aren't HTTP steps too.
MODEL_HOSTS = ("api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com",
               "aiplatform.googleapis.com", "api.mistral.ai", "api.cohere.com", "api.groq.com")
_ID = re.compile(r"^(\d+|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}|[0-9a-fA-F]{16,})$")
_lock = threading.Lock()
_on = False
_local = threading.local()  # set while a call is being recorded, so httpx over http.client isn't counted twice


def template(url: str) -> str:
    """host/path?keys, the same for every run of the same call: api.x.com/orders/{id}?page"""
    parts = urlsplit(url)
    path = "/".join("{id}" if _ID.match(seg) else seg for seg in parts.path.split("/"))
    keys = sorted({k for k, _ in parse_qsl(parts.query, keep_blank_values=True)})
    return f"{parts.hostname or ''}{path or '/'}" + (f"?{'&'.join(keys)}" if keys else "")


def _skipped(host: str) -> bool:
    import os
    server = urlsplit(os.environ.get("ASSAY_URL") or "").hostname
    host = (host or "").lower()
    return bool(server and host == server) or any(host == h or host.endswith(("." + h, "-" + h)) for h in MODEL_HOSTS)


def _record(method: str, url: str, started: datetime, status: Optional[int], error: Optional[str]) -> None:
    run = assay.current()
    if run is None:
        return
    try:
        where = template(url)
        if _skipped(urlsplit(url).hostname or ""):
            return
        method = (method or "GET").upper()
        run.tool(f"HTTP {method} {where}"[:128], {"method": method},  # the URL is in the name: a host as an argument reads as personal data
                 result={"status": status} if status is not None else None, error=error,
                 started=started, ended=datetime.now(timezone.utc))
    except Exception:  # the recording never breaks the request
        pass


def _patch_http_client() -> None:
    import http.client as hc
    put, get = hc.HTTPConnection.putrequest, hc.HTTPConnection.getresponse

    def putrequest(self, method, url, *a, **k):
        try:
            scheme = "https" if isinstance(self, hc.HTTPSConnection) else "http"
            full = url if "://" in url else f"{scheme}://{self.host}{'' if self.port in (80, 443, None) else f':{self.port}'}{url}"
            self._assay_call = (method, full, datetime.now(timezone.utc))
        except Exception:
            self._assay_call = None
        return put(self, method, url, *a, **k)

    def getresponse(self, *a, **k):
        call = getattr(self, "_assay_call", None)
        self._assay_call = None
        if call is None or getattr(_local, "busy", False):
            return get(self, *a, **k)
        try:
            resp = get(self, *a, **k)
        except Exception as exc:
            _record(call[0], call[1], call[2], None, f"{type(exc).__name__}: {exc}"[:500])
            raise
        _record(call[0], call[1], call[2], getattr(resp, "status", None), None)
        return resp

    hc.HTTPConnection.putrequest, hc.HTTPConnection.getresponse = putrequest, getresponse


def _patch_httpx() -> None:
    try:
        import httpx
    except ImportError:
        return
    send, asend = httpx.Client.send, httpx.AsyncClient.send

    def _done(request, started, resp, exc):
        _record(request.method, str(request.url), started, getattr(resp, "status_code", None),
                f"{type(exc).__name__}: {exc}"[:500] if exc else None)

    def sync_send(self, request, *a, **k):
        started, _local.busy = datetime.now(timezone.utc), True
        try:
            resp = send(self, request, *a, **k)
        except Exception as exc:
            _done(request, started, None, exc)
            raise
        finally:
            _local.busy = False
        _done(request, started, resp, None)
        return resp

    async def async_send(self, request, *a, **k):
        started = datetime.now(timezone.utc)
        try:
            resp = await asend(self, request, *a, **k)
        except Exception as exc:
            _done(request, started, None, exc)
            raise
        _done(request, started, resp, None)
        return resp

    httpx.Client.send, httpx.AsyncClient.send = sync_send, async_send


def record() -> bool:
    """Record outbound HTTP calls into the run in progress, from now on. Safe to call twice."""
    global _on
    with _lock:
        if _on:
            return False
        _patch_http_client()
        _patch_httpx()
        _on = True
    return True
