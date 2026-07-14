from fastapi.testclient import TestClient

from long_invest.bootstrap.app import create_app


def test_live_health_uses_standard_response() -> None:
    response = TestClient(create_app()).get("/health/live")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["code"] == "OK"
    assert body["message"] == "服务运行正常"
    assert body["data"] == {"status": "live"}
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["server_time"].endswith("Z")


def test_live_health_preserves_valid_request_id() -> None:
    response = TestClient(create_app()).get(
        "/health/live",
        headers={"X-Request-ID": "req_01J00000000000000000000000"},
    )

    assert response.headers["X-Request-ID"] == "req_01J00000000000000000000000"
    assert response.json()["request_id"] == "req_01J00000000000000000000000"


def test_live_health_replaces_invalid_request_id() -> None:
    response = TestClient(create_app()).get(
        "/health/live",
        headers={"X-Request-ID": "contains spaces"},
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("req_")
    assert response.headers["X-Request-ID"] != "contains spaces"
