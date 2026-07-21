from long_invest.bootstrap.app import create_app

EXPECTED_SIGNAL_PATHS = {
    "/api/v1/signals/states",
    "/api/v1/signals/states/{subscription_id}",
    "/api/v1/signal-events",
    "/api/v1/signal-events/{event_id}",
    "/api/v1/signal-evaluations",
    "/api/v1/signal-evaluations/{evaluation_id}",
    "/api/v1/signals/states/{subscription_id}/reset",
    "/api/v1/signals/states/{subscription_id}/reevaluate",
}


def test_main_app_exposes_concrete_target_and_signal_contracts() -> None:
    schema = create_app().openapi()
    paths = schema["paths"]

    assert set(paths) >= EXPECTED_SIGNAL_PATHS
    assert "/api/v1/targets" in paths
    assert "/api/v1/targets/{subscription_id}/manual" in paths

    operation_ids = []
    for path in EXPECTED_SIGNAL_PATHS:
        for operation in paths[path].values():
            operation_ids.append(operation["operationId"])
            success = operation["responses"]["200"]
            assert "$ref" in success["content"]["application/json"]["schema"]
    assert len(operation_ids) == len(set(operation_ids))


def test_all_signal_writes_require_idempotency_header() -> None:
    paths = create_app().openapi()["paths"]
    for path in EXPECTED_SIGNAL_PATHS:
        operation = paths[path].get("post")
        if operation is None:
            continue
        headers = {
            parameter["name"]
            for parameter in operation.get("parameters", [])
            if parameter["in"] == "header"
        }
        assert "Idempotency-Key" in headers
