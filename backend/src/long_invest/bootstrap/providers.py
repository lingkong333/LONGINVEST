import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.providers.contracts import ProviderCode
from long_invest.modules.providers.eastmoney import EastmoneyProvider
from long_invest.modules.providers.http_client import (
    ProviderHttpClient,
    create_async_client,
)
from long_invest.modules.providers.repository import ProviderRepository
from long_invest.modules.providers.resilience import RedisProviderRuntimeState
from long_invest.modules.providers.router import ProviderRouter
from long_invest.modules.providers.service import ProviderService
from long_invest.modules.providers.sina import SinaRealtimeProvider
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import get_database
from long_invest.platform.outbox.service import TransactionalOutboxWriter


class ProviderAuditAdapter:
    def __init__(self, session: AsyncSession) -> None:
        self._audit = AuditService(session)

    async def record(
        self,
        *,
        context: AuditContext,
        action_code: str,
        object_type: str,
        object_id: str,
        reason: str,
        before_summary: dict | None,
        after_summary: dict | None,
    ) -> None:
        digest = hashlib.sha256(context.idempotency_key.encode()).hexdigest()
        await self._audit.append(
            AuditWrite(
                action_code=action_code,
                object_type=object_type,
                object_id=object_id,
                result="SUCCESS",
                request_id=context.request_id,
                idempotency_key=f"providers:{action_code}:{digest}",
                risk_level="HIGH",
                reason=reason,
                before_summary=before_summary,
                after_summary=after_summary,
                actor_user_id=context.actor_user_id,
                session_id=context.session_id,
                trusted_ip=context.trusted_ip,
            )
        )


class ProviderEventAdapter:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._writer = TransactionalOutboxWriter()

    async def append(
        self,
        event_type: str,
        payload: dict,
        *,
        idempotency_key: str,
    ) -> None:
        aggregate_id = str(
            payload.get("circuit_id")
            or payload.get("provider_code")
            or "providers"
        )
        digest = hashlib.sha256(idempotency_key.encode()).hexdigest()
        await self._writer.append(
            session=self._session,
            topic=event_type,
            aggregate_type="provider",
            aggregate_id=aggregate_id,
            queue="domain-events",
            payload={"event_type": event_type, **payload},
            dedupe_key=f"providers:{event_type}:{digest}",
        )


@dataclass(slots=True)
class ProviderResources:
    http_client: httpx.AsyncClient
    redis: Redis
    runtime: RedisProviderRuntimeState
    providers: dict[ProviderCode, object]

    async def close(self) -> None:
        await self.http_client.aclose()
        await self.redis.aclose()


_resources: ProviderResources | None = None


def get_provider_resources() -> ProviderResources:
    global _resources
    if _resources is None:
        client = create_async_client()
        provider_http = ProviderHttpClient(
            client,
            allowed_hosts=frozenset(
                {
                    "push2.eastmoney.com",
                    "push2his.eastmoney.com",
                    "datacenter-web.eastmoney.com",
                    "np-anotice-stock.eastmoney.com",
                    "np-cnotice-stock.eastmoney.com",
                    "hq.sinajs.cn",
                }
            ),
        )
        redis = Redis.from_url(get_settings().redis_url)
        _resources = ProviderResources(
            http_client=client,
            redis=redis,
            runtime=RedisProviderRuntimeState(redis),
            providers={
                ProviderCode.EASTMONEY: EastmoneyProvider(provider_http),
                ProviderCode.SINA: SinaRealtimeProvider(provider_http),
            },
        )
    return _resources


async def close_provider_resources() -> None:
    global _resources
    if _resources is not None:
        await _resources.close()
        _resources = None


async def provide_provider_service() -> AsyncIterator[ProviderService]:
    resources = get_provider_resources()
    async with get_database().session() as session:
        yield build_provider_service(session, resources=resources)


def build_provider_service(
    session: AsyncSession,
    *,
    resources: ProviderResources | None = None,
) -> ProviderService:
    active = resources or get_provider_resources()
    repository = ProviderRepository(
        session,
        audit=ProviderAuditAdapter(session),
        events=ProviderEventAdapter(session),
    )
    provider_router = ProviderRouter(
        active.providers[ProviderCode.EASTMONEY],
        active.providers[ProviderCode.SINA],
        config=repository,
        runtime=active.runtime,
        observer=repository,
    )
    return ProviderService(
        provider_router,
        active.providers,
        repository,
        active.runtime,
    )
