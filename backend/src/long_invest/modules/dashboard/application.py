from __future__ import annotations

from datetime import datetime

from long_invest.modules.dashboard.contracts import (
    DashboardSummary,
    DashboardTimeline,
    DashboardTimelineUnavailable,
)
from long_invest.modules.dashboard.service import DashboardService
from long_invest.platform.errors import AppError


class DashboardApplication:
    def __init__(self, service: DashboardService) -> None:
        self._service = service

    async def summary(self) -> DashboardSummary:
        return await self._service.summary()

    async def timeline(
        self,
        *,
        limit: int,
        before: datetime | None,
    ) -> DashboardTimeline:
        if before is not None and before.tzinfo is None:
            raise AppError(
                code="DASHBOARD_TIME_CURSOR_INVALID",
                message="Dashboard timeline cursor must include a timezone",
                status_code=422,
            )
        try:
            return await self._service.timeline(limit=limit, before=before)
        except DashboardTimelineUnavailable as exc:
            raise AppError(
                code="DASHBOARD_TIMELINE_UNAVAILABLE",
                message="Dashboard timeline is temporarily unavailable",
                status_code=503,
            ) from exc
