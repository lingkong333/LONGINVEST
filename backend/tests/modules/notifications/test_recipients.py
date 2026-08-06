import base64
import os
from uuid import uuid4

import pytest

from long_invest.modules.notifications.recipients import (
    RecipientInput,
    RecipientType,
    _validate,
)
from long_invest.modules.settings.crypto import SecretCipher
from long_invest.platform.errors import AppError


def cipher() -> SecretCipher:
    key = base64.urlsafe_b64encode(os.urandom(32)).decode()
    return SecretCipher(key)


def test_robot_webhook_is_encrypted_and_never_stored_in_config() -> None:
    recipient_id = uuid4()
    webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=test-key"
    secret_cipher = cipher()

    result = _validate(
        RecipientInput(
            name="投资提醒群",
            recipient_type=RecipientType.WECOM_ROBOT,
            destination="",
            config={},
            secret=webhook,
        ),
        secret_cipher,
        recipient_id,
        secret_required=True,
    )

    assert webhook.encode() not in result["secret_ciphertext"]
    assert result["config"] == {}
    assert result["destination"] == "企业微信群机器人"
    assert (
        secret_cipher.decrypt(
            f"notification-recipient:{recipient_id}", result["secret_ciphertext"]
        )
        == webhook
    )


def test_email_rejects_invalid_address() -> None:
    with pytest.raises(AppError) as raised:
        _validate(
            RecipientInput(
                name="错误邮箱",
                recipient_type=RecipientType.EMAIL,
                destination="not-an-email",
                config={},
                secret=None,
            ),
            cipher(),
            uuid4(),
            secret_required=True,
        )

    assert raised.value.code == "NOTIFICATION_RECIPIENT_INVALID"


def test_existing_enterprise_user_can_keep_its_secret_when_edited() -> None:
    result = _validate(
        RecipientInput(
            name="高坤",
            recipient_type=RecipientType.WECOM_USER,
            destination="gaokun",
            config={"corp_id": "corp", "agent_id": "100001"},
            secret=None,
        ),
        cipher(),
        uuid4(),
        secret_required=False,
    )

    assert result["secret_ciphertext"] is None
    assert result["destination"] == "gaokun"
