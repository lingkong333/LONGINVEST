import pytest
from httpx import ASGITransport, AsyncClient

from long_invest.bootstrap.app import create_app


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


async def get_live_health(*, request_id: str | None = None):
    headers = {"X-Request-ID": request_id} if request_id else None
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get("/health/live", headers=headers)


@pytest.mark.anyio
async def test_live_health_uses_standard_response() -> None:
    response = await get_live_health()

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["code"] == "OK"
    assert body["message"] == "服务运行正常"
    assert body["data"] == {"status": "live"}
    assert body["request_id"] == response.headers["X-Request-ID"]
    assert body["server_time"].endswith("Z")


@pytest.mark.anyio
async def test_live_health_preserves_valid_request_id() -> None:
    response = await get_live_health(
        request_id="req_01J00000000000000000000000",
    )

    assert response.headers["X-Request-ID"] == "req_01J00000000000000000000000"
    assert response.json()["request_id"] == "req_01J00000000000000000000000"


@pytest.mark.anyio
async def test_live_health_replaces_invalid_request_id() -> None:
    response = await get_live_health(
        request_id="contains spaces",
    )

    assert response.status_code == 200
    assert response.headers["X-Request-ID"].startswith("req_")
    assert response.headers["X-Request-ID"] != "contains spaces"
