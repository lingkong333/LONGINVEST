from __future__ import annotations

from collections.abc import Callable
from datetime import date
from hashlib import sha256
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from long_invest.modules.daily_data.contracts import (
    DailyBarSnapshot,
    DailyBatchAction,
    DailyRetryAuditContext,
)
from long_invest.modules.daily_data.repository import DailyDataRepository
from long_invest.modules.daily_data.service import (
    DailyDataService,
    daily_batch_allowed_actions,
)
from long_invest.modules.providers.contracts import validate_symbol
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.service import JobService


class DailyDataApplication:
    def __init__(
        self,
        database: Database,
        *,
        job_service_factory: Callable[..., JobService] = JobService,
        repository_factory: Callable[..., Any] = DailyDataRepository,
        domain_service_factory: Callable[..., Any] = DailyDataService,
        audit_service_factory: Callable[..., Any] = AuditService,
    ) -> None:
        self._database = database
        self._job_service_factory = job_service_factory
        self._repository_factory = repository_factory
        self._domain_service_factory = domain_service_factory
        self._audit_service_factory = audit_service_factory

    @staticmethod
    def allowed_actions(batch: Any) -> tuple[DailyBatchAction, ...]:
        return daily_batch_allowed_actions(
            batch.status,
            missing_count=batch.missing_count,
            failed_count=batch.failed_count,
        )

    async def list_batches(self, *, page: int, page_size: int):
        try:
            async with self._database.session() as session:
                repository = DailyDataRepository(session)
                return (
                    await repository.list_batches(page=page, page_size=page_size),
                    await repository.count_batches(),
                )
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def list_missing(self, batch_id: UUID, *, page: int, page_size: int):
        try:
            async with self._database.session() as session:
                repository = DailyDataRepository(session)
                if await repository.get_batch(batch_id) is None:
                    raise AppError(
                        code="DAILY_BATCH_NOT_FOUND",
                        message="日线批次不存在",
                        status_code=404,
                    )
                return (
                    await repository.list_missing(
                        batch_id, page=page, page_size=page_size
                    ),
                    await repository.count_missing(batch_id),
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def list_bars(
        self,
        symbol: str,
        *,
        start: date,
        end: date,
        page: int,
        page_size: int,
    ):
        _validate_symbol(symbol)
        if start > end:
            raise AppError(
                code="DAILY_DATE_RANGE_INVALID",
                message="开始日期不能晚于结束日期",
                status_code=422,
            )
        try:
            async with self._database.session() as session:
                repository = DailyDataRepository(session)
                return (
                    await repository.list_bars(
                        symbol,
                        start=start,
                        end=end,
                        page=page,
                        page_size=page_size,
                    ),
                    await repository.count_bars(symbol, start=start, end=end),
                )
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def list_revisions(self, symbol: str, *, page: int, page_size: int):
        _validate_symbol(symbol)
        try:
            async with self._database.session() as session:
                repository = DailyDataRepository(session)
                return (
                    await repository.list_revisions(
                        symbol, page=page, page_size=page_size
                    ),
                    await repository.count_revisions(symbol),
                )
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def snapshot(self, symbol: str, trade_date: date) -> DailyBarSnapshot | None:
        _validate_symbol(symbol)
        try:
            async with self._database.session() as session:
                repository = self._repository_factory(session)
                bar = await repository.get_bar_by_symbol_date(symbol, trade_date)
                if bar is None:
                    return None
                return DailyBarSnapshot(
                    security_id=bar.security_id,
                    symbol=bar.symbol,
                    trade_date=bar.trade_date,
                    close=bar.close,
                    data_version=bar.data_version,
                    source=bar.source,
                    updated_at=bar.updated_at,
                )
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def retry(
        self,
        *,
        batch_id: UUID,
        audit_context: DailyRetryAuditContext,
    ) -> Any:
        try:
            async with self._database.transaction() as session:
                repository = self._repository_factory(session)
                service = self._domain_service_factory(repository)
                symbols = await service.retry_scope(batch_id)
                if not symbols:
                    raise AppError(
                        code="DAILY_RETRY_SCOPE_EMPTY",
                        message="原批次没有可重试股票",
                        status_code=409,
                    )
                batch = await repository.get_batch(batch_id)
                command = SubmitJob(
                    job_type="DAILY_DATA_RETRY",
                    queue="daily-market-data",
                    idempotency_scope=f"daily-data:retry:{batch_id}",
                    idempotency_key=audit_context.idempotency_key,
                    request_id=audit_context.request_id,
                    config_snapshot={
                        "original_batch_id": str(batch_id),
                        "universe_snapshot_id": str(batch.universe_snapshot_id),
                        "trading_date": batch.trading_date.isoformat(),
                        "symbols": list(symbols),
                        "known_corporate_action_symbols": [
                            symbol
                            for symbol in symbols
                            if symbol in batch.known_corporate_action_symbols
                        ],
                        "reason": audit_context.reason,
                    },
                    business_object_type="daily_data_batch",
                    business_object_id=str(batch_id),
                    created_by_user_id=audit_context.actor_user_id,
                    soft_timeout_seconds=300,
                    hard_timeout_seconds=600,
                )
                job = await self._job_service_factory(session).submit(command)
                audit = self._audit_service_factory(session)
                audit_key = _retry_audit_key(batch_id, audit_context.idempotency_key)
                audit_write = AuditWrite(
                    action_code="daily_data.batch_retry_requested",
                    object_type="daily_data_batch",
                    object_id=str(batch_id),
                    result="SUCCESS",
                    request_id=audit_context.request_id,
                    idempotency_key=audit_key,
                    risk_level="HIGH",
                    reason=audit_context.reason,
                    before_summary=None,
                    after_summary={
                        "retry_symbols": list(symbols),
                        "trading_date": batch.trading_date.isoformat(),
                    },
                    actor_user_id=audit_context.actor_user_id,
                    session_id=audit_context.session_id,
                    trusted_ip=audit_context.trusted_ip,
                )
                try:
                    async with session.begin_nested():
                        await audit.append(audit_write)
                except IntegrityError:
                    existing_audit = await audit.find_by_idempotency(audit_key)
                    if existing_audit is None:
                        raise
                    if not _same_audit_content(existing_audit, audit_write):
                        raise AppError(
                            code="DAILY_RETRY_AUDIT_CONFLICT",
                            message="该重试请求的审计幂等键已用于不同内容",
                            status_code=409,
                        ) from None
                return job
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


def get_daily_data_application() -> DailyDataApplication:
    return DailyDataApplication(get_database())


def _validate_symbol(symbol: str) -> None:
    try:
        validate_symbol(symbol)
    except (TypeError, ValueError) as exc:
        raise AppError(
            code="DAILY_BAR_SYMBOL_INVALID",
            message="股票代码格式无效",
            status_code=422,
        ) from exc


def _backend_unavailable() -> AppError:
    return AppError(
        code="DAILY_DATA_BACKEND_UNAVAILABLE",
        message="日线数据服务暂时不可用",
        status_code=503,
    )


def _retry_audit_key(batch_id: UUID, idempotency_key: str) -> str:
    digest = sha256(f"{batch_id}\0{idempotency_key}".encode()).hexdigest()
    return f"daily-retry:{digest}"


def _same_audit_content(existing: Any, candidate: AuditWrite) -> bool:
    fields = (
        "action_code",
        "object_type",
        "object_id",
        "result",
        "request_id",
        "idempotency_key",
        "risk_level",
        "reason",
        "before_summary",
        "after_summary",
        "actor_user_id",
        "session_id",
        "trusted_ip",
    )
    return all(
        getattr(existing, field) == getattr(candidate, field) for field in fields
    )
