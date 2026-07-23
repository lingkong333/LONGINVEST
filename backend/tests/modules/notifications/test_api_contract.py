import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from long_invest.bootstrap.app import create_app
from long_invest.modules.notifications.api import (
    TemplateTypePreviewRequest,
    get_channel,
    preview_template_type,
)
from long_invest.modules.notifications.contracts import DeliveryChannel
from long_invest.modules.notifications.delivery import CircuitSnapshot


def test_notification_and_dynamic_setting_routes_are_published() -> None:
    paths = create_app().openapi()["paths"]

    expected = {
        "/api/v1/settings",
        "/api/v1/settings/{key}",
        "/api/v1/settings/{key}/history",
        "/api/v1/settings/{key}/rollback",
        "/api/v1/secrets/status",
        "/api/v1/secrets/{key}",
        "/api/v1/notifications/events",
        "/api/v1/notifications/events/{event_id}",
        "/api/v1/notifications/deliveries",
        "/api/v1/notifications/deliveries/{delivery_id}/attempts",
        "/api/v1/notifications/deliveries/{delivery_id}/retry",
        "/api/v1/notifications/deliveries/{delivery_id}/cancel",
        "/api/v1/notifications/deliveries/retry-batch",
        "/api/v1/notifications/policies/{scope}",
        "/api/v1/notifications/channels",
        "/api/v1/notifications/channels/{channel}",
        "/api/v1/notifications/channels/{channel}/test",
        "/api/v1/notifications/templates",
        "/api/v1/notifications/templates/preview",
        "/api/v1/notification-events",
        "/api/v1/notification-events/{id}",
        "/api/v1/notification-deliveries",
        "/api/v1/notification-deliveries/{id}/attempts",
        "/api/v1/notification-deliveries/{id}/retry",
        "/api/v1/notification-deliveries/{id}/cancel",
        "/api/v1/notification-deliveries/retry-batch",
        "/api/v1/notification-policies/{scope}",
        "/api/v1/notification-channels/{channel}",
        "/api/v1/notification-channels/{channel}/test",
        "/api/v1/notification-channels/{channel}/probe",
        "/api/v1/notification-channels/{channel}/reset-circuit",
        "/api/v1/notification-templates",
        "/api/v1/notification-templates/{type}/preview",
        "/api/v1/notification-templates/{type}/activate",
    }
    assert expected <= paths.keys()


def test_all_notification_writes_require_idempotency_header() -> None:
    paths = create_app().openapi()["paths"]
    writes = [
        ("/api/v1/notifications/deliveries/{delivery_id}/retry", "post"),
        ("/api/v1/notifications/deliveries/{delivery_id}/cancel", "post"),
        ("/api/v1/notifications/deliveries/retry-batch", "post"),
        ("/api/v1/notifications/policies/{scope}", "patch"),
        ("/api/v1/notifications/channels/{channel}", "patch"),
        ("/api/v1/notifications/channels/{channel}/test", "post"),
        ("/api/v1/notification-deliveries/{id}/retry", "post"),
        ("/api/v1/notification-deliveries/{id}/cancel", "post"),
        ("/api/v1/notification-deliveries/retry-batch", "post"),
        ("/api/v1/notification-policies/{scope}", "patch"),
        ("/api/v1/notification-channels/{channel}", "patch"),
        ("/api/v1/notification-channels/{channel}/test", "post"),
        ("/api/v1/notification-channels/{channel}/probe", "post"),
        ("/api/v1/notification-channels/{channel}/reset-circuit", "post"),
        ("/api/v1/notification-templates/{type}/activate", "post"),
    ]
    for path, method in writes:
        parameters = paths[path][method]["parameters"]
        assert any(
            item["in"] == "header" and item["name"] == "Idempotency-Key"
            for item in parameters
        )


def test_single_delivery_retry_exposes_duplicate_risk_confirmation() -> None:
    document = create_app().openapi()
    operation = document["paths"][
        "/api/v1/notifications/deliveries/{delivery_id}/retry"
    ]["post"]
    reference = operation["requestBody"]["content"]["application/json"]["schema"][
        "$ref"
    ]
    schema = document["components"]["schemas"][reference.rsplit("/", 1)[-1]]

    assert "confirm_duplicate_risk" in schema["properties"]
    assert schema["properties"]["confirm_duplicate_risk"]["default"] is False


def test_spec_channel_read_returns_only_the_matching_secret_status() -> None:
    settings = SimpleNamespace(
        read=AsyncMock(
            side_effect=[
                {
                    "key": "notification.channel.wecom",
                    "value": {"enabled": True, "timeout_seconds": 5},
                    "version": 2,
                },
                [
                    {
                        "key": "notification.email.password",
                        "configured": True,
                    },
                    {
                        "key": "notification.wecom.webhook",
                        "configured": False,
                    },
                ],
            ]
        )
    )

    application = SimpleNamespace(
        channel_circuit_snapshots=AsyncMock(
            return_value={DeliveryChannel.WECOM: CircuitSnapshot.closed()}
        )
    )
    result = asyncio.run(
        get_channel(
            DeliveryChannel.WECOM,
            settings,
            application,
            SimpleNamespace(),
        )
    )

    assert result["data"] == {
        "channel": DeliveryChannel.WECOM,
        "setting": {
            "key": "notification.channel.wecom",
            "value": {"enabled": True, "timeout_seconds": 5},
            "version": 2,
            "allowed_actions": ["UPDATE"],
        },
        "secret": {
            "key": "notification.wecom.webhook",
            "configured": False,
        },
        "circuit": {
            "channel": DeliveryChannel.WECOM,
            "state": "CLOSED",
            "consecutive_failures": 0,
            "cooldown_level": 0,
            "retry_at": None,
        },
    }
    assert settings.read.await_args_list[0].args == (
        "get_setting",
        "notification.channel.wecom",
    )


def test_spec_template_preview_uses_type_from_the_path() -> None:
    result = asyncio.run(
        preview_template_type(
            "notification.test",
            TemplateTypePreviewRequest(
                version="v1",
                variables={
                    "message": "connectivity check",
                    "event_id": "event-1",
                },
                test_message=True,
            ),
            SimpleNamespace(),
        )
    )

    assert result["data"]["template_type"] == "notification.test"
    assert result["data"]["text"].startswith("[TEST MESSAGE]")
