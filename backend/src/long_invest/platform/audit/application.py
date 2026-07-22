from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import SQLAlchemyError

from long_invest.platform.audit.query import (
    AuditEventFilters,
    AuditEventPage,
    AuditEventQuery,
)
from long_invest.platform.database.engine import Database
from long_invest.platform.errors import AppError

DEFAULT_AUDIT_WINDOW = timedelta(days=30)
MAX_AUDIT_WINDOW = timedelta(days=90)


class AuditQueryApplication:
    def __init__(
        self,
        database: Database,
        *,
        query_factory: Callable[[object], AuditEventQuery] = AuditEventQuery,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._database = database
        self._query_factory = query_factory
        self._clock = clock or (lambda: datetime.now(UTC))

    async def list_events(
        self,
        *,
        page: int,
        page_size: int,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
        actor_user_id: str | None = None,
        action_code: str | None = None,
        object_type: str | None = None,
        object_id: str | None = None,
        result: str | None = None,
        risk_level: str | None = None,
        request_id: str | None = None,
    ) -> AuditEventPage:
        end = end_at or self._clock()
        start = start_at or (end - DEFAULT_AUDIT_WINDOW)
        _validate_window(start, end)
        filters = AuditEventFilters(
            start_at=start,
            end_at=end,
            actor_user_id=actor_user_id,
            action_code=action_code,
            object_type=object_type,
            object_id=object_id,
            result=result,
            risk_level=risk_level,
            request_id=request_id,
        )
        try:
            async with self._database.session() as session:
                return await self._query_factory(session).list_events(
                    filters,
                    page=page,
                    page_size=page_size,
                )
        except AppError:
            raise
        except (SQLAlchemyError, TimeoutError) as exc:
            raise AppError(
                code="AUDIT_BACKEND_UNAVAILABLE",
                message="审计查询暂时不可用",
                status_code=503,
            ) from exc


def _validate_window(start_at: datetime, end_at: datetime) -> None:
    if start_at.tzinfo is None or end_at.tzinfo is None:
        raise AppError(
            code="AUDIT_TIME_RANGE_INVALID",
            message="审计查询时间必须包含时区",
            status_code=422,
        )
    if start_at > end_at:
        raise AppError(
            code="AUDIT_TIME_RANGE_INVALID",
            message="审计查询开始时间不能晚于结束时间",
            status_code=422,
        )
    if end_at - start_at > MAX_AUDIT_WINDOW:
        raise AppError(
            code="AUDIT_TIME_RANGE_TOO_WIDE",
            message="审计查询时间范围不能超过 90 天",
            status_code=422,
        )
