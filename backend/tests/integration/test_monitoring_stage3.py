from long_invest.bootstrap.app import create_app

ROUTES = (
    ("/api/v1/watchlists", "get"),
    ("/api/v1/watchlists", "post"),
    ("/api/v1/watchlists/{watchlist_id}", "get"),
    ("/api/v1/watchlists/{watchlist_id}", "patch"),
    ("/api/v1/watchlists/{watchlist_id}", "delete"),
    ("/api/v1/watchlists/{watchlist_id}/items", "post"),
    ("/api/v1/watchlists/{watchlist_id}/items/batch", "post"),
    ("/api/v1/watchlists/{watchlist_id}/items/{symbol}", "delete"),
    ("/api/v1/monitor-schedules", "get"),
    ("/api/v1/monitor-schedules", "post"),
    ("/api/v1/monitor-schedules/{schedule_id}", "get"),
    ("/api/v1/monitor-schedules/{schedule_id}", "patch"),
    ("/api/v1/monitor-schedules/{schedule_id}/archive", "post"),
    ("/api/v1/monitor-schedules/{schedule_id}/versions", "get"),
    (
        "/api/v1/monitor-schedules/{schedule_id}/versions/{revision_id}/restore",
        "post",
    ),
    ("/api/v1/positions", "get"),
    ("/api/v1/position-history", "get"),
    ("/api/v1/positions/batch", "post"),
    ("/api/v1/positions/{symbol}", "get"),
    ("/api/v1/positions/{symbol}/history", "get"),
    ("/api/v1/positions/{symbol}/hold", "post"),
    ("/api/v1/positions/{symbol}/clear", "post"),
    ("/api/v1/monitor-subscriptions", "get"),
    ("/api/v1/monitor-subscriptions", "post"),
    ("/api/v1/monitor-subscriptions/{subscription_id}", "get"),
    ("/api/v1/monitor-subscriptions/{subscription_id}", "patch"),
    ("/api/v1/monitor-subscriptions/{subscription_id}/enable", "post"),
    ("/api/v1/monitor-subscriptions/{subscription_id}/disable", "post"),
    ("/api/v1/monitor-subscriptions/{subscription_id}/archive", "post"),
    ("/api/v1/monitor-subscriptions/{subscription_id}/restore", "post"),
    ("/api/v1/monitor-subscriptions/{subscription_id}/check-now", "post"),
    ("/api/v1/monitor-subscriptions/{subscription_id}/diagnose", "post"),
)


def test_monitoring_foundation_routes_are_registered_with_concrete_responses() -> None:
    paths = create_app().openapi()["paths"]

    for path, method in ROUTES:
        operation = paths[path][method]
        success = next(
            response
            for status, response in operation["responses"].items()
            if status.startswith("2")
        )
        schema = success["content"]["application/json"]["schema"]
        assert "$ref" in schema, f"{method.upper()} {path} has no response model"


def test_monitoring_writes_require_idempotency_key() -> None:
    paths = create_app().openapi()["paths"]

    for path, method in ROUTES:
        if method == "get":
            continue
        header = next(
            parameter
            for parameter in paths[path][method]["parameters"]
            if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
        )
        assert header["required"] is True, f"{method.upper()} {path} header optional"


def test_monitoring_operation_ids_are_unique() -> None:
    schema = create_app().openapi()
    operation_ids = [
        operation["operationId"]
        for path in schema["paths"].values()
        for method, operation in path.items()
        if method in {"get", "post", "put", "patch", "delete"}
    ]

    assert len(operation_ids) == len(set(operation_ids))
