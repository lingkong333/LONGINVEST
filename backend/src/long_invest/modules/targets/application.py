import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.strategies.contracts import StrategyForecastRequest
from long_invest.modules.targets.outbox import TargetOutbox
from long_invest.modules.targets.repository import TargetRepository
from long_invest.modules.targets.service import TargetService
from long_invest.modules.targets.strategy_service import (
    CalculationResult,
    StrategyTargetService,
)
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError


class TargetApplication:
    def __init__(
        self,
        database: Database,
        *,
        subscription_factory: Callable[[Any], Any],
        repository_factory: Callable[[Any], Any] = TargetRepository,
        audit_factory: Callable[[Any], Any] = AuditService,
        event_factory: Callable[[Any], Any] = TargetOutbox,
        service_factory: Callable[..., Any] = TargetService,
        strategy_application: Any | None = None,
        training_data: Any | None = None,
        forecast: Any | None = None,
        strategy_service_factory: Callable[..., Any] = StrategyTargetService,
    ) -> None:
        self._database = database
        self._subscription_factory = subscription_factory
        self._repository_factory = repository_factory
        self._audit_factory = audit_factory
        self._event_factory = event_factory
        self._service_factory = service_factory
        self._strategy_application = strategy_application
        self._training_data = training_data
        self._forecast = forecast
        self._strategy_service_factory = strategy_service_factory

    async def set_manual(self, command):
        return await self._write("set_manual", command)

    async def restore(self, command):
        return await self._write("restore", command)

    async def calculate(self, command):
        self._require_calculation_dependencies()
        reservation = await self._strategy_write("reserve", command)
        if reservation.replayed and reservation.status in {"SUCCEEDED", "FAILED"}:
            return CalculationResult(
                "TARGET_CALCULATION_REPLAYED", reservation.run_id, replayed=True
            )
        strategy = await self._strategy_application.get_execution_snapshot(
            reservation.strategy_version_id
        )
        if strategy is None:
            return await self._strategy_write(
                "fail",
                reservation.run_id,
                code="TARGET_STRATEGY_NOT_PUBLISHED",
                summary="策略版本未发布或已失效",
            )
        training = await self._training_data.get_training_data(
            security_id=reservation.security_id,
            start_date=command.training_start_date,
            end_date=command.training_end_date,
        )
        if training is None:
            return await self._strategy_write(
                "fail",
                reservation.run_id,
                code="TARGET_TRAINING_DATA_NOT_READY",
                summary="训练日线数据尚未就绪",
            )
        await self._strategy_write(
            "mark_running", reservation.run_id, data_version=training.data_version
        )
        try:
            forecast = await self._forecast.forecast(
                StrategyForecastRequest(
                    strategy_id=strategy.strategy_id,
                    security_name=reservation.symbol,
                    strategy_version_id=strategy.id,
                    source_code=strategy.source_code,
                    source_code_hash=strategy.source_code_hash,
                    metadata=strategy.metadata,
                    parameter_schema=strategy.parameter_schema,
                    parameter_snapshot=reservation.parameter_snapshot,
                    environment_version=strategy.environment_version,
                    runner_image_digest=strategy.runner_image_digest,
                    parameter_hash=_mapping_hash(reservation.parameter_snapshot),
                    training_data=training,
                    requested_at=datetime.now(UTC),
                )
            )
        except (AppError, TimeoutError) as exc:
            return await self._strategy_write(
                "fail",
                reservation.run_id,
                code=getattr(exc, "code", "STRATEGY_FORECAST_TIMEOUT"),
                summary=str(exc),
            )
        current_training = await self._training_data.get_training_data(
            security_id=reservation.security_id,
            start_date=command.training_start_date,
            end_date=command.training_end_date,
        )
        current_version = current_training.data_version if current_training else -1
        return await self._strategy_write(
            "complete",
            reservation.run_id,
            values=forecast.values,
            target_date=command.target_date,
            source_code_hash=strategy.source_code_hash,
            current_data_version=current_version,
            resource_usage=forecast.diagnostics,
        )

    async def decide_review(self, command, *, approve: bool):
        result = await self._strategy_write("decide", command, approve=approve)
        if result.code == "TARGET_REVIEW_STALE":
            raise AppError(
                code=result.code,
                message="目标配置已经变化，请重新计算",
                status_code=409,
            )
        return result

    async def list(self, *, page: int = 1, page_size: int = 50):
        return await self._read("list", page=page, page_size=page_size)

    async def get(self, subscription_id):
        return await self._read("get", subscription_id)

    async def history(self, subscription_id, *, page: int = 1, page_size: int = 50):
        return await self._read(
            "history", subscription_id, page=page, page_size=page_size
        )

    async def _read(self, method, *args, **kwargs):
        try:
            async with self._database.session() as session:
                service = self._service_factory(
                    self._repository_factory(session),
                    subscriptions=self._subscription_factory(session),
                    audit=self._audit_factory(session),
                    events=self._event_factory(session),
                )
                return await getattr(service, method)(*args, **kwargs)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def _write(self, method, command):
        try:
            async with self._database.transaction() as session:
                service = self._service_factory(
                    self._repository_factory(session),
                    subscriptions=self._subscription_factory(session),
                    audit=self._audit_factory(session),
                    events=self._event_factory(session),
                )
                return await getattr(service, method)(command)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def _strategy_write(self, method, *args, **kwargs):
        try:
            async with self._database.transaction() as session:
                service = self._strategy_service_factory(
                    self._repository_factory(session),
                    subscriptions=self._subscription_factory(session),
                    audit=self._audit_factory(session),
                    events=self._event_factory(session),
                )
                return await getattr(service, method)(*args, **kwargs)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    def _require_calculation_dependencies(self):
        if any(
            dependency is None
            for dependency in (
                self._strategy_application,
                self._training_data,
                self._forecast,
            )
        ):
            raise AppError(
                code="TARGET_CAPABILITY_NOT_READY",
                message="目标策略计算依赖尚未接入",
                status_code=503,
            )


def _backend_unavailable() -> AppError:
    return AppError(
        code="TARGET_BACKEND_UNAVAILABLE",
        message="目标服务暂时不可用",
        status_code=503,
    )


def _mapping_hash(value) -> str:
    payload = json.dumps(
        dict(value), sort_keys=True, separators=(",", ":"), default=str
    ).encode()
    return hashlib.sha256(payload).hexdigest()


class TransactionalTargetSnapshotPort:
    """Public read port for callers already owning the database transaction."""

    def __init__(self, session, *, repository_factory=TargetRepository) -> None:
        self._repository = repository_factory(session)

    async def get_target_snapshot(self, subscription_id):
        service = TargetService(
            self._repository, subscriptions=None, audit=None, events=None
        )
        return await service.get(subscription_id)


def transactional_target_snapshot_port(
    session, **factories
) -> TransactionalTargetSnapshotPort:
    return TransactionalTargetSnapshotPort(session, **factories)
