"""A login instead of a stored password (assay/credentials.py), and `assay connect db` with one."""
import sqlite3
import sys

import pytest
from sqlalchemy import event, text

from assay import credentials
from assay.__main__ import main
from assay.credentials import CredentialError, PasswordCommand


@pytest.fixture
def cli(tmp_path):
    """A fake cloud CLI: prints a token and counts how often it was run."""
    count = tmp_path / "count"
    script = tmp_path / "token.py"
    script.write_text(f"import pathlib\np = pathlib.Path({str(count)!r})\nn = int(p.read_text()) + 1 if p.exists() else 1\n"
                      "p.write_text(str(n))\nprint(f's3cr3t-token-{n}')\n", encoding="utf-8")
    return f"{sys.executable} {script}", count


def test_the_token_is_fetched_when_needed_and_again_once_it_expires(cli):
    cmd, count = cli
    pc = PasswordCommand(cmd, ttl=3600)
    assert pc.token() == pc.token() == "s3cr3t-token-1" and count.read_text(encoding="utf-8") == "1"  # kept while fresh
    pc.ttl = 0
    assert pc.token() == "s3cr3t-token-2"  # expired: asked again


def test_a_login_that_fails_says_what_to_do():
    with pytest.raises(CredentialError, match=r"failed \(3\): token expired.*aws sso login"):
        PasswordCommand(f"{sys.executable} -c \"import sys; sys.stderr.write('token expired'); sys.exit(3)\"").token()
    with pytest.raises(CredentialError, match="printed nothing"):
        PasswordCommand(f"{sys.executable} -c pass").token()


def test_the_token_is_the_new_connections_password(cli):
    cmd, _ = cli
    pc, params = PasswordCommand(cmd), {"host": "db", "user": "me"}
    credentials.inject("postgresql", params, pc)
    assert params == {"host": "db", "user": "me", "password": "s3cr3t-token-1"}
    lite = {}
    credentials.inject("sqlite", lite, pc)
    assert lite == {}  # SQLite takes no password; the login still ran


def test_every_new_connection_gets_the_token_as_its_password(cli):
    pytest.importorskip("psycopg")
    cmd, _ = cli
    eng = credentials.engine("postgresql+psycopg://me@db.example.internal:5432/pipeline", cmd)
    seen = []

    class Stop(Exception):
        pass

    @event.listens_for(eng, "do_connect")  # after Assay's: what the driver is handed, then no network
    def spy(dialect, rec, cargs, cparams):
        seen.append(cparams.get("password"))
        raise Stop
    for _ in range(2):
        with pytest.raises(Exception):
            eng.connect()
    assert seen == ["s3cr3t-token-1", "s3cr3t-token-1"]  # the token, and the same one while it's fresh


def test_presets_build_the_command_from_the_url_and_the_clis_here():
    url = "postgresql+psycopg://me@prod.abc123.eu-west-1.rds.amazonaws.com:5432/pipeline"
    got = credentials.preset("aws-rds", url, profile="data-ro", have={"aws": True})
    assert got["command"] == ("aws rds generate-db-auth-token --hostname prod.abc123.eu-west-1.rds.amazonaws.com "
                              "--port 5432 --username me --region eu-west-1 --profile data-ro")
    assert got["url"].endswith("?sslmode=require")  # token logins need TLS
    okta = credentials.preset("okta", url, profile="data-ro", have={"saml2aws": True})
    assert okta["command"].startswith("saml2aws exec --exec-profile data-ro -- aws rds generate-db-auth-token")
    assert "--profile" not in okta["command"].split(" -- ")[1]  # saml2aws hands it the keys
    assert credentials.preset("okta", url, have={"aws-okta": True})["command"].startswith("aws-okta exec default -- aws rds")
    assert "--profile data-ro" in credentials.preset("okta", url, profile="data-ro", have={})["command"]  # gimme-aws-creds
    az = credentials.preset("azure", "postgresql://me%40acme.com@pg.postgres.database.azure.com/db", have={"az": True})
    assert az["command"] == "az account get-access-token --resource-type oss-rdbms --query accessToken -o tsv"
    assert credentials.preset("gcloud", "postgresql://me@10.0.0.3/db", have={})["command"] == "gcloud sql generate-login-token"
    sf = credentials.preset("snowflake-sso", "snowflake://me@acme/db/public?warehouse=wh", have={})
    assert sf["command"] is None and "authenticator=externalbrowser" in sf["url"] and "warehouse=wh" in sf["url"]
    with pytest.raises(CredentialError, match="database user"):
        credentials.preset("aws-rds", "postgresql://prod.rds.amazonaws.com/db", have={"aws": True})


def test_the_preset_is_suggested_from_the_host_and_the_clis_here():
    rds = "postgresql://me@db.x.us-east-1.rds.amazonaws.com/p"
    assert credentials.suggest(rds, {"aws": True}) == "aws-rds"
    assert credentials.suggest(rds, {"aws": True, "saml2aws": True}) == "okta"
    assert credentials.suggest(rds, {}) is None  # no CLI to get a token with
    assert credentials.suggest("postgresql://me@pg.postgres.database.azure.com/p", {"az": True}) == "azure"
    assert credentials.suggest("snowflake://me@acme/db", {}) == "snowflake-sso"
    assert credentials.suggest("postgresql://me@localhost/p", {"aws": True}) is None


def test_connect_db_with_a_login_never_shows_the_token(cli, tmp_path, monkeypatch, capsys):
    cmd, count = cli
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NO_COLOR", "1")
    for k in ("DATABASE_URL", "ASSAY_SOURCE_URL", "ASSAY_SOURCE_PASSWORD_COMMAND"):
        monkeypatch.delenv(k, raising=False)
    db = tmp_path / "p.db"
    c = sqlite3.connect(db)
    c.executescript("create table documents (id text, created_at timestamp);"
                    "create table stage_runs (document_id text, stage text, started_at timestamp);")
    c.commit()
    assert main(["connect", "db", f"sqlite:///{db}", "--password-command", cmd]) == 0
    out = capsys.readouterr().out
    assert f"Getting a short-lived password with: {cmd}" in out and "export ASSAY_SOURCE_PASSWORD_COMMAND=" in out
    assert "s3cr3t" not in out and "s3cr3t" not in (tmp_path / "mappings" / "p.json").read_text(encoding="utf-8")
    assert int(count.read_text(encoding="utf-8")) >= 1  # the login was used
    monkeypatch.setenv("ASSAY_SOURCE_PASSWORD_COMMAND", f"{sys.executable} -c \"import sys; sys.exit(1)\"")
    assert main(["connect", "db", f"sqlite:///{db}", "--out", "other.json"]) == 2
    assert "Are you logged in" in capsys.readouterr().err
