from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from long_invest.bootstrap.strategy_data import QfqStrategyDataPort
from long_invest.modules.qfq.contracts import (
    QfqBarView,
    QfqDatasetLifecycle,
    QfqDatasetView,
    QfqDataWindow,
    QfqFreshness,
)
from long_invest.modules.strategies.forecast import hash_training_data_snapshot
from long_invest.platform.errors import AppError


def _window(*, freshness: QfqFreshness = QfqFreshness.FRESH) -> QfqDataWindow:
    security_id = uuid4()
    now = datetime(2026, 7, 21, tzinfo=UTC)
    dataset = QfqDatasetView(
        id=uuid4(),
        security_id=security_id,
        symbol="600000.SH",
        version=7,
        requested_start=date(2025, 1, 1),
        requested_end=date(2025, 12, 31),
        actual_start=date(2025, 1, 2),
        actual_end=date(2025, 12, 31),
        as_of_date=date(2025, 12, 31),
        provider="eastmoney",
        provider_contract_version="v1",
        anchor_date=date(2025, 12, 31),
        anchor_close="10.00",
        row_count=1,
        checksum="a" * 64,
        lifecycle=QfqDatasetLifecycle.CURRENT,
        freshness=freshness,
        stale_reason=None if freshness is QfqFreshness.FRESH else "DATA_REVISED",
        created_at=now,
        activated_at=now,
        superseded_at=None,
    )
    return QfqDataWindow(
        dataset=dataset,
        bars=(
            QfqBarView(
                trade_date=date(2025, 12, 31),
                open="9.00",
                high="11.00",
                low="8.00",
                close="10.00",
                volume=100,
                amount="1000.0000",
            ),
        ),
    )


@pytest.mark.anyio
async def test_builds_verified_snapshot_from_public_qfq_window() -> None:
    window = _window()
    qfq = SimpleNamespace(get_window=lambda *args, **kwargs: None)

    async def get_window(*args, **kwargs):
        return window

    qfq.get_window = get_window
    port = QfqStrategyDataPort(qfq)

    snapshot = await port.get_training_data(
        security_id=window.dataset.security_id,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
    )

    assert snapshot is not None
    assert snapshot.data_version == 7
    assert snapshot.price_basis == "QFQ_AS_OF"
    assert snapshot.content_hash == hash_training_data_snapshot(snapshot)


@pytest.mark.anyio
async def test_rejects_stale_or_missing_qfq_data() -> None:
    stale = _window(freshness=QfqFreshness.STALE)

    class FakeQfq:
        async def get_window(self, security_id, *, start, end):
            if security_id == stale.dataset.security_id:
                return stale
            raise AppError(
                code="QFQ_DATA_NOT_FOUND", message="missing", status_code=404
            )

    port = QfqStrategyDataPort(FakeQfq())

    assert (
        await port.get_training_data(
            security_id=stale.dataset.security_id,
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )
        is None
    )
    assert (
        await port.get_test_data(
            security_id=uuid4(),
            start_date=date(2025, 1, 1),
            end_date=date(2025, 12, 31),
        )
        is None
    )
