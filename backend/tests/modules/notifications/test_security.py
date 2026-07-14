import importlib
import importlib.util

import pytest


def load_security():
    module_name = "long_invest.modules.notifications.security"
    assert importlib.util.find_spec(module_name) is not None, (
        "notification security serialization is not implemented"
    )
    return importlib.import_module(module_name)


def test_event_and_queue_payloads_reject_secret_fields() -> None:
    security = load_security()

    with pytest.raises(security.UnsafeNotificationPayloadError) as exc_info:
        security.validate_notification_payload(
            {"event_id": "evt-1", "smtp_password": "plaintext"}
        )

    assert exc_info.value.code == "NOTIFICATION_SECRET_VALUE_REJECTED"


def test_event_and_queue_payloads_reject_secret_reference_objects() -> None:
    security = load_security()

    with pytest.raises(security.UnsafeNotificationPayloadError):
        security.validate_notification_payload(
            {
                "event_id": "evt-1",
                "config": security.SecretReferenceValue(
                    "secret://notifications/email/password"
                ),
            }
        )


def test_safe_payload_is_json_compatible_and_copied() -> None:
    security = load_security()
    source = {"symbol": "600000.SH", "targets": ["8.50", "9.20"]}

    safe = security.validate_notification_payload(source)
    source["targets"].append("12.80")

    assert safe == {"symbol": "600000.SH", "targets": ["8.50", "9.20"]}


def test_safe_payload_rejects_exceptions_and_non_finite_numbers() -> None:
    security = load_security()

    with pytest.raises(security.UnsafeNotificationPayloadError):
        security.validate_notification_payload({"error": RuntimeError("boom")})
    with pytest.raises(security.UnsafeNotificationPayloadError):
        security.validate_notification_payload({"duration": float("nan")})


def test_models_reject_sensitive_event_and_attempt_content_before_persistence() -> None:
    models_module = "long_invest.modules.notifications.models"
    assert importlib.util.find_spec(models_module) is not None
    models = importlib.import_module(models_module)
    security = load_security()

    with pytest.raises(security.UnsafeNotificationPayloadError):
        models.NotificationEvent(
            event_type="signal.high",
            business_event_type="signal.transitioned",
            business_event_id="sig-1",
            business_object_type="subscription",
            business_object_id="sub-1",
            template_variables={"webhook_secret": "plaintext"},
            effective_channels=["WECOM"],
            template_version="v1",
            idempotency_key="signal:sig-1",
            request_id="req-1",
        )

    with pytest.raises(security.UnsafeNotificationPayloadError):
        models.NotificationDeliveryAttempt(
            attempt_no=1,
            phase="SEND",
            duration_ms=100,
            outcome="TEMPORARY_FAILURE",
            possibly_delivered=False,
            request_id="req-1",
            response_summary={"authorization": "Bearer secret"},
        )
