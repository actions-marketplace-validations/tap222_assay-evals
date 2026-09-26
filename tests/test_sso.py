"""SSO (assay/sso.py): sign in with an OpenID Connect provider, roles from groups, sessions with CSRF,
people an admin can change, and the audit log. The provider here is a real HTTP server with a real
RSA key: the ID token is verified exactly as it would be against Okta or Entra."""
import base64
import hashlib
import json
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

jwt = pytest.importorskip("jwt")
from cryptography.hazmat.primitives.asymmetric import rsa  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from assay import sso  # noqa: E402
from assay.api import create_app  # noqa: E402
from assay.config import Settings  # noqa: E402

SECRET = "s" * 40


class IdP:
    """A minimal OpenID Connect provider: discovery, keys, and a token endpoint that checks PKCE."""

    def __init__(self):
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.codes = {}
        idp = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, body, code=200):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/.well-known/openid-configuration":
                    return self._json({"issuer": idp.url, "authorization_endpoint": f"{idp.url}/authorize",
                                       "token_endpoint": f"{idp.url}/token", "jwks_uri": f"{idp.url}/jwks"})
                if self.path == "/jwks":
                    k = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(idp.key.public_key()))
                    return self._json({"keys": [{**k, "kid": "k1", "use": "sig", "alg": "RS256"}]})
                self._json({}, 404)

            def do_POST(self):
                form = dict(urllib.parse.parse_qsl(self.rfile.read(int(self.headers["Content-Length"])).decode()))
                grant = idp.codes.pop(form.get("code"), None)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(form.get("code_verifier", "").encode()).digest()
                                                     ).rstrip(b"=").decode()
                if grant is None or challenge != grant["challenge"]:
                    return self._json({"error": "invalid_grant"}, 400)
                self._json({"id_token": grant["token"], "token_type": "Bearer"})

        self.server = HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def token(self, claims, *, key=None, kid="k1"):
        now = int(time.time())
        body = {"iss": self.url, "aud": "assay", "iat": now, "exp": now + 300, **claims}
        return jwt.encode(body, key or self.key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def idp():
    p = IdP()
    yield p
    p.server.shutdown()


def app(tmp_path, idp, **kw):
    s = Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", oidc_issuer=idp.url, oidc_client_id="assay",
                 oidc_client_secret="shh", session_secret=SECRET, public_url="http://testserver",
                 oidc_admins=["assay-admins"], oidc_managers=["ml-team"], **kw)
    return TestClient(create_app(s))


def sign_in(c, idp, claims, *, token_claims=None, key=None, state=None):
    r = c.get("/auth/login", params={"next": "/?tab=agents"}, follow_redirects=False)
    assert r.status_code == 302
    q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
    assert q["code_challenge_method"] == "S256" and q["redirect_uri"] == "http://testserver/auth/callback"
    code = f"code-{len(idp.codes)}-{time.time()}"
    idp.codes[code] = {"challenge": q["code_challenge"],
                       "token": idp.token({"nonce": q["nonce"], **claims, **(token_claims or {})}, key=key)}
    return c.get("/auth/callback", params={"code": code, "state": state or q["state"]}, follow_redirects=False)


def csrf(c):
    return {"X-CSRF-Token": c.cookies.get(sso.CSRF)}


def test_an_admin_signs_in_and_every_change_is_audited(tmp_path, idp):
    c = app(tmp_path, idp)
    assert c.get("/v1/measures").status_code == 401  # never open mode with SSO on
    assert c.get("/auth/config").json() == {"sso": True, "login": "/auth/login"}
    r = sign_in(c, idp, {"sub": "u1", "email": "ana@acme.com", "name": "Ana", "groups": ["assay-admins"]})
    assert r.status_code == 302 and r.headers["location"] == "/?tab=agents"
    me = c.get("/v1/whoami").json()
    assert (me["mode"], me["name"], me["tenant"]) == ("sso", "ana@acme.com", "default") and "admin" in me["scopes"]
    body = {"kind": "never", "step": "delete_order"}
    assert c.post("/v1/contracts", json=body).status_code == 403  # a session change without its CSRF token
    assert c.post("/v1/contracts", json=body, headers=csrf(c)).status_code == 201
    log = c.get("/v1/audit").json()
    assert [(x["actor"], x["action"], x["status"]) for x in log[:3]] == [
        ("user:ana@acme.com", "POST /v1/contracts", 201), ("user:ana@acme.com", "POST /v1/contracts", 403),
        ("user:ana@acme.com", "sign-in", 200)]
    c.post("/auth/logout")
    assert c.get("/v1/whoami").status_code == 401


