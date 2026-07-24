import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import pytest

from long_invest.bootstrap import history_backfills
from long_invest.bootstrap.history_backfills import DatabaseHistoryBarsProvider
from long_invest.modules.history_backfills.contracts import (
    HistoryBackfillItemError,
    HistoryBackfillWorkItem,
)
from long_invest.modules.providers.retry import ProviderHttpError


class FakeDatabase:
    @asynccontextmanager
    async def session(self):
        yield object()


def test_provider_transport_failure_keeps_stable_error_code(monkeypatch) -> None:
    class FailingProviderService:
        async def daily_bars(self, request, deadline):
            del request, deadline
            raise ProviderHttpError(
                "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
            )

    monkeypatch.setattr(
        history_backfills,
        "build_provider_service",
        lambda session: FailingProviderService(),
    )

    async def run() -> None:
        provider = DatabaseHistoryBarsProvider(FakeDatabase())
        with pytest.raises(HistoryBackfillItemError) as captured:
            await provider.fetch(
                HistoryBackfillWorkItem(
                    security_id=uuid4(),
                    symbol="600519.SH",
                ),
                start_date=date(2025, 1, 1),
                end_date=date(2025, 12, 31),
                deadline=datetime.now(UTC) + timedelta(seconds=2),
            )
        assert captured.value.code == "PROVIDER_UPSTREAM_TEMPORARY"
        assert captured.value.retryable is True

    asyncio.run(run())
