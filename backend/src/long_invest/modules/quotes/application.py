from collections.abc import Callable
from typing import Any
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.quotes.contracts import QuoteCycleStatus
from long_invest.modules.quotes.repository import QuoteCycleRepository
from long_invest.modules.quotes.service import _item_view, _summary
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.service import JobService


class QuoteApplication:
    def __init__(
        self,
        database: Database,
        *,
        job_service_factory: Callable[..., JobService] = JobService,
    ) -> None:
        self._database = database
        self._job_service_factory = job_service_factory

    async def list_cycles(
        self,
        *,
        status: QuoteCycleStatus | None,
        page: int,
        page_size: int,
    ) -> dict[str, object]:
        try:
            async with self._database.session() as session:
                repository = QuoteCycleRepository(session)
                items = await repository.list(
                    status=status, page=page, page_size=page_size
                )
                total = await repository.count(status=status)
                return {
                    "items": [_summary(item) for item in items],
                    "total": total,
                    "page": page,
                    "page_size": page_size,
                }
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def list_items(
        self, cycle_id: UUID, *, page: int, page_size: int
    ) -> list[object]:
        try:
            async with self._database.session() as session:
                repository = QuoteCycleRepository(session)
                if await repository.get_with_items(cycle_id) is None:
                    raise AppError(
                        code="QUOTE_CYCLE_NOT_FOUND",
                        message="行情批次不存在",
                        status_code=404,
                    )
                return [
                    _item_view(item)
                    for item in await repository.list_items(
                        cycle_id, page=page, page_size=page_size
                    )
                ]
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc

    async def submit_manual(
        self,
        *,
        symbols: tuple[str, ...],
        timeout_seconds: int,
        idempotency_key: str,
        request_id: str,
        created_by_user_id: str,
    ) -> Any:
        return await self._submit(
            job_type="REALTIME_QUOTE_CYCLE",
            queue="realtime-quotes",
            scope="quotes:manual",
            symbols=symbols,
            idempotency_key=idempotency_key,
            request_id=request_id,
            created_by_user_id=created_by_user_id,
            extra={"timeout_seconds": timeout_seconds},
        )

    async def submit_diagnostic(
        self,
        *,
        symbols: tuple[str, ...],
        idempotency_key: str,
        request_id: str,
        created_by_user_id: str,
    ) -> Any:
        return await self._submit(
            job_type="QUOTE_DIAGNOSTIC",
            queue="realtime-quotes",
            scope="quotes:diagnostic",
            symbols=symbols,
            idempotency_key=idempotency_key,
            request_id=request_id,
            created_by_user_id=created_by_user_id,
            extra={},
        )

    async def _submit(
        self,
        *,
        job_type: str,
        queue: str,
        scope: str,
        symbols: tuple[str, ...],
        idempotency_key: str,
        request_id: str,
        created_by_user_id: str,
        extra: dict[str, object],
    ) -> Any:
        command = SubmitJob(
            job_type=job_type,
            queue=queue,
            idempotency_scope=f"{scope}:{created_by_user_id}",
            idempotency_key=idempotency_key,
            request_id=request_id,
            config_snapshot={"symbols": list(symbols), **extra},
            business_object_type="quote_cycle_request",
            created_by_user_id=created_by_user_id,
        )
        try:
            async with self._database.transaction() as session:
                return await self._job_service_factory(session).submit(command)
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise _backend_unavailable() from exc


def get_quote_application() -> QuoteApplication:
    return QuoteApplication(get_database())


def _backend_unavailable() -> AppError:
    return AppError(
        code="QUOTE_BACKEND_UNAVAILABLE",
        message="实时行情服务暂时不可用",
        status_code=503,
    )
