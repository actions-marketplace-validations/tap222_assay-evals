import json
import os
import sqlite3
from datetime import datetime
import pytest

pytest.importorskip("cryptography")

from sqlalchemy import select

from assay import crypto, integrations, store
from assay.config import Settings


def test_crypto_key_derivation():
    k1 = crypto.derive_key("my-secret-key")
    k2 = crypto.derive_key("my-secret-key")
    k3 = crypto.derive_key("different-key")
    assert k1 == k2
    assert k1 != k3
    assert len(k1) == 44  # 32 bytes base64-encoded


def test_crypto_encrypt_and_decrypt():
    key = "server-master-key-12345"
    secret = "https://hooks.slack.com/services/T00/B00/X00"
    encrypted = crypto.encrypt_secret(secret, key)
    assert encrypted.startswith(crypto.ENC_PREFIX)
    assert encrypted != secret

    # Idempotent encryption
    assert crypto.encrypt_secret(encrypted, key) == encrypted

    # Decrypt
    decrypted = crypto.decrypt_secret(encrypted, key)
    assert decrypted == secret

    # Backward compatibility: plaintext string returns as-is
    assert crypto.decrypt_secret("plain-text-token", key) == "plain-text-token"


def test_crypto_wrong_key_fails():
    encrypted = crypto.encrypt_secret("secret-value", "key-one")
    with pytest.raises(ValueError, match="Failed to decrypt"):
        crypto.decrypt_secret(encrypted, "key-two")


def test_integrations_secrets_at_rest_encrypted_in_db(tmp_path):
    db_path = tmp_path / "test_secrets.db"
    engine = store.make_engine(f"sqlite:///{db_path}")
    secret_key = "prod-server-encryption-key"

    src = "events:test"
    webhook_url = "https://hooks.slack.com/services/SECRET/TEST/123"

    # Save integration with encryption
    res = integrations.save(engine, src, "slack", {"webhook_url": webhook_url}, secret_key=secret_key)
    assert res["webhook_url"] == integrations.MASK

    # Check raw database: must be encrypted at rest, NOT plaintext!
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT config FROM integrations WHERE source=? AND kind='slack'", (src,)).fetchone()
        stored_cfg = json.loads(row[0])
        assert stored_cfg["webhook_url"].startswith(crypto.ENC_PREFIX)
        assert webhook_url not in stored_cfg["webhook_url"]

    # reveal=False returns masked
    masked = integrations.get(engine, src, "slack", reveal=False, secret_key=secret_key)
    assert masked["webhook_url"] == integrations.MASK

    # reveal=True returns decrypted plaintext
    revealed = integrations.get(engine, src, "slack", reveal=True, secret_key=secret_key)
    assert revealed["webhook_url"] == webhook_url

    # Updating with mask keeps the encrypted secret intact
    integrations.save(engine, src, "slack", {"webhook_url": integrations.MASK}, secret_key=secret_key)
    revealed2 = integrations.get(engine, src, "slack", reveal=True, secret_key=secret_key)
    assert revealed2["webhook_url"] == webhook_url


def test_integrations_plaintext_migration_to_encrypted(tmp_path):
    db_path = tmp_path / "test_mig.db"
    engine = store.make_engine(f"sqlite:///{db_path}")

    # Insert an unencrypted plaintext secret directly (simulating pre-existing database)
    plaintext_token = "lin_api_key_plaintext_123"
    with engine.begin() as conn:
        conn.execute(store.integrations.insert().values(
            source="events:old",
            kind="linear",
            config={"api_key": plaintext_token, "team_id": "T1"},
            enabled=True,
            updated_at=datetime.utcnow(),
        ))

    # Transparent read of older plaintext row
    key = "new-server-key"
    old_read = integrations.get(engine, "events:old", "linear", reveal=True, secret_key=key)
    assert old_read["api_key"] == plaintext_token

    # Re-encrypt all secrets at rest
    updated = integrations.reencrypt_all(engine, new_key=key)
    assert updated == 1

    # Raw database is now encrypted
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT config FROM integrations WHERE source='events:old' AND kind='linear'").fetchone()
        cfg = json.loads(row[0])
        assert cfg["api_key"].startswith(crypto.ENC_PREFIX)

    # Decrypt with new key works
    mig_read = integrations.get(engine, "events:old", "linear", reveal=True, secret_key=key)
    assert mig_read["api_key"] == plaintext_token
