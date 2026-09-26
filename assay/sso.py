"""Single sign-on (OpenID Connect), users and roles, and the audit log.

People sign in with the company's identity provider instead of pasting an API key: Okta,
Microsoft Entra ID, Google Workspace, Auth0, Keycloak, or anything that speaks OpenID Connect.

  ASSAY_OIDC_ISSUER=https://acme.okta.com       ASSAY_OIDC_CLIENT_ID=...   ASSAY_OIDC_CLIENT_SECRET=...
  ASSAY_SESSION_SECRET=<32+ random bytes>        ASSAY_PUBLIC_URL=https://assay.acme.internal

The flow is the authorization code flow with PKCE, a state and a nonce. The ID token is verified
here: its signature against the provider's published keys (JWKS), its issuer, its audience (this
client), its expiry, and the nonce this login sent. Nothing is trusted because the browser says so.

Roles are scopes (assay/auth.py): read, manage, admin. They come from a claim (groups, by
default) on every sign-in: someone in a group listed in ASSAY_OIDC_ADMINS is an admin, in
ASSAY_OIDC_MANAGERS a manager, and anyone else who may sign in reads. ASSAY_OIDC_ALLOWED_DOMAINS
limits who may sign in by email domain. An admin can set a person's role or disable them
(PUT /v1/users/{id}); that holds over the claims until it's cleared.

A session is a signed, HttpOnly cookie that expires (ASSAY_SESSION_HOURS). A change made with a
session also needs the X-CSRF-Token header to match the session's CSRF cookie, so another site
can't make one on a signed-in person's behalf. API keys keep working as before, for pipelines and CI.

Every change is in the audit log (GET /v1/audit): who (a person, a key, or the admin key), what
(method and path), when, and whether it worked; and every sign-in, refused sign-in and sign-out.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import and_, desc, select
from sqlalchemy.engine import Engine

from assay import store

SESSION, FLOW, CSRF = "assay_session", "assay_oidc", "assay_csrf"
ROLES = ("read", "manage", "admin")
FLOW_SECONDS = 600
UNAUDITED = ("/v1/ingest", "/v1/events", "/v1/otlp", "/v1/traces")  # high volume, and not settings


class SSOError(Exception):
    pass


# ---------- signed values ----------

def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode()


def _unb64(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def sign(secret: str, value: dict) -> str:
    body = _b64(json.dumps(value, separators=(",", ":")).encode())
    mac = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}"


def unsign(secret: str, token: Optional[str]) -> Optional[dict]:
    """The value, if the signature holds and it hasn't expired (exp)."""
    if not token or "." not in token:
        return None
    body, mac = token.rsplit(".", 1)
    good = _b64(hmac.new(secret.encode(), body.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, good):
        return None
    try:
        value = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    return value if value.get("exp", 0) > time.time() else None


# ---------- the provider ----------

def _get_json(url: str, timeout: float = 10) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers={"Accept": "application/json"}),
                                timeout=timeout) as r:
        return json.loads(r.read())


@dataclass
class Config:
    issuer: str
    client_id: str
    client_secret: Optional[str]
    redirect_url: str
    session_secret: str
    admins: List[str] = field(default_factory=list)
    managers: List[str] = field(default_factory=list)
    role_claim: str = "groups"
    allowed_domains: List[str] = field(default_factory=list)
    tenant: str = "default"
    tenant_claim: Optional[str] = None
    session_hours: float = 12.0
    scopes: str = "openid email profile"

    @property
    def secure(self) -> bool:
        return self.redirect_url.startswith("https://")


