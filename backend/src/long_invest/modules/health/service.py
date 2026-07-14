from dataclasses import dataclass
from typing import Protocol

from long_invest.platform.cache.redis import get_redis_probe
from long_invest.platform.database.engine import get_database


class DependencyProbe(Protocol):
    async def ping(self) -> bool: ...


class DatabaseProbe(DependencyProbe, Protocol):
    async def migration_is_current(self) -> bool: ...


@dataclass(frozen=True)
class ReadinessReport:
    status: str
    http_status: int
    dependencies: dict[str, str]


class ReadinessService:
    def __init__(
        self,
        *,
        database: DatabaseProbe,
        redis: DependencyProbe,
    ) -> None:
        self._database = database
        self._redis = redis

    async def check(self) -> ReadinessReport:
        postgresql_status = await self._probe(self._database)
        migration_status = await self._probe_migration(self._database)
        redis_status = await self._probe(self._redis)
        dependencies = {
            "postgresql": postgresql_status,
            "migration": migration_status,
            "redis": redis_status,
        }

        if postgresql_status != "healthy" or migration_status != "compatible":
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

    @staticmethod
    async def _probe_migration(probe: DatabaseProbe) -> str:
        try:
            is_current = await probe.migration_is_current()
            return "compatible" if is_current else "incompatible"
        except Exception:
            return "incompatible"


def get_readiness_service() -> ReadinessService:
    return ReadinessService(
        database=get_database(),
        redis=get_redis_probe(),
    )
