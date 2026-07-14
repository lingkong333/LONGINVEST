from dataclasses import dataclass

import pytest
from httpx import ASGITransport, AsyncClient

from long_invest.bootstrap.app import create_app
from long_invest.modules.health.service import (
    ReadinessService,
    get_readiness_service,
)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@dataclass
class Probe:
    error: Exception | None = None

    async def ping(self) -> bool:
        if self.error is not None:
            raise self.error
        return True


@pytest.mark.anyio
async def test_readiness_is_ready_when_dependencies_are_healthy() -> None:
    report = await ReadinessService(
        database=Probe(),
        redis=Probe(),
    ).check()

    assert report.status == "ready"
    assert report.dependencies == {
        "postgresql": "healthy",
        "redis": "healthy",
    }


@pytest.mark.anyio
async def test_readiness_is_degraded_when_only_redis_fails() -> None:
    report = await ReadinessService(
        database=Probe(),
        redis=Probe(ConnectionError("redis unavailable")),
    ).check()

    assert report.status == "degraded"
    assert report.http_status == 200
    assert report.dependencies["postgresql"] == "healthy"
    assert report.dependencies["redis"] == "unavailable"


@pytest.mark.anyio
async def test_readiness_is_unavailable_when_postgresql_fails() -> None:
    report = await ReadinessService(
        database=Probe(ConnectionError("database unavailable")),
        redis=Probe(),
    ).check()

    assert report.status == "unavailable"
    assert report.http_status == 503


@pytest.mark.anyio
async def test_ready_endpoint_checks_real_compose_dependencies() -> None:
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"] == {
        "status": "ready",
        "dependencies": {
            "postgresql": "healthy",
            "redis": "healthy",
        },
    }


@pytest.mark.anyio
async def test_ready_endpoint_returns_standard_failure_when_database_is_down() -> None:
    app = create_app()
    app.dependency_overrides[get_readiness_service] = lambda: ReadinessService(
        database=Probe(ConnectionError("database unavailable")),
        redis=Probe(),
    )
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "SERVICE_NOT_READY"
    assert body["data"] is None
    assert body["details"]["dependencies"]["postgresql"] == "unavailable"