def from_settings(settings) -> Optional[Config]:
    """The SSO config, or None when it isn't set up."""
    if not getattr(settings, "oidc_issuer", None):
        return None
    missing = [n for n, v in (("ASSAY_OIDC_CLIENT_ID", settings.oidc_client_id),
                              ("ASSAY_SESSION_SECRET", settings.session_secret)) if not v]
    if missing:
        raise SSOError(f"SSO needs {' and '.join(missing)} (ASSAY_OIDC_ISSUER is set).")
    if len(settings.session_secret) < 32:
        raise SSOError("ASSAY_SESSION_SECRET must be at least 32 characters: it signs every session.")
    base = (settings.public_url or "").rstrip("/")
    redirect = settings.oidc_redirect_url or (f"{base}/auth/callback" if base else None)
    if not redirect:
        raise SSOError("SSO needs ASSAY_PUBLIC_URL (or ASSAY_OIDC_REDIRECT_URL): where the provider sends people back.")
    return Config(issuer=settings.oidc_issuer.rstrip("/"), client_id=settings.oidc_client_id,
                  client_secret=settings.oidc_client_secret, redirect_url=redirect,
                  session_secret=settings.session_secret, admins=list(settings.oidc_admins),
                  managers=list(settings.oidc_managers), role_claim=settings.oidc_role_claim or "groups",
                  allowed_domains=[d.lower().lstrip("@") for d in settings.oidc_allowed_domains],
                  tenant=settings.oidc_tenant or "default", tenant_claim=settings.oidc_tenant_claim,
                  session_hours=settings.session_hours)


