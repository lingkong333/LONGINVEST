from __future__ import annotations

import hashlib
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from typing import Any
from uuid import UUID, uuid4

import docker

from long_invest.bootstrap.strategy_data import (
    PointInTimeBacktestDataPort,
    QfqStrategyDataPort,
)
from long_invest.bootstrap.strategy_validation import (
    StrategyValidationEvidenceVerifier,
    StrategyValidationExecutor,
)
from long_invest.modules.backtests.application import BacktestApplication
from long_invest.modules.backtests.contracts import (
    BacktestCreateRequest,
    BacktestStrategyExecution,
    BacktestTaskSnapshot,
    BacktestUniverseEntry,
    BacktestUniverseSelection,
)
from long_invest.modules.backtests.engine import FixedTargetBacktestEngine
from long_invest.modules.backtests.outbox import BacktestOutboxAdapter
from long_invest.modules.backtests.signal_rule import BacktestProductionSignalRule
from long_invest.modules.backtests.universe import BacktestUniverseFreezer
from long_invest.modules.daily_data.application import get_daily_data_application
from long_invest.modules.market_data.application import (
    CorporateActionCollectionApplication,
)
from long_invest.modules.market_data.repository import CorporateActionRepository
from long_invest.modules.market_data.service import CorporateActionService
from long_invest.modules.monitoring.application import (
    transactional_monitor_subscription_port,
)
from long_invest.modules.qfq.application import get_qfq_application
from long_invest.modules.signals.rules import ProductionPriceZoneRule
from long_invest.modules.strategies.application import get_strategy_application
from long_invest.modules.strategies.contracts import StrategyForecastRequest
from long_invest.modules.strategies.forecast import (
    hash_parameter_snapshot,
    hash_source_code,
)
from long_invest.modules.strategies.forecast_service import (
    SandboxedStrategyForecastService,
)
from long_invest.modules.strategies.runner_client import DockerStrategyRunnerClient
from long_invest.modules.strategies.static_analysis import analyze_strategy_source
from long_invest.modules.targets.application import TargetApplication
from long_invest.platform.config.settings import get_settings
from long_invest.platform.database.engine import get_database
from long_invest.platform.errors import AppError


class BacktestSnapshotResolver:
    def __init__(
        self, *, securities: Any, strategies: Any, watchlists: Any | None = None
    ) -> None:
        self._securities = securities
        self._strategies = strategies
        self._watchlists = watchlists

    async def resolve_creation_snapshot(
        self,
        *,
        task_id,
        request: BacktestCreateRequest,
        actor_user_id: str | None = None,
    ) -> BacktestTaskSnapshot:
        universe = await BacktestUniverseFreezer(
            _BacktestUniverseSource(
                securities=self._securities,
                watchlists=self._watchlists,
                actor_user_id=actor_user_id,
            )
        ).freeze(
            BacktestUniverseSelection(
                mode=request.mode,
                symbol=request.symbol,
                watchlist_id=request.watchlist_id,
            )
        )
        settings = get_settings()
        if request.strategy_version_id is not None:
            version = await self._strategies.get_execution_snapshot(
                request.strategy_version_id
            )
            if version is None:
                raise _error(
                    "BACKTEST_STRATEGY_NOT_FOUND",
                    "strategy version is unavailable",
                    404,
                )
            strategy_version_id = version.id
            draft_id = None
            draft_version = None
            draft_source_code = None
            source_code_hash = version.source_code_hash
            metadata = version.metadata
            parameter_schema = version.parameter_schema
            environment_version = version.environment_version
            runner_image_digest = version.runner_image_digest
            strategy_api_version = str(version.metadata.get("api_version", "1.0"))
        else:
            draft = await self._strategies.get_draft_by_id(request.draft_id)
            if draft.draft_version != request.draft_version:
                raise _error(
                    "BACKTEST_DRAFT_VERSION_CONFLICT",
                    "strategy draft changed before the backtest was frozen",
                )
            analysis = analyze_strategy_source(draft.source_code)
            strategy_version_id = None
            draft_id = draft.id
            draft_version = draft.draft_version
            draft_source_code = draft.source_code
            source_code_hash = hash_source_code(draft.source_code)
            metadata = analysis.metadata
            parameter_schema = analysis.parameter_schema
            environment_version = settings.strategy_environment_version
            runner_image_digest = settings.strategy_runner_image_digest
            strategy_api_version = analysis.api_version
        if not runner_image_digest:
            raise _error(
                "STRATEGY_RUNNER_IMAGE_NOT_CONFIGURED",
                "strategy runner image digest is not configured",
                503,
            )
        return BacktestTaskSnapshot(
            id=task_id,
            mode=universe.mode,
            universe_snapshot=universe.entries,
            universe_hash=universe.content_hash,
            survivor_bias_disclosed=universe.survivor_bias_disclosed,
            date_range=request.date_range,
            strategy_version_id=strategy_version_id,
            draft_id=draft_id,
            draft_version=draft_version,
            draft_source_code=draft_source_code,
            source_code_hash=source_code_hash,
            strategy_metadata=metadata,
            parameter_schema=parameter_schema,
            parameter_snapshot=request.parameter_snapshot,
            parameter_hash=hash_parameter_snapshot(request.parameter_snapshot),
            environment_version=environment_version,
            runner_image_digest=runner_image_digest,
            strategy_api_version=strategy_api_version,
            rule_version="signals-price-zone-v1",
            hysteresis_ratio=Decimal("0.02"),
            minimum_hysteresis=Decimal("0.02"),
            initial_capital=request.initial_capital,
            price_basis=PointInTimeBacktestDataPort.price_basis,
            data_source="EASTMONEY",
        )


