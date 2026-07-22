from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from long_invest.modules.backtests.contracts import (
    BacktestDailyResultView,
    BacktestForecastSnapshotView,
    BacktestMetricView,
    BacktestOrderView,
    BacktestTargetAdjustmentView,
    BacktestTradeView,
)
from long_invest.modules.backtests.models import (
    BacktestDailyResult,
    BacktestForecastSnapshot,
    BacktestItem,
    BacktestMetric,
    BacktestOrder,
    BacktestTargetAdjustment,
    BacktestTask,
    BacktestTrade,
    BacktestUniverseSnapshot,
)


class BacktestRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_task(self, task_id: UUID, *, for_update: bool = False):
        statement = select(BacktestTask).where(BacktestTask.id == task_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def get_task_by_idempotency(
        self, idempotency_key: str, *, for_update: bool = False
    ):
        statement = select(BacktestTask).where(
            BacktestTask.idempotency_key == idempotency_key
        )
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def get_item(self, task_id: UUID, *, for_update: bool = False):
        statement = select(BacktestItem).where(BacktestItem.task_id == task_id)
        if for_update:
            statement = statement.with_for_update()
        return await self._session.scalar(statement)

    async def get_item_by_id(self, task_id: UUID, item_id: UUID):
        return await self._session.scalar(
            select(BacktestItem).where(
                BacktestItem.task_id == task_id, BacktestItem.id == item_id
            )
        )

    async def get_universe(self, task_id: UUID):
        return await self._session.scalar(
            select(BacktestUniverseSnapshot).where(
                BacktestUniverseSnapshot.task_id == task_id
            )
        )

    async def get_forecast(self, item_id: UUID):
        return await self._session.scalar(
            select(BacktestForecastSnapshot).where(
                BacktestForecastSnapshot.item_id == item_id
            )
        )

    async def get_metric(self, item_id: UUID):
        return await self._session.scalar(
            select(BacktestMetric).where(BacktestMetric.item_id == item_id)
        )

    async def add_task(
        self,
        task: BacktestTask,
        universe: BacktestUniverseSnapshot,
        item: BacktestItem,
    ) -> None:
        self._session.add_all((task, universe, item))
        await self._session.flush()

    async def add_forecast(self, forecast: BacktestForecastSnapshot) -> None:
        self._session.add(forecast)
        await self._session.flush()

    async def add_results(
        self,
        *,
        adjustments: Sequence[BacktestTargetAdjustment],
        orders: Sequence[BacktestOrder],
        trades: Sequence[BacktestTrade],
        daily_results: Sequence[BacktestDailyResult],
        metric: BacktestMetric,
    ) -> None:
        self._session.add_all(
            [*adjustments, *orders, *trades, *daily_results, metric]
        )
        await self._session.flush()

    async def list_tasks(self, *, page: int, page_size: int):
        statement = (
            select(BacktestTask)
            .order_by(BacktestTask.created_at.desc(), BacktestTask.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        rows = await self._session.scalars(statement)
        total = await self._session.scalar(select(func.count(BacktestTask.id)))
        return list(rows.all()), int(total or 0)

    async def list_orders(self, item_id: UUID):
        rows = await self._session.scalars(
            select(BacktestOrder)
            .where(BacktestOrder.item_id == item_id)
            .order_by(BacktestOrder.signal_date, BacktestOrder.id)
        )
        return list(rows.all())

    async def list_adjustments(self, item_id: UUID):
        rows = await self._session.scalars(
            select(BacktestTargetAdjustment)
            .where(BacktestTargetAdjustment.item_id == item_id)
            .order_by(BacktestTargetAdjustment.event_date)
        )
        return list(rows.all())

    async def list_trades(self, item_id: UUID):
        rows = await self._session.scalars(
            select(BacktestTrade)
            .where(BacktestTrade.item_id == item_id)
            .order_by(BacktestTrade.execute_date, BacktestTrade.id)
        )
        return list(rows.all())

    async def list_daily_results(self, item_id: UUID):
        rows = await self._session.scalars(
            select(BacktestDailyResult)
            .where(BacktestDailyResult.item_id == item_id)
            .order_by(BacktestDailyResult.trade_date)
        )
        return list(rows.all())


def forecast_view(row: BacktestForecastSnapshot) -> BacktestForecastSnapshotView:
    return BacktestForecastSnapshotView(
        item_id=row.item_id,
        training_start_date=row.training_start_date,
        training_end_date=row.training_end_date,
        training_row_count=row.training_row_count,
        training_fetched_at=row.training_fetched_at,
        training_data_hash=row.training_data_hash,
        source_code_hash=row.source_code_hash,
        parameter_hash=row.parameter_hash,
        values=_targets(row, ""),
        diagnostics=row.diagnostics,
        environment_version=row.environment_version,
        runner_image_digest=row.runner_image_digest,
        price_basis=row.price_basis,
        frozen_at=row.frozen_at,
    )


def order_view(row: BacktestOrder) -> BacktestOrderView:
    return BacktestOrderView(
        id=row.id,
        item_id=row.item_id,
        signal_date=row.signal_date,
        execute_date=row.execute_date,
        status=row.status,
        direction=row.direction,
        execution_price=row.execution_price,
        quantity=row.quantity,
        cash_before=row.cash_before,
        position_before=row.position_before,
        target_values=_targets(row, "target_"),
        target_zone=row.target_zone,
    )


def trade_view(row: BacktestTrade) -> BacktestTradeView:
    return BacktestTradeView(
        id=row.id,
        item_id=row.item_id,
        order_id=row.order_id,
        execute_date=row.execute_date,
        direction=row.direction,
        price=row.price,
        quantity=row.quantity,
        cash_after=row.cash_after,
        position_after=row.position_after,
        target_values=_targets(row, "target_"),
        target_zone=row.target_zone,
        round_trip_no=row.round_trip_no,
        holding_trade_days=row.holding_trade_days,
        realized_return_amount=row.realized_return_amount,
        realized_return_rate=row.realized_return_rate,
    )


def adjustment_view(row: BacktestTargetAdjustment) -> BacktestTargetAdjustmentView:
    return BacktestTargetAdjustmentView(
        item_id=row.item_id,
        event_date=row.event_date,
        before_values=_targets(row, "before_"),
        after_values=_targets(row, "after_"),
        adjustment_factor=row.adjustment_factor,
        source=row.source,
        data_hash=row.data_hash,
        published_at=row.published_at,
        effective_at=row.effective_at,
    )


def metric_view(row: BacktestMetric) -> BacktestMetricView:
    fields = BacktestMetricView.model_fields
    return BacktestMetricView(**{name: getattr(row, name) for name in fields})


def daily_view(row: BacktestDailyResult) -> BacktestDailyResultView:
    return BacktestDailyResultView(
        item_id=row.item_id,
        trade_date=row.trade_date,
        cash=row.cash,
        position_quantity=row.position_quantity,
        close_price=row.close_price,
        position_market_value=row.position_market_value,
        equity=row.equity,
        drawdown=row.drawdown,
        target_values=_targets(row, "target_"),
        zone=row.zone,
        position_status=row.position_status,
    )


def _targets(row, prefix: str):
    from long_invest.modules.targets.contracts import TargetValues

    return TargetValues(
        low_strong=getattr(row, f"{prefix}low_strong"),
        low_watch=getattr(row, f"{prefix}low_watch"),
        high_watch=getattr(row, f"{prefix}high_watch"),
        high_strong=getattr(row, f"{prefix}high_strong"),
    )
