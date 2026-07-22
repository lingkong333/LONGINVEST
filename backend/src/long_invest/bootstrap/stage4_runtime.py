from __future__ import annotations

import hashlib
import json
import socket
from decimal import Decimal
from functools import lru_cache
from typing import Any

import docker

from long_invest.bootstrap.strategy_data import QfqStrategyDataPort
from long_invest.bootstrap.strategy_validation import (
    StrategyValidationEvidenceVerifier,
    StrategyValidationExecutor,
)
from long_invest.modules.backtests.application import BacktestApplication
from long_invest.modules.backtests.contracts import (
    BacktestCreateRequest,
    BacktestMode,
    BacktestStrategyExecution,
    BacktestTaskSnapshot,
    BacktestUniverseEntry,
)
from long_invest.modules.backtests.engine import FixedTargetBacktestEngine
from long_invest.modules.backtests.outbox import BacktestOutboxAdapter
from long_invest.modules.backtests.signal_rule import BacktestProductionSignalRule
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
from long_invest.platform.json_snapshot import thaw_json_value


class BacktestSnapshotResolver:
    def __init__(self, *, securities: Any, strategies: Any) -> None:
        self._securities = securities
        self._strategies = strategies

    async def resolve_creation_snapshot(
        self, *, task_id, request: BacktestCreateRequest
    ) -> BacktestTaskSnapshot:
        security = await self._securities.get(request.symbol)
        entry = BacktestUniverseEntry(
            security_id=security.id,
            symbol=security.symbol,
            name=security.name,
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
            mode=BacktestMode.SINGLE,
            universe_snapshot=(entry,),
            universe_hash=_hash_json(entry.model_dump(mode="json")),
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
            price_basis="QFQ_AS_OF",
            data_source="EASTMONEY",
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


class UnavailableAdjustmentTimeline:
    async def get_adjustment_timeline(self, **_: Any):
        raise _error(
            "ADJUSTMENT_DATA_UNAVAILABLE",
            "point-in-time corporate action data is unavailable",
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

    database = get_database()
    strategies = get_strategy_application()
    data = QfqStrategyDataPort(get_qfq_application())
    rule = BacktestProductionSignalRule(ProductionPriceZoneRule())
    return BacktestApplication(
        database,
        creation_snapshots=BacktestSnapshotResolver(
            securities=get_security_application(), strategies=strategies
        ),
        strategy_executions=BacktestStrategyResolver(strategies),
        training_data=data,
        test_data=data,
        forecasts=LazyStrategyForecastService(),
        adjustments=UnavailableAdjustmentTimeline(),
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


def _hash_json(value: Any) -> str:
    payload = json.dumps(
        thaw_json_value(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _error(code: str, message: str, status_code: int = 409) -> AppError:
    return AppError(code=code, message=message, status_code=status_code)
