from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.daily_data.repository import DailyDataRepository
from long_invest.modules.daily_data.service import DailyDataService
from long_invest.modules.providers.contracts import validate_symbol
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
    ) -> None:
        self._database = database
        self._job_service_factory = job_service_factory

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

    async def retry(
        self,
        *,
        batch_id: UUID,
        idempotency_key: str,
        request_id: str,
        created_by_user_id: str,
    ) -> Any:
        try:
            async with self._database.transaction() as session:
                repository = DailyDataRepository(session)
                service = DailyDataService(
                    repository,
                )
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
                    queue="daily-data",
                    idempotency_scope=f"daily-data:retry:{batch_id}",
                    idempotency_key=idempotency_key,
                    request_id=request_id,
                    config_snapshot={
                        "original_batch_id": str(batch_id),
                        "universe_snapshot_id": str(batch.universe_snapshot_id),
                        "trading_date": batch.trading_date.isoformat(),
                        "symbols": list(symbols),
                    },
                    business_object_type="daily_data_batch",
                    business_object_id=str(batch_id),
                    created_by_user_id=created_by_user_id,
                )
                return await self._job_service_factory(session).submit(command)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


def get_daily_data_application() -> DailyDataApplication:
    return DailyDataApplication(get_database())


def _validate_symbol(symbol: str) -> None:
    try:
        validate_symbol(symbol)
    except ValueError as exc:
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
