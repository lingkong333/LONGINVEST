import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.strategies.contracts import StrategyForecastRequest
from long_invest.modules.targets.outbox import TargetOutbox
from long_invest.modules.targets.repository import TargetRepository
from long_invest.modules.targets.service import TargetService
from long_invest.modules.targets.strategy_service import (
    StrategyTargetService,
)
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.service import JobService


@dataclass(frozen=True, slots=True)
class CalculationSubmission:
    code: str
    run_id: Any
    job_id: Any
    replayed: bool = False


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
        job_service_factory: Callable[..., Any] = JobService,
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
        self._job_service_factory = job_service_factory

    async def set_manual(self, command):
        return await self._write("set_manual", command)

    async def restore(self, command):
        return await self._write("restore", command)

    async def calculate(self, command):
        return await self._schedule("reserve", command, calculation=command)

    async def execute(self, run_id):
        self._require_calculation_dependencies()
        try:
            plan = await self._strategy_read("execution_plan", run_id)
        except AppError as exc:
            return await self._strategy_write(
                "fail", run_id, code=exc.code, summary=exc.message
            )
        if plan.terminal_result is not None:
            return plan.terminal_result
        return await self._execute_reservation(plan, plan.reservation)

    async def apply_strategy(self, command):
        return await self._schedule(
            "apply_and_reserve", command, calculation=command.calculation
        )

    async def apply_strategy_batch(self, commands):
        results = []
        for command in commands:
            try:
                result = await self.apply_strategy(command)
            except AppError as exc:
                results.append((command.calculation.subscription_id, exc.code, None))
            else:
                results.append(
                    (command.calculation.subscription_id, result.code, result)
                )
        return tuple(results)

    async def _execute_reservation(self, command, reservation):
        if reservation.replayed and reservation.status in {"SUCCEEDED", "FAILED"}:
            return await self._strategy_write(
                "result", reservation.run_id, replayed=True
            )
        try:
            strategy = await self._strategy_application.get_execution_snapshot(
                reservation.strategy_version_id
            )
            if strategy is None:
                raise AppError(
                    code="TARGET_STRATEGY_NOT_PUBLISHED",
                    message="策略版本未发布或已失效",
                    status_code=409,
                )
            training = await self._training_data.get_training_data(
                security_id=reservation.security_id,
                start_date=command.training_start_date,
                end_date=command.training_end_date,
            )
            if training is None:
                raise AppError(
                    code="TARGET_TRAINING_DATA_NOT_READY",
                    message="训练日线数据尚未就绪",
                    status_code=409,
                )
            await self._strategy_write(
                "mark_running", reservation.run_id, data_version=training.data_version
            )
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
        except (AppError, TimeoutError, RuntimeError, ValueError) as exc:
            return await self._strategy_write(
                "fail",
                reservation.run_id,
                code=getattr(exc, "code", "TARGET_CALCULATION_FAILED"),
                summary=str(exc),
            )

    async def decide_review(self, command, *, approve: bool):
        self._require_calculation_dependencies()
        security_id, start_date, end_date = await self._strategy_read(
            "review_freshness", command.review_id
        )
        try:
            training = await self._training_data.get_training_data(
                security_id=security_id,
                start_date=start_date,
                end_date=end_date,
            )
        except (AppError, TimeoutError, RuntimeError) as exc:
            raise _backend_unavailable() from exc
        command = replace(
            command,
            current_data_version=training.data_version if training else -1,
        )
        result = await self._strategy_write("decide", command, approve=approve)
        if result.code == "TARGET_REVIEW_STALE":
            raise AppError(
                code=result.code,
                message="目标配置已经变化，请重新计算",
                status_code=409,
            )
        return result

    async def list_calculation_runs(self, *, page: int = 1, page_size: int = 50):
        return await self._strategy_read(
            "list_calculations", page=page, page_size=page_size
        )

    async def list_reviews(self, *, page: int = 1, page_size: int = 50):
        return await self._strategy_read("list_reviews", page=page, page_size=page_size)

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

    async def _schedule(self, method, command, *, calculation):
        try:
            async with self._database.transaction() as session:
                jobs = self._job_service_factory(session)
                scope = f"target-calculate:{calculation.subscription_id}"
                await jobs.lock_submission(scope, calculation.idempotency_key)
                service = self._strategy_service_factory(
                    self._repository_factory(session),
                    subscriptions=self._subscription_factory(session),
                    audit=self._audit_factory(session),
                    events=self._event_factory(session),
                )
                reservation = await getattr(service, method)(command)
                job = await jobs.submit(
                    SubmitJob(
                        job_type="TARGET_CALCULATE",
                        queue="strategy-targets",
                        idempotency_scope=scope,
                        idempotency_key=calculation.idempotency_key,
                        request_id=calculation.request_id,
                        config_snapshot={"run_id": str(reservation.run_id)},
                        business_object_type="target_calculation_run",
                        business_object_id=str(reservation.run_id),
                        created_by_user_id=calculation.actor_user_id,
                        soft_timeout_seconds=300,
                        hard_timeout_seconds=360,
                    )
                )
                return CalculationSubmission(
                    code="TARGET_CALCULATION_ACCEPTED",
                    run_id=reservation.run_id,
                    job_id=job.id,
                    replayed=reservation.replayed,
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def _strategy_read(self, method, *args, **kwargs):
        try:
            async with self._database.session() as session:
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