def test_roles_come_from_groups_and_an_admin_can_override_or_disable(tmp_path, idp):
    admin, reader = app(tmp_path, idp), app(tmp_path, idp)
    sign_in(admin, idp, {"sub": "a", "email": "ana@acme.com", "groups": ["assay-admins"]})
    sign_in(reader, idp, {"sub": "b", "email": "bo@acme.com", "groups": ["sales"]})
    assert reader.get("/v1/whoami").json()["scopes"] == ["read"]
    assert reader.post("/v1/contracts", json={"kind": "never", "step": "x"}, headers=csrf(reader)).status_code == 403
    bo = next(u for u in admin.get("/v1/users").json() if u["email"] == "bo@acme.com")
    assert (bo["role"], bo["role_set_by_admin"]) == ("read", False)
    r = admin.put(f"/v1/users/{bo['id']}", json={"role": "manage"}, headers=csrf(admin))
    assert r.json()["role"] == "manage" and r.json()["role_set_by_admin"]
    assert "manage" in reader.get("/v1/whoami").json()["scopes"]  # holds over the claims
    admin.put(f"/v1/users/{bo['id']}", json={"disabled": True}, headers=csrf(admin))
    assert reader.get("/v1/whoami").status_code == 401  # a disabled person's session stops working
    me = admin.get("/v1/whoami").json()["user_id"]
    assert admin.put(f"/v1/users/{me}", json={"disabled": True}, headers=csrf(admin)).status_code == 400
    ml = app(tmp_path, idp)
    sign_in(ml, idp, {"sub": "m", "email": "mo@acme.com", "groups": ["ml-team"]})
    assert "manage" in ml.get("/v1/whoami").json()["scopes"] and "admin" not in ml.get("/v1/whoami").json()["scopes"]


@pytest.mark.parametrize("why, kw", [
    ("nonce", {"token_claims": {"nonce": "someone-elses"}}),
    ("didn't verify", {"key": "other"}),
    ("wasn't started here", {"state": "forged"}),
    ("didn't verify", {"token_claims": {"aud": "another-app"}}),
    ("didn't verify", {"token_claims": {"exp": int(time.time()) - 3600}}),
])
def test_a_sign_in_that_doesnt_verify_is_refused(tmp_path, idp, why, kw):
    c = app(tmp_path, idp)
    if kw.get("key") == "other":
        kw = {**kw, "key": idp.other}
    r = sign_in(c, idp, {"sub": "u", "email": "eve@acme.com"}, **kw)
    assert r.status_code == 403 and why in r.json()["detail"], r.json()
    assert c.get("/v1/whoami").status_code == 401


def test_only_allowed_domains_sign_in_and_refusals_are_audited(tmp_path, idp):
    c = app(tmp_path, idp, oidc_allowed_domains=["acme.com"])
    r = sign_in(c, idp, {"sub": "x", "email": "mallory@evil.com", "email_verified": True})
    assert r.status_code == 403 and "isn't in an allowed domain" in r.json()["detail"]
    admin = app(tmp_path, idp, oidc_allowed_domains=["acme.com"])
    sign_in(admin, idp, {"sub": "a", "email": "ana@acme.com", "email_verified": True, "groups": ["assay-admins"]})
    refused = [x for x in admin.get("/v1/audit").json() if x["action"] == "sign-in refused"]
    assert refused and refused[0]["actor"] == "user:mallory@evil.com"


def test_api_keys_keep_working_alongside_sso(tmp_path, idp):
    c = app(tmp_path, idp, admin_key="ak_admin_for_tests")
    h = {"Authorization": "Bearer ak_admin_for_tests"}
    assert c.post("/v1/contracts", json={"kind": "never", "step": "x"}, headers=h).status_code == 201  # no CSRF: a key
    assert c.get("/v1/audit", headers=h).json()[0]["actor"] == "ASSAY_ADMIN_KEY"


def test_half_set_up_sso_fails_at_start(tmp_path, idp):
    with pytest.raises(sso.SSOError, match="ASSAY_SESSION_SECRET must be at least 32"):
        create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", oidc_issuer=idp.url, oidc_client_id="assay",
                            session_secret="short", public_url="http://x"))
    with pytest.raises(sso.SSOError, match="ASSAY_OIDC_CLIENT_ID"):
        create_app(Settings(store_url=f"sqlite:///{tmp_path / 's.db'}", oidc_issuer=idp.url, session_secret=SECRET))


def test_signed_values():
    t = sso.sign(SECRET, {"a": 1, "exp": time.time() + 60})
    assert sso.unsign(SECRET, t)["a"] == 1
    assert sso.unsign("x" * 40, t) is None and sso.unsign(SECRET, t[:-2] + "xx") is None
    assert sso.unsign(SECRET, sso.sign(SECRET, {"exp": time.time() - 1})) is None