class _BacktestUniverseSource:
    def __init__(
        self, *, securities: Any, watchlists: Any, actor_user_id: str | None
    ) -> None:
        self._securities = securities
        self._watchlists = watchlists
        self._actor_user_id = actor_user_id

    async def get_single(self, symbol: str) -> BacktestUniverseEntry:
        return _backtest_entry(await self._securities.get(symbol))

    async def list_watchlist(
        self, watchlist_id: UUID
    ) -> tuple[BacktestUniverseEntry, ...]:
        if self._actor_user_id is None:
            raise _error(
                "BACKTEST_WATCHLIST_OWNER_REQUIRED",
                "watchlist backtest requires an authenticated owner",
                403,
            )
        if self._watchlists is None:
            raise _error(
                "BACKTEST_WATCHLIST_UNAVAILABLE",
                "watchlist service is unavailable",
                503,
            )
        watchlist = await self._watchlists.get(
            watchlist_id, owner_user_id=UUID(self._actor_user_id)
        )
        if watchlist.archived:
            raise _error(
                "BACKTEST_WATCHLIST_ARCHIVED",
                "archived watchlist cannot start a new backtest",
            )
        entries = []
        for item in watchlist.items:
            entries.append(_backtest_entry(await self._securities.get(item.symbol)))
        return tuple(entries)

    async def list_market(self) -> tuple[BacktestUniverseEntry, ...]:
        entries: list[BacktestUniverseEntry] = []
        page = 1
        while True:
            rows, total = await self._securities.list(page=page, page_size=200)
            entries.extend(
                _backtest_entry(row) for row in rows if _is_backtest_security(row)
            )
            if page * 200 >= total:
                break
            page += 1
        return tuple(entries)


def _backtest_entry(security: Any) -> BacktestUniverseEntry:
    return BacktestUniverseEntry(
        security_id=security.id,
        symbol=security.symbol,
        name=security.name,
    )


def _is_backtest_security(security: Any) -> bool:
    return (
        str(security.market) in {"SH", "SZ", "BJ"}
        and str(security.security_type) == "A_SHARE"
        and str(security.listing_status) != "DATA_MISSING"
    )


class BacktestStrategyResolver:
    def __init__(self, strategies: Any) -> None:
        self._strategies = strategies

    async def resolve_execution(
        self, task: BacktestTaskSnapshot
    ) -> BacktestStrategyExecution:
        if task.strategy_version_id is not None:
            version = await self._strategies.get_execution_snapshot(
                task.strategy_version_id
            )
            if version is None or version.source_code_hash != task.source_code_hash:
                raise _error(
                    "BACKTEST_STRATEGY_CHANGED",
                    "frozen strategy version is unavailable",
                )
            return BacktestStrategyExecution(
                strategy_id=version.strategy_id,
                source_code=version.source_code,
            )
        if task.draft_id is None or task.draft_source_code is None:
            raise _error("BACKTEST_STRATEGY_INVALID", "frozen draft is incomplete")
        draft = await self._strategies.get_draft_by_id(task.draft_id)
        return BacktestStrategyExecution(
            strategy_id=draft.strategy_id,
            source_code=task.draft_source_code,
        )


