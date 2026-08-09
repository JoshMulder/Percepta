"""`verify_secure_config` refuses a fail-open boot unless ALLOW_INSECURE.

Pure settings logic, no database — but it lives here so it runs with the rest
of the platform suite. `conftest` sets `allow_insecure` for the app under test;
these cases drive `verify_secure_config` directly and restore the settings after
each via monkeypatch.
"""

import pytest

from backend.core.config import settings, verify_secure_config


def test_refuses_when_rls_would_be_bypassed(monkeypatch):
    monkeypatch.setattr(settings, "allow_insecure", False)
    monkeypatch.setattr(settings, "app_db_password", None)          # RLS bypassed
    monkeypatch.setattr(settings, "secrets_encryption_key", "a-key")
    with pytest.raises(RuntimeError, match="ROW-LEVEL SECURITY"):
        verify_secure_config()


def test_refuses_when_secrets_would_be_plaintext(monkeypatch):
    monkeypatch.setattr(settings, "allow_insecure", False)
    monkeypatch.setattr(settings, "app_db_password", "a-password")
    monkeypatch.setattr(settings, "secrets_encryption_key", None)
    with pytest.raises(RuntimeError, match="SECRETS_ENCRYPTION_KEY"):
        verify_secure_config()


def test_names_both_when_both_are_missing(monkeypatch):
    monkeypatch.setattr(settings, "allow_insecure", False)
    monkeypatch.setattr(settings, "app_db_password", None)
    monkeypatch.setattr(settings, "secrets_encryption_key", None)
    with pytest.raises(RuntimeError) as exc:
        verify_secure_config()
    message = str(exc.value)
    assert "ROW-LEVEL SECURITY" in message
    assert "SECRETS_ENCRYPTION_KEY" in message


def test_allow_insecure_permits_a_fail_open_boot(monkeypatch):
    monkeypatch.setattr(settings, "allow_insecure", True)
    monkeypatch.setattr(settings, "app_db_password", None)
    monkeypatch.setattr(settings, "secrets_encryption_key", None)
    verify_secure_config()  # must not raise


def test_fully_configured_boots_cleanly(monkeypatch):
    monkeypatch.setattr(settings, "allow_insecure", False)
    monkeypatch.setattr(settings, "app_db_password", "a-password")
    monkeypatch.setattr(settings, "secrets_encryption_key", "a-key")
    verify_secure_config()  # must not raise
