from __future__ import annotations

from datetime import date
from uuid import UUID

from long_invest.modules.qfq.application import QfqApplication
from long_invest.modules.qfq.contracts import QfqFreshness
from long_invest.modules.strategies.contracts import TrainingDataSnapshot
from long_invest.modules.strategies.forecast import hash_training_data_snapshot
from long_invest.platform.errors import AppError


class QfqStrategyDataPort:
    """Translate the qfq module's public window into a frozen strategy snapshot."""

    def __init__(self, qfq: QfqApplication) -> None:
        self._qfq = qfq

    async def get_training_data(
        self, *, security_id: UUID, start_date: date, end_date: date
    ) -> TrainingDataSnapshot | None:
        return await self._get_data(
            security_id=security_id, start_date=start_date, end_date=end_date
        )

    async def get_test_data(
        self, *, security_id: UUID, start_date: date, end_date: date
    ) -> TrainingDataSnapshot | None:
        return await self._get_data(
            security_id=security_id, start_date=start_date, end_date=end_date
        )

    async def _get_data(
        self, *, security_id: UUID, start_date: date, end_date: date
    ) -> TrainingDataSnapshot | None:
        try:
            window = await self._qfq.get_window(
                security_id, start=start_date, end=end_date
            )
        except AppError as exc:
            if exc.code == "QFQ_DATA_NOT_FOUND":
                return None
            raise
        dataset = window.dataset
        if dataset.freshness is not QfqFreshness.FRESH:
            return None
        rows = tuple(
            {
                "trade_date": bar.trade_date,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
                "amount": bar.amount,
            }
            for bar in window.bars
        )
        snapshot = TrainingDataSnapshot(
            security_id=dataset.security_id,
            symbol=dataset.symbol,
            start_date=start_date,
            end_date=end_date,
            data_version=dataset.version,
            fetched_at=dataset.created_at,
            source=dataset.provider.upper(),
            price_basis="QFQ_AS_OF",
            content_hash="0" * 64,
            rows=rows,
        )
        return snapshot.model_copy(
            update={"content_hash": hash_training_data_snapshot(snapshot)}
        )
