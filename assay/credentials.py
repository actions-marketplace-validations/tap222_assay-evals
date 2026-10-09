"""Reach a pipeline database with the login you already have, not a stored password.

Most companies don't hand out database passwords. They hand out a login: `aws sso login`,
`gcloud auth login`, `az login`, or Okta in front of any of them. The database then accepts a
short-lived token that the cloud's CLI makes. A password command is that CLI call: Assay runs it
when it opens a connection, keeps the token in memory only, and runs it again before it expires.
Nothing is written to disk, and the token is never printed or logged.

    ASSAY_SOURCE_PASSWORD_COMMAND='aws rds generate-db-auth-token --hostname db --port 5432 --username me'
    assay connect db postgresql://me@db:5432/pipeline --preset aws-rds

Presets build the command from the database URL and the CLIs on this machine:

  aws-rds   IAM database auth: aws rds generate-db-auth-token (region from the host name)
  azure     Microsoft Entra auth: az account get-access-token --resource-type oss-rdbms
  gcloud    Cloud SQL IAM auth: gcloud sql generate-login-token (or the Cloud SQL Auth Proxy)
  okta      Okta in front of AWS: the aws-rds token through saml2aws, aws-okta or
            gimme-aws-creds (an AWS profile); Okta into Azure or Google Cloud uses those presets
  snowflake-sso   Snowflake behind Okta or any SSO: sign in in the browser, no password at all
"""
from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional
from urllib.parse import urlencode

TTL = 600  # seconds a token is used for; the clouds' last 15 minutes (RDS) to an hour (Entra)
ENV = "ASSAY_SOURCE_PASSWORD_COMMAND"


class CredentialError(RuntimeError):
    pass


class PasswordCommand:
    """Runs the command for a token, and again once `ttl` seconds have passed."""

    def __init__(self, command: str, ttl: float = TTL, timeout: float = 60):
        self.command, self.ttl, self.timeout = command, ttl, timeout
        self._token, self._at, self._lock = None, 0.0, threading.Lock()

    def token(self) -> str:
        with self._lock:
            if self._token is None or time.monotonic() - self._at >= self.ttl:  # >=: a coarse clock can read 0
                self._token, self._at = self._run(), time.monotonic()
            return self._token

    def _run(self) -> str:
        try:
            r = subprocess.run(self.command, shell=True, capture_output=True, text=True, timeout=self.timeout, encoding="utf-8", errors="replace")
        except subprocess.TimeoutExpired:
            raise CredentialError(f"The password command took longer than {self.timeout:g}s: {self.command}")
        if r.returncode != 0:
            why = (r.stderr or r.stdout).strip().splitlines()[:1]
            hint = " Are you logged in (aws sso login, az login, gcloud auth login, or your Okta tool)?"
            raise CredentialError(f"The password command failed ({r.returncode}): {why[0] if why else 'no output'}.{hint}")
        token = r.stdout.strip()
        if not token:
            raise CredentialError(f"The password command printed nothing: {self.command}")
        return token


def engine(url: str, password_command: Optional[str] = None, **kwargs):
    """A SQLAlchemy engine; with a password command, every new connection gets a fresh token."""
    from sqlalchemy import create_engine, event
    password_command = password_command or os.environ.get(ENV)
    if not password_command:
        return create_engine(url, pool_pre_ping=True, **kwargs)
    pc = PasswordCommand(password_command)
    eng = create_engine(url, pool_pre_ping=True, pool_recycle=int(TTL * 0.8), **kwargs)  # never outlive a token

    @event.listens_for(eng, "do_connect")
    def _fresh_token(dialect, conn_rec, cargs, cparams):
        inject(dialect.name, cparams, pc)
    eng._assay_password_command = pc
    return eng


def inject(dialect: str, cparams: dict, pc: PasswordCommand) -> None:
    """Give a new connection the current token as its password."""
    token = pc.token()  # run even where there's no password (SQLite), so a login that fails says so
    if dialect != "sqlite":
        cparams["password"] = token


# ---------- presets ----------

def _parts(url: str) -> dict:
    from sqlalchemy.engine import make_url
    u = make_url(url)
    return {"dialect": u.get_backend_name(), "host": u.host or "", "port": u.port, "user": u.username or "",
            "database": u.database or "", "query": dict(u.query)}


def tools() -> Dict[str, bool]:
    """The CLIs on this machine a preset could use."""
    names = ("aws", "az", "gcloud", "saml2aws", "aws-okta", "gimme-aws-creds", "okta", "cloud-sql-proxy")
    return {n: shutil.which(n) is not None for n in names}


def _aws_region(host: str) -> Optional[str]:
    m = re.search(r"\.([a-z]{2}(?:-gov)?-[a-z]+-\d)\.rds\.amazonaws\.com", host)
    return m.group(1) if m else os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION")


