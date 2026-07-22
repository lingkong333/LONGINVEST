from long_invest.bootstrap.app import create_app


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
    ]
    for path, method in writes:
        parameters = paths[path][method]["parameters"]
        assert any(
            item["in"] == "header" and item["name"] == "Idempotency-Key"
            for item in parameters
        )