class PersistentAdjustmentTimeline:
    def __init__(
        self,
        *,
        database: Any | None = None,
        providers: Any | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._database = database or get_database()
        self._providers = providers or LazyCorporateActionProvider(self._database)
        self._clock = clock

    async def prepare_adjustment_timeline(
        self,
        *,
        security_id,
        symbol,
        start_date,
        end_date,
        deadline,
    ):
        collector = CorporateActionCollectionApplication(
            self._database,
            providers=self._providers,
            clock=self._clock,
        )
        await collector.collect(
            batch_id=uuid4(),
            security_id=security_id,
            symbol=symbol,
            start_date=start_date,
            end_date=end_date,
            deadline=deadline,
        )
        as_of = self._clock()
        async with self._database.session() as session:
            return await CorporateActionService(
                CorporateActionRepository(session)
            ).get_adjustment_timeline(
                security_id=security_id,
                start_date=start_date,
                end_date=end_date,
                as_of=as_of,
            )


class LazyCorporateActionProvider:
    def __init__(self, database: Any) -> None:
        self._database = database

    async def corporate_actions(self, request, deadline):
        from long_invest.bootstrap.providers import build_provider_service

        async with self._database.session() as session:
            return await build_provider_service(session).corporate_actions(
                request, deadline
            )


class LazyStrategyForecastService:
    async def forecast(self, request: StrategyForecastRequest):
        return await _forecast_service().forecast(request)


def build_target_application() -> TargetApplication:
    database = get_database()
    strategies = get_strategy_application()
    data = QfqStrategyDataPort(get_qfq_application())
    return TargetApplication(
        database,
        subscription_factory=lambda session: transactional_monitor_subscription_port(
            session,
            strategy_readiness=strategies,
            strategy_snapshots=strategies,
        ),
        strategy_application=strategies,
        training_data=data,
        forecast=LazyStrategyForecastService(),
    )


def build_backtest_application() -> BacktestApplication:
    from long_invest.modules.securities.application import get_security_application
    from long_invest.modules.watchlists.application import get_watchlist_application

    database = get_database()
    strategies = get_strategy_application()
    data = PointInTimeBacktestDataPort(
        get_qfq_application(), get_daily_data_application()
    )
    rule = BacktestProductionSignalRule(ProductionPriceZoneRule())
    return BacktestApplication(
        database,
        creation_snapshots=BacktestSnapshotResolver(
            securities=get_security_application(),
            strategies=strategies,
            watchlists=get_watchlist_application(),
        ),
        strategy_executions=BacktestStrategyResolver(strategies),
        training_data=data,
        test_data=data,
        forecasts=LazyStrategyForecastService(),
        adjustments=PersistentAdjustmentTimeline(database=database),
        engine=FixedTargetBacktestEngine(rule, rule_version=rule.rule_version),
        event_factory=BacktestOutboxAdapter,
    )


def build_strategy_validation_executor() -> StrategyValidationExecutor:
    return StrategyValidationExecutor(
        strategies=get_strategy_application(),
        backtests=build_backtest_application(),
        forecasts=LazyStrategyForecastService(),
    )


def build_strategy_validation_evidence_verifier() -> StrategyValidationEvidenceVerifier:
    return StrategyValidationEvidenceVerifier(backtests=build_backtest_application())


@lru_cache
def _forecast_service() -> SandboxedStrategyForecastService:
    settings = get_settings()
    client = docker.from_env(timeout=10)
    runner = DockerStrategyRunnerClient(
        docker_client=client,
        image=settings.strategy_runner_image_digest,
        worker_id=_worker_id(socket.gethostname()),
    )
    return SandboxedStrategyForecastService(
        runner,
        request_verifier=get_strategy_application(),
    )


def _worker_id(hostname: str) -> str:
    digest = hashlib.sha256(hostname.encode()).hexdigest()[:16]
    return f"stage4-{digest}"


def _error(code: str, message: str, status_code: int = 409) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)