def _aws_rds(p: dict, profile: Optional[str]) -> str:
    port = p["port"] or (3306 if p["dialect"] == "mysql" else 5432)
    cmd = ["aws", "rds", "generate-db-auth-token", "--hostname", p["host"], "--port", str(port), "--username", p["user"]]
    region = _aws_region(p["host"])
    if region:
        cmd += ["--region", region]
    if profile:
        cmd += ["--profile", profile]
    return shlex.join(cmd)


def preset(name: str, url: str, profile: Optional[str] = None, have: Optional[Dict[str, bool]] = None) -> dict:
    """{"command": password command or None, "url": the URL to use, "notes": [...]} for a preset."""
    have = tools() if have is None else have
    p = _parts(url)
    profile = profile or os.environ.get("AWS_PROFILE")
    notes: List[str] = []
    ssl = _with_ssl(url, p)
    if name == "aws-rds":
        if not p["user"]:
            raise CredentialError("Put your database user in the URL: postgresql://you@host:5432/db")
        notes.append("the database user needs IAM auth: GRANT rds_iam TO you (Postgres), and your AWS role "
                     "rds-db:connect on it")
        return {"command": _aws_rds(p, profile), "url": ssl, "notes": notes}
    if name == "azure":
        notes.append("the user is your Microsoft Entra name (you@company.com), made a database role by an admin")
        return {"command": "az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv",
                "url": ssl, "notes": notes}
    if name == "gcloud":
        if have.get("cloud-sql-proxy"):
            notes.append("or, with no password command: run cloud-sql-proxy --auto-iam-authn INSTANCE and connect to "
                         "localhost")
        notes.append("the user is your IAM email without .gserviceaccount.com for a service account")
        return {"command": "gcloud sql generate-login-token", "url": ssl, "notes": notes}
    if name == "okta":
        aws = _aws_rds(p, None if have.get("saml2aws") or have.get("aws-okta") else profile)
        if have.get("saml2aws"):
            cmd = f"saml2aws exec {'--exec-profile ' + shlex.quote(profile) + ' ' if profile else ''}-- {aws}"
            notes.append("saml2aws signs you in through Okta (saml2aws login) and hands the AWS call its keys")
        elif have.get("aws-okta"):
            cmd = f"aws-okta exec {shlex.quote(profile or 'default')} -- {aws}"
            notes.append("aws-okta signs you in through Okta and hands the AWS call its keys")
        else:
            cmd = aws
            notes.append("uses the AWS profile Okta signs you into (gimme-aws-creds, or AWS IAM Identity Center "
                         "with aws sso login --profile …)")
        notes.append("if Okta signs you into Azure or Google Cloud instead, use the azure or gcloud preset")
        return {"command": cmd, "url": ssl, "notes": notes}
    if name == "snowflake-sso":
        q = {**p["query"], "authenticator": "externalbrowser"}
        base = url.split("?", 1)[0]
        notes.append("a browser window opens to sign you in through Okta (or your SSO); needs snowflake-sqlalchemy")
        return {"command": None, "url": f"{base}?{urlencode(q)}", "notes": notes}
    raise CredentialError(f"No preset '{name}'. Presets: {', '.join(PRESETS)}.")


PRESETS = ("aws-rds", "azure", "gcloud", "okta", "snowflake-sso")


def suggest(url: str, have: Optional[Dict[str, bool]] = None) -> Optional[str]:
    """The preset that fits this URL and the CLIs here, or None."""
    have = tools() if have is None else have
    p = _parts(url)
    host = p["host"]
    if p["dialect"] == "snowflake":
        return "snowflake-sso"
    if host.endswith(".rds.amazonaws.com") or ".rds." in host:
        return "okta" if (have.get("saml2aws") or have.get("aws-okta") or have.get("gimme-aws-creds")) else \
            "aws-rds" if have.get("aws") else None
    if host.endswith((".database.azure.com", ".postgres.database.azure.com", ".mysql.database.azure.com")):
        return "azure" if have.get("az") else None
    if "cloudsql" in host or host.endswith(".googleapis.com") or p["query"].get("host", "").startswith("/cloudsql/"):
        return "gcloud" if have.get("gcloud") else None
    return None


def _with_ssl(url: str, p: dict) -> str:
    """Token logins need TLS: add it to the URL if it isn't there."""
    if p["dialect"] == "postgresql" and "sslmode" not in p["query"]:
        return url + ("&" if "?" in url else "?") + "sslmode=require"
    return url


def redact(url: str) -> str:
    return re.sub(r"//([^:/@]+):[^@]*@", r"//\1:…@", url)
