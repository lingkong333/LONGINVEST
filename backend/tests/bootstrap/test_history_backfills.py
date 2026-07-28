import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from long_invest.bootstrap import history_backfills
from long_invest.bootstrap.history_backfills import (
    DatabaseHistoryBarsProvider,
    DatabaseHistoryBarStore,
)
from long_invest.modules.daily_data.contracts import HistoricalDailyStoreResult
from long_invest.modules.history_backfills.contracts import (
    HistoryBackfillItemError,
    HistoryBackfillWorkItem,
    HistoryBarInput,
    HistoryBarsBundle,
)
from long_invest.modules.providers.resilience import ProviderCallError
from long_invest.modules.providers.retry import ProviderHttpError
from long_invest.modules.qfq.contracts import QfqHistoryStoreResult


class FakeDatabase:
    @asynccontextmanager
    async def session(self):
        yield object()

    @asynccontextmanager
    async def transaction(self):
        yield object()


def test_provider_transport_failure_keeps_stable_error_code(monkeypatch) -> None:
    class FailingProviderService:
        calls = 0

        async def daily_bars(self, request, deadline, *, concurrency):
            del request, deadline
            assert concurrency == 64
            self.calls += 1
            raise ProviderHttpError(
                "PROVIDER_UPSTREAM_TEMPORARY", retryable=True
            )

    service = FailingProviderService()
    monkeypatch.setattr(
        history_backfills,
        "build_provider_service",
        lambda session: service,
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
                concurrency=64,
            )
        assert captured.value.code == "PROVIDER_UPSTREAM_TEMPORARY"
        assert captured.value.retryable is True
        assert service.calls == 3

    asyncio.run(run())


def test_provider_waits_for_circuit_recovery(monkeypatch) -> None:
    class OpenCircuitProviderService:
        calls = 0

        async def daily_bars(self, request, deadline, *, concurrency):
            del request, deadline
            assert concurrency == 64
            self.calls += 1
            if self.calls == 1:
                raise ProviderCallError("PROVIDER_CIRCUIT_OPEN")
            return object()

    monkeypatch.setattr(
        history_backfills,
        "build_provider_service",
        lambda session: OpenCircuitProviderService(),
    )

    async def run() -> None:
        service = OpenCircuitProviderService()
        result = await history_backfills._wait_for_history_capacity(
            service,
            object(),
            deadline=datetime.now(UTC) + timedelta(seconds=2),
            concurrency=64,
        )

        assert result is not None
        assert service.calls == 2

    asyncio.run(run())


def test_store_continues_to_qfq_when_unadjusted_rows_need_review(monkeypatch) -> None:
    security_id = uuid4()
    dataset_id = uuid4()
    bar = HistoryBarInput(
        symbol="600519.SH",
        trade_date=date(2026, 7, 27),
        open=Decimal("10"),
        high=Decimal("11"),
        low=Decimal("9"),
        close=Decimal("10.5"),
        volume=100,
        amount=Decimal("1000"),
        source="SINA",
    )

    class FakeDailyDataService:
        def __init__(self, *args, **kwargs):
            del args, kwargs

        async def store_historical_bars(self, inputs, *, reason):
            assert len(inputs) == 1
            assert reason == "历史回填验收"
            return HistoricalDailyStoreResult(1, 0, 0, 1)

    class FakeQfqApplication:
        async def store_history(self, **kwargs):
            assert kwargs["symbol"] == "600519.SH"
            return QfqHistoryStoreResult(
                dataset_id=dataset_id,
                version=1,
                row_count=1,
                actual_start=bar.trade_date,
                actual_end=bar.trade_date,
                unchanged=False,
            )

    monkeypatch.setattr(history_backfills, "DailyDataService", FakeDailyDataService)
    monkeypatch.setattr(
        history_backfills, "get_qfq_application", lambda: FakeQfqApplication()
    )

    async def run() -> None:
        result = await DatabaseHistoryBarStore(FakeDatabase()).store(
            HistoryBackfillWorkItem(security_id, bar.symbol),
            HistoryBarsBundle((bar,), (bar,), "SINA:config-v1"),
            idempotency_key="history-review-continues",
            reason="历史回填验收",
        )

        assert result.review_required == 1
        assert result.qfq_dataset_id == dataset_id

    asyncio.run(run())
