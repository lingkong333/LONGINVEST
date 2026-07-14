from dataclasses import dataclass
from typing import Protocol

from long_invest.platform.cache.redis import get_redis_probe
from long_invest.platform.database.engine import get_database


class DependencyProbe(Protocol):
    async def ping(self) -> bool: ...


@dataclass(frozen=True)
class ReadinessReport:
    status: str
    http_status: int
    dependencies: dict[str, str]


class ReadinessService:
    def __init__(
        self,
        *,
        database: DependencyProbe,
        redis: DependencyProbe,
    ) -> None:
        self._database = database
        self._redis = redis

    async def check(self) -> ReadinessReport:
        postgresql_status = await self._probe(self._database)
        redis_status = await self._probe(self._redis)
        dependencies = {
            "postgresql": postgresql_status,
            "redis": redis_status,
        }

        if postgresql_status != "healthy":
            return ReadinessReport(
                status="unavailable",
                http_status=503,
                dependencies=dependencies,
            )
        if redis_status != "healthy":
            return ReadinessReport(
                status="degraded",
                http_status=200,
                dependencies=dependencies,
            )
        return ReadinessReport(
            status="ready",
            http_status=200,
            dependencies=dependencies,
        )

    @staticmethod
    async def _probe(probe: DependencyProbe) -> str:
        try:
            return "healthy" if await probe.ping() else "unavailable"
        except Exception:
            return "unavailable"


def get_readiness_service() -> ReadinessService:
    return ReadinessService(
        database=get_database(),
        redis=get_redis_probe(),
    )

