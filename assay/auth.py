"""API keys, scopes and tenant isolation.

Every request is made by a *principal*: an API key bound to one tenant (or to
"*", every tenant, for platform operators) with a set of scopes:

  ingest   send events (a pipeline's key)
  read     read results: dashboard, measures, alerts, traces, cost
  manage   run measures, backfill, set SLOs and rates, record gate decisions (includes read)
  admin    create and revoke keys (includes everything)

A tenant key only ever sees its own tenant: its source is events:<tenant>,
and a different X-Tenant header is refused. Keys are stored as SHA-256
hashes and shown once, at creation.

Open mode: with no keys created and no ASSAY_ADMIN_KEY set, the API runs
without authentication (for local use and demos) and says so in /v1/whoami.
Open mode never grants `admin`, so nobody can mint keys on an open server;
the first key comes from the CLI or from ASSAY_ADMIN_KEY.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Dict, FrozenSet, Iterable, Optional, Tuple

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store

SCOPES = ("ingest", "read", "manage", "admin")
_IMPLIES = {"admin": set(SCOPES), "manage": {"manage", "read"}, "read": {"read"}, "ingest": {"ingest"}}
KEY_PREFIX = "ak_"


def expand(scopes: Iterable[str]) -> FrozenSet[str]:
    out = set()
    for s in scopes:
        if s not in _IMPLIES:
            raise ValueError(f"Unknown scope '{s}'. Scopes: {', '.join(SCOPES)}.")
        out |= _IMPLIES[s]
    return frozenset(out)


@dataclass(frozen=True)
class Principal:
    tenant: str  # "*" = every tenant
    scopes: FrozenSet[str]
    key_id: Optional[int] = None
    name: str = ""
    mode: str = "key"  # key | admin_key | open | sso
    user_id: Optional[str] = None  # sso: the person (assay/sso.py)

    @property
    def platform(self) -> bool:
        return self.tenant == "*"

    def can(self, scope: str) -> bool:
        return scope in self.scopes

    def can_source(self, source: str) -> bool:
        return self.platform or source == f"events:{self.tenant}"

    def write_tenant(self, requested: Optional[str]) -> str:
        """The tenant events are written to. Tenant keys can't write elsewhere."""
        if self.platform:
            return requested or "default"
        if requested and requested != self.tenant:
            raise PermissionError(f"This key belongs to tenant '{self.tenant}' and can't write to '{requested}'.")
        return self.tenant

    def public(self) -> dict:
        return {"tenant": self.tenant, "scopes": sorted(self.scopes), "key_id": self.key_id,
                "name": self.name, "mode": self.mode, **({"user_id": self.user_id} if self.user_id else {})}


OPEN = Principal("*", frozenset({"ingest", "read", "manage"}), name="open mode", mode="open")


def hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def create_key(engine: Engine, tenant: str, name: str, scopes: Iterable[str],
               expires_in_days: Optional[int] = None) -> Tuple[dict, str]:
    scopes = sorted(set(scopes))
    expand(scopes)  # validates
    if not tenant or (tenant != "*" and not tenant.replace("-", "").replace("_", "").isalnum()):
        raise ValueError("Tenant must be letters, digits, - or _ (or * for every tenant).")
    key = KEY_PREFIX + secrets.token_urlsafe(32)
    now = datetime.utcnow()
    row = dict(key_hash=hash_key(key), prefix=key[:10], name=name or "", tenant=tenant,
               scopes=",".join(scopes), created_at=now,
               expires_at=now + timedelta(days=expires_in_days) if expires_in_days else None)
    with engine.begin() as conn:
        row["id"] = conn.execute(store.api_keys.insert().values(**row)).inserted_primary_key[0]
    return public_key(row), key


def public_key(row) -> dict:
    r = dict(row._mapping) if hasattr(row, "_mapping") else dict(row)
    iso = lambda v: v.isoformat() if v else None
    return {"id": r["id"], "prefix": r["prefix"], "name": r["name"], "tenant": r["tenant"],
            "scopes": r["scopes"].split(",") if r["scopes"] else [], "created_at": iso(r["created_at"]),
            "expires_at": iso(r.get("expires_at")), "last_used_at": iso(r.get("last_used_at")),
            "revoked_at": iso(r.get("revoked_at"))}


