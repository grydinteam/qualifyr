"""C3: the per-user API-key encryption layer (api/keys.py). It protects users' third-party keys
at rest with Fernet, so a regression here either corrupts stored keys or leaks them in plaintext.
DB-free: resolve_api_key only needs an object with get_user_key, so a stub stands in for Postgres."""

import pytest
from cryptography.fernet import Fernet

from gtm_engine.api import keys as K


@pytest.fixture
def enc_key(monkeypatch):
    key = Fernet.generate_key().decode()
    monkeypatch.setenv("GTM_ENCRYPTION_KEY", key)
    return key


class _StubDB:
    def __init__(self, stored: dict | None = None):
        self._stored = stored or {}

    def get_user_key(self, user_id, key_name):
        return self._stored.get((user_id, key_name))


def test_encryption_available_reflects_env(monkeypatch):
    monkeypatch.delenv("GTM_ENCRYPTION_KEY", raising=False)
    assert K.encryption_available() is False
    monkeypatch.setenv("GTM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    assert K.encryption_available() is True


def test_round_trip(enc_key):
    for plaintext in ["sk-secret-123", "", "unicode-é-你好", "a" * 5000]:
        token = K.encrypt_key(plaintext)
        assert token != plaintext  # actually encrypted, not stored in the clear
        assert K.decrypt_key(token) == plaintext


def test_tokens_are_non_deterministic(enc_key):
    # Fernet embeds a random IV, so the same plaintext encrypts to different ciphertexts.
    assert K.encrypt_key("same") != K.encrypt_key("same")


def test_encrypt_without_key_raises(monkeypatch):
    monkeypatch.delenv("GTM_ENCRYPTION_KEY", raising=False)
    with pytest.raises(RuntimeError):
        K.encrypt_key("x")
    with pytest.raises(RuntimeError):
        K.decrypt_key("x")


def test_decrypt_with_rotated_key_raises_valueerror(monkeypatch):
    monkeypatch.setenv("GTM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    token = K.encrypt_key("secret")
    # Key rotated out from under the stored token: a clear ValueError, not a raw InvalidToken.
    monkeypatch.setenv("GTM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    with pytest.raises(ValueError):
        K.decrypt_key(token)


def test_resolve_prefers_user_key_over_env(enc_key, monkeypatch):
    monkeypatch.setenv("GTM_BRAVE_API_KEY", "operator-env-key")
    db = _StubDB({("user-1", "brave"): K.encrypt_key("user-own-key")})
    assert K.resolve_api_key(db, "user-1", "brave") == "user-own-key"


def test_resolve_falls_back_to_env_without_user_key(enc_key, monkeypatch):
    monkeypatch.setenv("GTM_BRAVE_API_KEY", "operator-env-key")
    db = _StubDB()
    assert K.resolve_api_key(db, "user-1", "brave") == "operator-env-key"
    # No signed-in user: env only.
    assert K.resolve_api_key(db, None, "brave") == "operator-env-key"


def test_resolve_falls_back_when_decrypt_fails(monkeypatch):
    # User key stored under a now-rotated encryption key: must not raise, falls back to env.
    monkeypatch.setenv("GTM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    stale = K.encrypt_key("stale-user-key")
    monkeypatch.setenv("GTM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("GTM_HUNTER_API_KEY", "operator-hunter")
    db = _StubDB({("user-1", "hunter"): stale})
    assert K.resolve_api_key(db, "user-1", "hunter") == "operator-hunter"


def test_resolve_all_keys_covers_allowed_set(enc_key):
    db = _StubDB()
    resolved = K.resolve_all_keys(db, None)
    assert set(resolved) == set(K.ALLOWED_KEYS)
