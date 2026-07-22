from __future__ import annotations

from datetime import date
from decimal import Decimal
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


class PointInTimeBacktestDataPort:
    """Build training data at its cutoff and keep holdout prices unadjusted."""

    price_basis = "PIT_QFQ_TRAIN_RAW_TEST"

    def __init__(self, qfq: QfqApplication, daily: object) -> None:
        self._qfq = qfq
        self._daily = daily

    async def get_training_data(
        self, *, security_id: UUID, start_date: date, end_date: date
    ) -> TrainingDataSnapshot | None:
        window = await self._qfq_window(security_id, start_date, end_date)
        if window is None:
            return None
        raw = await self._raw(window.dataset.symbol, start_date, end_date)
        if raw is None or len(raw) != len(window.bars):
            return None
        if any(
            bar.security_id != security_id
            or bar.source.upper() != window.dataset.provider.upper()
            for bar in raw
        ):
            return None
        by_date = {bar.trade_date: bar for bar in raw}
        if set(by_date) != {bar.trade_date for bar in window.bars}:
            return None
        anchor_qfq = Decimal(str(window.bars[-1].close))
        anchor_raw = Decimal(str(by_date[window.bars[-1].trade_date].close))
        anchor_ratio = anchor_qfq / anchor_raw
        if not anchor_ratio.is_finite() or anchor_ratio <= 0:
            return None
        rows = tuple(
            {
                "trade_date": bar.trade_date,
                "open": Decimal(str(bar.open)) / anchor_ratio,
                "high": Decimal(str(bar.high)) / anchor_ratio,
                "low": Decimal(str(bar.low)) / anchor_ratio,
                "close": Decimal(str(bar.close)) / anchor_ratio,
                "volume": bar.volume,
                "amount": bar.amount,
            }
            for bar in window.bars
        )
        return self._snapshot(
            security_id=security_id,
            symbol=window.dataset.symbol,
            start_date=start_date,
            end_date=end_date,
            data_version=max(
                window.dataset.version, *(bar.data_version for bar in raw)
            ),
            fetched_at=max(
                window.dataset.created_at, *(bar.updated_at for bar in raw)
            ),
            source=window.dataset.provider.upper(),
            rows=rows,
        )

    async def get_test_data(
        self, *, security_id: UUID, start_date: date, end_date: date
    ) -> TrainingDataSnapshot | None:
        window = await self._qfq_window(security_id, start_date, end_date)
        if window is None:
            return None
        raw = await self._raw(window.dataset.symbol, start_date, end_date)
        if raw is None:
            return None
        if any(bar.security_id != security_id for bar in raw):
            return None
        return self._snapshot(
            security_id=security_id,
            symbol=window.dataset.symbol,
            start_date=start_date,
            end_date=end_date,
            data_version=max(bar.data_version for bar in raw),
            fetched_at=max(bar.updated_at for bar in raw),
            source=raw[0].source.upper(),
            rows=tuple(
                {
                    "trade_date": bar.trade_date,
                    "open": bar.open,
                    "high": bar.high,
                    "low": bar.low,
                    "close": bar.close,
                    "volume": bar.volume,
                    "amount": bar.amount,
                }
                for bar in raw
            ),
        )

    async def _qfq_window(self, security_id: UUID, start: date, end: date):
        try:
            window = await self._qfq.get_window(security_id, start=start, end=end)
        except AppError as exc:
            if exc.code == "QFQ_DATA_NOT_FOUND":
                return None
            raise
        if window.dataset.freshness is not QfqFreshness.FRESH or not window.bars:
            return None
        return window

    async def _raw(self, symbol: str, start: date, end: date):
        rows, total = await self._daily.list_bars(
            symbol, start=start, end=end, page=1, page_size=100_000
        )
        if total == 0 or total != len(rows):
            return None
        return tuple(rows)

    def _snapshot(self, **values) -> TrainingDataSnapshot:
        snapshot = TrainingDataSnapshot(
            **values,
            price_basis=self.price_basis,
            content_hash="0" * 64,
        )
        return snapshot.model_copy(
            update={"content_hash": hash_training_data_snapshot(snapshot)}
        )
