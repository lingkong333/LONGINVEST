import base64
import os

import pytest
from cryptography.exceptions import InvalidTag
from pydantic import ValidationError

from long_invest.modules.settings.contracts import validate_setting
from long_invest.modules.settings.crypto import SecretCipher


def test_only_allowlisted_setting_keys_are_accepted() -> None:
    with pytest.raises(KeyError):
        validate_setting("database.url", {"value": "unsafe"})


def test_channel_settings_reject_unknown_fields_and_bad_ranges() -> None:
    with pytest.raises(ValidationError):
        validate_setting(
            "notification.channel.wecom",
            {"enabled": True, "timeout_seconds": 120, "webhook": "secret"},
        )


def test_email_recipients_are_capped() -> None:
    with pytest.raises(ValidationError):
        validate_setting(
            "notification.channel.email",
            {
                "enabled": True,
                "smtp_host": "smtp.example.com",
                "smtp_port": 465,
                "security": "SSL",
                "username": "user",
                "sender": "sender@example.com",
                "recipients": [f"user-{index}@example.com" for index in range(6)],
                "timeout_seconds": 10,
            },
        )


def test_cipher_round_trip_binds_ciphertext_to_secret_name() -> None:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    cipher = SecretCipher(key)
    encrypted = cipher.encrypt("notification.email.password", "secret-value")

    assert b"secret-value" not in encrypted
    assert cipher.decrypt("notification.email.password", encrypted) == "secret-value"
    with pytest.raises(InvalidTag):
        cipher.decrypt("notification.wecom.webhook", encrypted)


def test_secret_fingerprint_is_stable_without_exposing_secret() -> None:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    cipher = SecretCipher(key)
    fingerprint = cipher.fingerprint("notification.email.password", "secret-value")
    assert fingerprint == cipher.fingerprint(
        "notification.email.password", "secret-value"
    )
    assert "secret-value" not in fingerprint
    assert len(fingerprint) == 16
    assert fingerprint != cipher.fingerprint(
        "notification.wecom.webhook", "secret-value"
    )


@pytest.mark.parametrize(
    "key", ["", "invalid", base64.urlsafe_b64encode(b"short").decode()]
)
def test_cipher_rejects_invalid_master_key(key: str) -> None:
    with pytest.raises(ValueError):
        SecretCipher(key)
