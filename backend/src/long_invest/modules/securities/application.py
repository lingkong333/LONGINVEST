from __future__ import annotations

from collections.abc import Callable

from long_invest.modules.securities.contracts import validate_symbol
from long_invest.modules.securities.models import Security
from long_invest.modules.securities.repository import SecurityRepository
from long_invest.platform.database.engine import Database, get_database
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.models import Job
from long_invest.platform.jobs.service import JobService


class SecurityApplication:
    def __init__(
        self,
        database: Database,
        *,
        job_service_factory: Callable[..., JobService] = JobService,
    ) -> None:
        self._database = database
        self._job_service_factory = job_service_factory

    async def list(
        self, *, page: int, page_size: int
    ) -> tuple[list[Security], int]:
        async with self._database.session() as session:
            repository = SecurityRepository(session)
            return (
                await repository.list(page=page, page_size=page_size),
                await repository.count(),
            )

    async def search(
        self, *, query: str, page: int, page_size: int
    ) -> tuple[list[Security], int]:
        async with self._database.session() as session:
            repository = SecurityRepository(session)
            return (
                await repository.search(query, page=page, page_size=page_size),
                await repository.count_search(query),
            )

    async def get(self, symbol: str) -> Security:
        try:
            validate_symbol(symbol)
        except ValueError as exc:
            raise AppError(
                code="SECURITY_SYMBOL_INVALID",
                message=str(exc),
                status_code=422,
            ) from exc
        async with self._database.session() as session:
            security = await SecurityRepository(session).get_by_symbol(symbol)
        if security is None:
            raise AppError(
                code="SECURITY_NOT_FOUND",
                message="股票不存在",
                status_code=404,
            )
        return security

    async def refresh(
        self,
        *,
        idempotency_key: str,
        request_id: str,
        created_by_user_id: str,
    ) -> Job:
        command = SubmitJob(
            job_type="SECURITY_MASTER_REFRESH",
            queue="maintenance",
            idempotency_scope="securities:refresh",
            idempotency_key=idempotency_key,
            request_id=request_id,
            config_snapshot={"source": "eastmoney"},
            business_object_type="security_master",
            created_by_user_id=created_by_user_id,
        )
        async with self._database.transaction() as session:
            return await self._job_service_factory(session).submit(command)


def get_security_application() -> SecurityApplication:
    return SecurityApplication(get_database())
