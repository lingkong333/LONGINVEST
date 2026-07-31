import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.auth.audit import AuditContext
from long_invest.modules.market_data.contracts import OpenQualityIssue, QualitySeverity
from long_invest.modules.market_data.repository import QualityIssueRepository
from long_invest.modules.market_data.service import QualityIssueService
from long_invest.modules.providers.baostock import BaoStockProvider
from long_invest.modules.providers.browser_http_client import (
    BrowserProviderHttpClient,
    create_browser_json_client,
)
from long_invest.modules.providers.budget import ProviderRequestBudget
from long_invest.modules.providers.contracts import DailyBar, ProviderCode
from long_invest.modules.providers.eastmoney import EastmoneyProvider
from long_invest.modules.providers.http_client import (
    ProviderHttpClient,
    create_async_client,
)
from long_invest.modules.providers.playwright_http_client import (
    PlaywrightProviderHttpClient,
    create_playwright_json_client,
)
from long_invest.modules.providers.postgres_runtime import (
    PostgresProviderRuntimeState,
)
from long_invest.modules.providers.repository import ProviderRepository
from long_invest.modules.providers.router import ProviderRouter
from long_invest.modules.providers.service import ProviderService
from long_invest.modules.providers.sina import SinaRealtimeProvider
from long_invest.modules.providers.tushare import TushareProvider
from long_invest.modules.settings.application import get_settings_application
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.config.settings import AppSettings, get_settings
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
            payload.get("circuit_id") or payload.get("provider_code") or "providers"
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


class ProviderConflictQualityAdapter:
    def __init__(self, session: AsyncSession) -> None:
        self._service = QualityIssueService(QualityIssueRepository(session))

    async def record_daily_conflict(
        self, primary: DailyBar, candidate: DailyBar
    ) -> None:
        await self._service.open(
            OpenQualityIssue(
                issue_type="PROVIDER_DATA_CONFLICT",
                subject_type="daily_bar_unadjusted",
                subject_id=f"{primary.symbol}:{primary.trading_date}",
                symbol=primary.symbol,
                severity=QualitySeverity.ERROR,
                evidence={
                    "trade_date": primary.trading_date.isoformat(),
                    "sources": {
                        primary.source.value: _bar_evidence(primary),
                        candidate.source.value: _bar_evidence(candidate),
                    },
                },
                dedupe_key=(
                    f"provider-conflict:{primary.symbol}:{primary.trading_date}"
                ),
                requires_review=True,
            )
        )


@dataclass(slots=True)
class ProviderResources:
    http_client: httpx.AsyncClient
    history_http_client: BrowserProviderHttpClient | PlaywrightProviderHttpClient
    runtime: PostgresProviderRuntimeState
    providers: dict[ProviderCode, object]
    budget: ProviderRequestBudget

    async def close(self) -> None:
        await self.http_client.aclose()
        await self.history_http_client.close()


_resources: ProviderResources | None = None


def build_history_http_client(
    settings: AppSettings,
    *,
    budget: ProviderRequestBudget | None = None,
) -> BrowserProviderHttpClient | PlaywrightProviderHttpClient:
    if settings.eastmoney_history_transport == "playwright":
        return create_playwright_json_client(
            host="push2his.eastmoney.com",
            resolve_addresses=tuple(
                value.strip()
                for value in settings.eastmoney_history_resolve_ips.split(",")
                if value.strip()
            ),
            minimum_interval_seconds=(settings.eastmoney_history_min_interval_seconds),
            request_guard=budget.guard if budget else None,
        )
    return create_browser_json_client(
        host="push2his.eastmoney.com",
        resolve_addresses=(
            value.strip()
            for value in settings.eastmoney_history_resolve_ips.split(",")
            if value.strip()
        ),
        request_guard=budget.guard if budget else None,
    )


def get_provider_resources() -> ProviderResources:
    global _resources
    if _resources is None:
        client = create_async_client()
        budget = ProviderRequestBudget(get_database())
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
                    "vip.stock.finance.sina.com.cn",
                }
            ),
            request_guard=budget.guard,
        )
        settings = get_settings()
        history_http = build_history_http_client(settings, budget=budget)
        _resources = ProviderResources(
            http_client=client,
            history_http_client=history_http,
            runtime=PostgresProviderRuntimeState(get_database()),
            budget=budget,
            providers={
                ProviderCode.EASTMONEY: EastmoneyProvider(
                    provider_http,
                    history_client=history_http,
                    request_complete_history=(
                        settings.eastmoney_history_transport == "playwright"
                    ),
                ),
                ProviderCode.SINA: SinaRealtimeProvider(
                    provider_http,
                    request_guard=budget.guard,
                ),
                ProviderCode.TUSHARE: TushareProvider(
                    token_resolver=_resolve_tushare_token,
                    request_guard=budget.guard,
                ),
                ProviderCode.BAOSTOCK: BaoStockProvider(
                    request_guard=budget.guard,
                ),
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
        providers=active.providers,
        config=repository,
        runtime=active.runtime,
        observer=repository,
        conflict_observer=ProviderConflictQualityAdapter(session),
    )
    return ProviderService(
        provider_router,
        active.providers,
        repository,
        active.runtime,
        active.budget,
    )


def _bar_evidence(bar: DailyBar) -> dict[str, object]:
    return {
        "open": str(bar.open),
        "high": str(bar.high),
        "low": str(bar.low),
        "close": str(bar.close),
        "volume": bar.volume,
        "amount": str(bar.amount),
        "source_identity": (
            bar.source_identity.contract_version
            if bar.source_identity is not None
            else None
        ),
    }


async def _resolve_tushare_token() -> str | None:
    return await get_settings_application().read(
        "resolve_secret", "provider.tushare.token"
    )
