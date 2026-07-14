import json
from dataclasses import dataclass
from typing import Any


class UnsafeNotificationPayloadError(ValueError):
    code = "NOTIFICATION_SECRET_VALUE_REJECTED"


@dataclass(frozen=True, slots=True)
class SecretReferenceValue:
    key: str

    def __post_init__(self) -> None:
        if not self.key.startswith("secret://") or len(self.key) <= len("secret://"):
            raise ValueError("secret reference must use a non-empty secret:// key")


_SENSITIVE_KEY_PARTS = {
    "api_key",
    "authorization",
    "cookie",
    "credential",
    "password",
    "secret",
    "smtp_password",
    "token",
    "webhook",
}


def _contains_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _reject_sensitive_values(value: Any) -> None:
    if isinstance(value, SecretReferenceValue):
        raise UnsafeNotificationPayloadError(
            "secret references cannot enter notification events, logs, or queues"
        )
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise UnsafeNotificationPayloadError("payload keys must be strings")
            if _contains_sensitive_key(key):
                raise UnsafeNotificationPayloadError(
                    f"sensitive field is not allowed in notification payload: {key}"
                )
            _reject_sensitive_values(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_sensitive_values(item)


def validate_notification_payload(value: Any) -> Any:
    _reject_sensitive_values(value)
    try:
        serialized = json.dumps(value, ensure_ascii=False, allow_nan=False)
        return json.loads(serialized)
    except (TypeError, ValueError) as exc:
        raise UnsafeNotificationPayloadError(
            "notification payload must be finite JSON data"
        ) from exc
