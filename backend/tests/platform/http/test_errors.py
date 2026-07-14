import pytest
from fastapi import Query
from httpx import ASGITransport, AsyncClient

from long_invest.bootstrap.app import create_app
from long_invest.platform.errors import AppError


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_app_error_uses_standard_failure_response() -> None:
    app = create_app()

    @app.get("/_test/conflict")
    async def conflict() -> None:
        raise AppError(
            code="VERSION_CONFLICT",
            message="数据已被其他操作更新",
            status_code=409,
            details={"current_version": 2},
        )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/conflict")

    assert response.status_code == 409
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "VERSION_CONFLICT"
    assert body["message"] == "数据已被其他操作更新"
    assert body["details"] == {"current_version": 2}
    assert body["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.anyio
async def test_validation_error_uses_stable_error_code() -> None:
    app = create_app()

    @app.get("/_test/validated")
    async def validated(limit: int = Query(ge=1)) -> dict[str, int]:
        return {"limit": limit}

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/validated", params={"limit": 0})

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "VALIDATION_ERROR"
    assert body["message"] == "请求参数校验失败"
    assert "query.limit" in body["details"]["fields"]


@pytest.mark.anyio
async def test_unknown_error_hides_internal_details() -> None:
    app = create_app()

    @app.get("/_test/crash")
    async def crash() -> None:
        raise RuntimeError("secret internal detail")

    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/_test/crash")

    assert response.status_code == 500
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "INTERNAL_ERROR"
    assert body["message"] == "服务器内部错误"
    assert "secret internal detail" not in response.text
    assert body["request_id"] == response.headers["X-Request-ID"]


@pytest.mark.anyio
async def test_framework_http_error_uses_standard_failure_response() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/definitely-missing")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "RESOURCE_NOT_FOUND"
    assert body["message"] == "请求的资源不存在"
    assert body["request_id"] == response.headers["X-Request-ID"]