def any_keys(engine: Engine) -> bool:
    with engine.connect() as conn:
        return conn.execute(select(store.api_keys.c.id).limit(1)).first() is not None


class Authenticator:
    def __init__(self, engine: Engine, admin_key: Optional[str], mode: str = "auto"):
        self.engine, self.admin_key, self.mode = engine, admin_key, mode
        self.sso = None  # an assay.sso.Config when people sign in with SSO
        self._keys_exist_until = 0.0
        self._keys_exist = False

    def required(self) -> bool:
        if self.sso:  # people sign in: never open
            return True
        if self.mode == "off":
            return False
        if self.mode == "required" or self.admin_key:
            return True
        # Cache the "has anyone created a key yet" check briefly; it flips once.
        if time.monotonic() > self._keys_exist_until:
            self._keys_exist = any_keys(self.engine)
            self._keys_exist_until = time.monotonic() + (300 if self._keys_exist else 5)
        return self._keys_exist

    def authenticate(self, token: Optional[str]) -> Optional[Principal]:
        """The principal for a presented token; OPEN when auth isn't required; None if rejected."""
        if not token:
            return None if self.required() else OPEN
        if self.admin_key and hmac.compare_digest(token, self.admin_key):
            return Principal("*", expand(["admin"]), name="ASSAY_ADMIN_KEY", mode="admin_key")
        t = store.api_keys
        now = datetime.utcnow()
        with self.engine.connect() as conn:
            row = conn.execute(select(t).where(t.c.key_hash == hash_key(token))).first()
        if row is None or row.revoked_at is not None or (row.expires_at and row.expires_at <= now):
            return None  # a wrong key is refused even in open mode: it's a mistake worth surfacing
        if row.last_used_at is None or now - row.last_used_at > timedelta(minutes=1):
            with self.engine.begin() as conn:  # throttled, so it isn't a write per request
                conn.execute(t.update().where(t.c.id == row.id).values(last_used_at=now))
        return Principal(row.tenant, expand(row.scopes.split(",")), row.id, row.name)


    def session(self, cookie: Optional[str]) -> Optional[Principal]:
        """The person a session cookie belongs to, while it's valid and they're not disabled."""
        if self.sso is None or not cookie:
            return None
        from assay import sso
        v = sso.unsign(self.sso.session_secret, cookie)
        u = sso.get_user(self.engine, v["uid"]) if v else None
        if u is None or u["disabled"]:
            return None
        return Principal(u["tenant"], expand([u["role"]]), name=u["email"] or u["id"], mode="sso", user_id=u["id"])


class RateLimiter:
    """Fixed one-minute window per key. In memory, so per instance."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self._counts: Dict[str, Tuple[int, int]] = {}
        self._lock = threading.Lock()

    def check(self, who: str) -> Optional[int]:
        """None if allowed, else seconds until the window resets."""
        if self.per_minute <= 0:
            return None
        now = time.time()
        window = int(now // 60)
        with self._lock:
            w, n = self._counts.get(who, (window, 0))
            if w != window:
                w, n = window, 0
            if n >= self.per_minute:
                return int(60 - now % 60) + 1
            self._counts[who] = (w, n + 1)
            if len(self._counts) > 10000:  # drop stale windows
                self._counts = {k: v for k, v in self._counts.items() if v[0] == window}
        return None


def revoke_key(engine: Engine, key_id: int, tenant: Optional[str] = None) -> bool:
    t = store.api_keys
    cond = [t.c.id == key_id, t.c.revoked_at.is_(None)]
    if tenant and tenant != "*":
        cond.append(t.c.tenant == tenant)
    with engine.begin() as conn:
        return conn.execute(t.update().where(and_(*cond)).values(revoked_at=datetime.utcnow())).rowcount > 0


def list_keys(engine: Engine, tenant: Optional[str] = None) -> list:
    t = store.api_keys
    q = select(t).order_by(t.c.id)
    if tenant and tenant != "*":
        q = q.where(t.c.tenant == tenant)
    with engine.connect() as conn:
        return [public_key(r) for r in conn.execute(q)]
