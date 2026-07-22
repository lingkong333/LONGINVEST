from datetime import datetime
from unittest.mock import AsyncMock

import pytest

from long_invest.modules.dashboard.application import DashboardApplication
from long_invest.platform.errors import AppError

pytestmark = pytest.mark.anyio


async def test_timeline_rejects_cursor_without_timezone() -> None:
    application = object.__new__(DashboardApplication)
    application._service = AsyncMock()

    with pytest.raises(AppError) as error:
        await application.timeline(limit=50, before=datetime(2026, 7, 22))

    assert error.value.code == "DASHBOARD_TIME_CURSOR_INVALID"
    application._service.timeline.assert_not_awaited()