class Provider:
    """One OpenID Connect provider: its discovery document and keys, cached."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._meta: Tuple[float, dict] = (0.0, {})
        self._keys: Tuple[float, dict] = (0.0, {})

    def meta(self) -> dict:
        with self._lock:
            if time.monotonic() - self._meta[0] > 3600 or not self._meta[1]:
                try:
                    m = _get_json(f"{self.cfg.issuer}/.well-known/openid-configuration")
                except (OSError, ValueError) as exc:
                    raise SSOError(f"Couldn't read the provider's configuration at {self.cfg.issuer}: {exc}")
                if m.get("issuer", "").rstrip("/") != self.cfg.issuer:
                    raise SSOError(f"The provider says its issuer is {m.get('issuer')!r}, not {self.cfg.issuer!r}.")
                self._meta = (time.monotonic(), m)
            return self._meta[1]

    def key(self, kid: Optional[str]):
        """The signing key with this id, refetching the key set once if it's new (keys rotate)."""
        import jwt
        for attempt in (0, 1):
            with self._lock:
                stale = attempt == 1 or time.monotonic() - self._keys[0] > 3600 or not self._keys[1]
            if stale:
                keys = _get_json(self.meta()["jwks_uri"]).get("keys", [])
                with self._lock:
                    self._keys = (time.monotonic(), {k.get("kid"): k for k in keys})
            with self._lock:
                jwk = self._keys[1].get(kid) or (next(iter(self._keys[1].values())) if kid is None and
                                                 len(self._keys[1]) == 1 else None)
            if jwk is not None:
                return jwt.PyJWK(jwk).key
        raise SSOError(f"The ID token was signed with a key the provider doesn't publish ({kid}).")

    def login_url(self, state: str, nonce: str, verifier: str) -> str:
        challenge = _b64(hashlib.sha256(verifier.encode()).digest())
        q = {"response_type": "code", "client_id": self.cfg.client_id, "redirect_uri": self.cfg.redirect_url,
             "scope": self.cfg.scopes, "state": state, "nonce": nonce, "code_challenge": challenge,
             "code_challenge_method": "S256"}
        return f"{self.meta()['authorization_endpoint']}?{urllib.parse.urlencode(q)}"

    def exchange(self, code: str, verifier: str) -> str:
        """The authorization code for the ID token."""
        body = {"grant_type": "authorization_code", "code": code, "redirect_uri": self.cfg.redirect_url,
                "client_id": self.cfg.client_id, "code_verifier": verifier}
        if self.cfg.client_secret:
            body["client_secret"] = self.cfg.client_secret
        req = urllib.request.Request(self.meta()["token_endpoint"], data=urllib.parse.urlencode(body).encode(),
                                     headers={"Content-Type": "application/x-www-form-urlencoded",
                                              "Accept": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                tokens = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise SSOError(f"The provider refused the sign-in ({exc.code}): {detail}")
        except (OSError, ValueError) as exc:
            raise SSOError(f"Couldn't reach the provider's token endpoint: {exc}")
        if not tokens.get("id_token"):
            raise SSOError("The provider's answer had no ID token (is the openid scope allowed?).")
        return tokens["id_token"]

    def verify(self, id_token: str, nonce: str) -> dict:
        """The ID token's claims, once its signature, issuer, audience, expiry and nonce hold."""
        import jwt
        try:
            head = jwt.get_unverified_header(id_token)
            if head.get("alg") in (None, "none", "HS256", "HS384", "HS512"):
                raise SSOError(f"The ID token's algorithm ({head.get('alg')}) isn't a public-key signature.")
            claims = jwt.decode(id_token, self.key(head.get("kid")), algorithms=["RS256", "RS384", "RS512",
                                                                                "ES256", "ES384", "PS256"],
                                audience=self.cfg.client_id, issuer=self.meta()["issuer"], leeway=60,
                                options={"require": ["exp", "iat", "sub", "aud", "iss"]})
        except jwt.PyJWTError as exc:
            raise SSOError(f"The ID token didn't verify: {exc}")
        if not hmac.compare_digest(str(claims.get("nonce") or ""), nonce):
            raise SSOError("The ID token's nonce isn't this sign-in's: it may have been replayed.")
        return claims


# ---------- people ----------

def _groups(claims: dict, claim: str) -> List[str]:
    v = claims.get(claim)
    return [str(x) for x in v] if isinstance(v, list) else [str(v)] if v else []


def role_for(cfg: Config, claims: dict) -> str:
    who = {str(claims.get("email") or "").lower(), *_groups(claims, cfg.role_claim)}
    if who & {a.lower() if "@" in a else a for a in cfg.admins}:
        return "admin"
    if who & {m.lower() if "@" in m else m for m in cfg.managers}:
        return "manage"
    return "read"


def admitted(cfg: Config, claims: dict) -> Optional[str]:
    """Why someone may not sign in, or None."""
    email = str(claims.get("email") or "").lower()
    if cfg.allowed_domains:
        if not email or claims.get("email_verified") is False:
            return "a verified email address is needed to sign in here"
        if email.rsplit("@", 1)[-1] not in cfg.allowed_domains:
            return f"{email} isn't in an allowed domain ({', '.join(cfg.allowed_domains)})"
    return None


def user_id(issuer: str, sub: str) -> str:
    return hashlib.sha256(f"{issuer}|{sub}".encode()).hexdigest()[:32]


def upsert_user(engine: Engine, cfg: Config, claims: dict) -> dict:
    """The person, as of this sign-in: their claims' role unless an admin set one."""
    t = store.users
    uid = user_id(cfg.issuer, claims["sub"])
    tenant = str(claims.get(cfg.tenant_claim) or cfg.tenant) if cfg.tenant_claim else cfg.tenant
    now = datetime.utcnow()
    fields = {"email": claims.get("email"), "name": claims.get("name") or claims.get("preferred_username"),
              "claims_role": role_for(cfg, claims), "last_login_at": now, "tenant": tenant}
    with engine.begin() as conn:
        row = conn.execute(select(t).where(t.c.id == uid)).first()
        if row is None:
            conn.execute(t.insert().values(id=uid, issuer=cfg.issuer, subject=claims["sub"], created_at=now,
                                           disabled=False, **fields))
        else:
            conn.execute(t.update().where(t.c.id == uid).values(**fields))
        row = conn.execute(select(t).where(t.c.id == uid)).first()
    return public_user(row)


def public_user(row) -> dict:
    r = dict(row._mapping)
    iso = lambda v: v.isoformat() if v else None
    return {"id": r["id"], "email": r["email"], "name": r["name"], "tenant": r["tenant"],
            "role": r["role"] or r["claims_role"], "role_set_by_admin": bool(r["role"]),
            "claims_role": r["claims_role"], "disabled": bool(r["disabled"]),
            "created_at": iso(r["created_at"]), "last_login_at": iso(r["last_login_at"])}


def get_user(engine: Engine, uid: str) -> Optional[dict]:
    with engine.connect() as conn:
        row = conn.execute(select(store.users).where(store.users.c.id == uid)).first()
    return public_user(row) if row else None


def list_users(engine: Engine, tenant: Optional[str] = None) -> List[dict]:
    t = store.users
    q = select(t).order_by(t.c.email)
    if tenant and tenant != "*":
        q = q.where(t.c.tenant == tenant)
    with engine.connect() as conn:
        return [public_user(r) for r in conn.execute(q)]


def set_user(engine: Engine, uid: str, role: Optional[str] = "", disabled: Optional[bool] = None,
             tenant: Optional[str] = None) -> Optional[dict]:
    """An admin's decision: a role that holds over the claims (None clears it), or disabling them."""
    t = store.users
    values: Dict[str, Any] = {}
    if role != "":
        if role is not None and role not in ROLES:
            raise ValueError(f"role is one of {', '.join(ROLES)}, or null to go back to what the claims say.")
        values["role"] = role
    if disabled is not None:
        values["disabled"] = disabled
    cond = [t.c.id == uid] + ([t.c.tenant == tenant] if tenant and tenant != "*" else [])
    with engine.begin() as conn:
        if values:
            conn.execute(t.update().where(and_(*cond)).values(**values))
        row = conn.execute(select(t).where(and_(*cond))).first()
    return public_user(row) if row else None


# ---------- the audit log ----------

def audit(engine: Engine, actor: str, action: str, tenant: Optional[str] = None, status: Optional[int] = None,
          detail: Optional[dict] = None) -> None:
    with engine.begin() as conn:
        conn.execute(store.audit_log.insert().values(ts=datetime.utcnow(), actor=actor[:256], action=action[:512],
                                                     tenant=tenant, status=status, detail=detail))


def actor_of(p) -> str:
    if p is None:
        return "anonymous"
    if p.mode == "sso":
        return f"user:{p.name}"
    if p.mode == "admin_key":
        return "ASSAY_ADMIN_KEY"
    if p.mode == "open":
        return "open mode"
    return f"key:{p.key_id} ({p.name})" if p.name else f"key:{p.key_id}"


def audited(method: str, path: str) -> bool:
    return method in ("POST", "PUT", "PATCH", "DELETE") and path.startswith(("/v1/", "/auth/")) and \
        not path.startswith(UNAUDITED)


def read_audit(engine: Engine, tenant: Optional[str] = None, actor: Optional[str] = None,
               limit: int = 200) -> List[dict]:
    t = store.audit_log
    q = select(t).order_by(desc(t.c.id)).limit(min(max(limit, 1), 1000))
    if tenant and tenant != "*":
        q = q.where(t.c.tenant == tenant)
    if actor:
        q = q.where(t.c.actor.contains(actor))
    with engine.connect() as conn:
        return [{**{k: v for k, v in r._mapping.items() if k != "ts"}, "ts": r.ts.isoformat()} for r in conn.execute(q)]


def new_flow(cfg: Config, next_url: str) -> Tuple[str, dict]:
    """(signed cookie, values) for one sign-in: its state, nonce and PKCE verifier."""
    v = {"state": secrets.token_urlsafe(24), "nonce": secrets.token_urlsafe(24),
         "verifier": secrets.token_urlsafe(48), "next": next_url if next_url.startswith("/") and not
         next_url.startswith("//") else "/", "exp": time.time() + FLOW_SECONDS}
    return sign(cfg.session_secret, v), v


def new_session(cfg: Config, user: dict) -> Tuple[str, str]:
    """(signed session cookie, CSRF token)."""
    csrf = secrets.token_urlsafe(24)
    return sign(cfg.session_secret, {"uid": user["id"], "csrf": csrf,
                                     "exp": time.time() + cfg.session_hours * 3600}), csrf
