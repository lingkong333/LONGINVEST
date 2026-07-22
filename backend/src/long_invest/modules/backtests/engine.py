from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, localcontext
from statistics import fmean, pstdev
from uuid import UUID, uuid5
from zoneinfo import ZoneInfo

from long_invest.modules.backtests.contracts import (
    BacktestDailyResultView,
    BacktestMetricView,
    BacktestOrderDirection,
    BacktestOrderStatus,
    BacktestOrderView,
    BacktestPositionStatus,
    BacktestSignalRuleInput,
    BacktestSignalRulePort,
    BacktestTargetAdjustmentView,
    BacktestTradeView,
)
from long_invest.modules.market_data.contracts import AdjustmentTimelineEntry
from long_invest.modules.signals.contracts import SignalZone
from long_invest.modules.targets.contracts import TargetValues

_CENT = Decimal("0.01")
_QUANTITY = Decimal("0.000001")
_RATIO = Decimal("0.00000001")
_SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True, slots=True)
class BacktestBar:
    trade_date: date
    open_price: Decimal
    close_price: Decimal

    def __post_init__(self) -> None:
        if (
            not self.open_price.is_finite()
            or not self.close_price.is_finite()
            or self.open_price <= 0
            or self.close_price <= 0
        ):
            raise ValueError("backtest prices must be finite and positive")


@dataclass(frozen=True, slots=True)
class BacktestEngineResult:
    adjustments: tuple[BacktestTargetAdjustmentView, ...]
    orders: tuple[BacktestOrderView, ...]
    trades: tuple[BacktestTradeView, ...]
    daily_results: tuple[BacktestDailyResultView, ...]
    metric: BacktestMetricView


@dataclass(slots=True)
class _Position:
    cash: Decimal
    quantity: Decimal = Decimal("0")
    entry_price: Decimal | None = None
    entry_index: int | None = None
    round_trip_no: int = 1

    @property
    def status(self) -> BacktestPositionStatus:
        if self.quantity > 0:
            return BacktestPositionStatus.HOLDING
        return BacktestPositionStatus.FLAT


class FixedTargetBacktestEngine:
    def __init__(
        self, signal_rules: BacktestSignalRulePort, *, rule_version: str
    ) -> None:
        if not rule_version.strip():
            raise ValueError("backtest rule version must not be blank")
        self._signal_rules = signal_rules
        self._rule_version = rule_version

    @property
    def rule_version(self) -> str:
        return self._rule_version

    def run(
        self,
        *,
        item_id: UUID,
        security_id: UUID,
        bars: tuple[BacktestBar, ...],
        targets: TargetValues,
        adjustments: tuple[AdjustmentTimelineEntry, ...],
        initial_capital: Decimal,
        hysteresis_ratio: Decimal,
        minimum_hysteresis: Decimal,
    ) -> BacktestEngineResult:
        _validate_inputs(bars, initial_capital, adjustments)
        position = _Position(cash=_money(initial_capital))
        current_targets = targets
        current_zone = SignalZone.UNKNOWN
        pending: BacktestOrderView | None = None
        orders: list[BacktestOrderView] = []
        trades: list[BacktestTradeView] = []
        daily: list[BacktestDailyResultView] = []
        recorded_adjustments: list[BacktestTargetAdjustmentView] = []
        ordered_adjustments = sorted(
            adjustments,
            key=lambda entry: (
                entry.effective_date,
                entry.event_date,
                entry.published_at,
                entry.source,
                entry.data_hash,
            ),
        )
        adjustment_index = 0
        peak_equity = position.cash

        for index, bar in enumerate(bars):
            while (
                adjustment_index < len(ordered_adjustments)
                and ordered_adjustments[adjustment_index].effective_date
                <= bar.trade_date
            ):
                entry = ordered_adjustments[adjustment_index]
                current_targets, recorded = _adjust_targets(
                    item_id, current_targets, entry
                )
                pending = _adjust_open_position(position, pending, entry)
                recorded_adjustments.append(recorded)
                adjustment_index += 1

            if pending is not None:
                pending, trade = _fill_order(pending, bar, index, position)
                orders.append(pending)
                trades.append(trade)
                pending = None

            signal = BacktestSignalRuleInput(
                security_id=security_id,
                trade_date=bar.trade_date,
                close_price=bar.close_price,
                targets=current_targets,
                previous_zone=current_zone,
                position_status=position.status,
                hysteresis_ratio=hysteresis_ratio,
                minimum_hysteresis=minimum_hysteresis,
            )
            current_zone = self._signal_rules.evaluate(signal).zone
            if pending is None:
                pending = _new_order(
                    item_id, bar.trade_date, current_zone, position, current_targets
                )

            market_value = _money(position.quantity * bar.close_price)
            equity = _money(position.cash + market_value)
            peak_equity = max(peak_equity, equity)
            drawdown = _ratio((peak_equity - equity) / peak_equity)
            daily.append(
                BacktestDailyResultView(
                    item_id=item_id,
                    trade_date=bar.trade_date,
                    cash=position.cash,
                    position_quantity=position.quantity,
                    close_price=bar.close_price,
                    position_market_value=market_value,
                    equity=equity,
                    drawdown=drawdown,
                    target_values=current_targets,
                    zone=current_zone,
                    position_status=position.status,
                )
            )

        if pending is not None:
            orders.append(
                pending.model_copy(
                    update={"status": BacktestOrderStatus.UNFILLED_AT_END}
                )
            )
        metric = _metrics(item_id, initial_capital, daily, trades, orders, position)
        return BacktestEngineResult(
            adjustments=tuple(recorded_adjustments),
            orders=tuple(orders),
            trades=tuple(trades),
            daily_results=tuple(daily),
            metric=metric,
        )


def _validate_inputs(
    bars: tuple[BacktestBar, ...],
    initial_capital: Decimal,
    adjustments: tuple[AdjustmentTimelineEntry, ...],
) -> None:
    if not bars:
        raise ValueError("test data must contain at least one bar")
    dates = [bar.trade_date for bar in bars]
    if dates != sorted(dates) or len(set(dates)) != len(dates):
        raise ValueError("test bars must be unique and ordered")
    if not initial_capital.is_finite() or initial_capital <= 0:
        raise ValueError("initial capital must be finite and positive")
    effective_dates = [entry.effective_date for entry in adjustments]
    if len(set(effective_dates)) != len(effective_dates):
        raise ValueError("adjustment dates must be unique")


def _adjust_targets(
    item_id: UUID,
    before: TargetValues,
    entry: AdjustmentTimelineEntry,
) -> tuple[TargetValues, BacktestTargetAdjustmentView]:
    effective_at = datetime.combine(entry.effective_date, time.min, _SHANGHAI)
    if entry.published_at > effective_at:
        raise ValueError("adjustment was not public before its effective date")
    after = TargetValues(
        low_strong=before.low_strong * entry.adjustment_factor,
        low_watch=before.low_watch * entry.adjustment_factor,
        high_watch=before.high_watch * entry.adjustment_factor,
        high_strong=before.high_strong * entry.adjustment_factor,
    )
    return after, BacktestTargetAdjustmentView(
        item_id=item_id,
        event_date=entry.event_date,
        before_values=before,
        after_values=after,
        adjustment_factor=entry.adjustment_factor,
        source=entry.source,
        data_hash=entry.data_hash,
        published_at=entry.published_at,
        effective_at=effective_at,
    )


def _adjust_open_position(
    position: _Position,
    pending: BacktestOrderView | None,
    entry: AdjustmentTimelineEntry,
) -> BacktestOrderView | None:
    if position.status is BacktestPositionStatus.FLAT:
        return pending
    if position.entry_price is None:
        raise ValueError("open position requires an entry price")
    position.quantity = (position.quantity / entry.adjustment_factor).quantize(
        _QUANTITY, rounding=ROUND_DOWN
    )
    position.entry_price *= entry.adjustment_factor
    if pending is not None and pending.direction is BacktestOrderDirection.SELL:
        return pending.model_copy(update={"position_before": position.quantity})
    return pending


def _new_order(
    item_id: UUID,
    signal_date: date,
    zone: SignalZone,
    position: _Position,
    targets: TargetValues,
) -> BacktestOrderView | None:
    direction: BacktestOrderDirection | None = None
    if position.status is BacktestPositionStatus.FLAT and zone in {
        SignalZone.LOW,
        SignalZone.STRONG_LOW,
    }:
        direction = BacktestOrderDirection.BUY
    elif position.status is BacktestPositionStatus.HOLDING and zone in {
        SignalZone.HIGH,
        SignalZone.STRONG_HIGH,
    }:
        direction = BacktestOrderDirection.SELL
    if direction is None:
        return None
    return BacktestOrderView(
        id=uuid5(item_id, f"order:{signal_date.isoformat()}:{direction.value}"),
        item_id=item_id,
        signal_date=signal_date,
        execute_date=None,
        status=BacktestOrderStatus.PENDING,
        direction=direction,
        cash_before=position.cash,
        position_before=position.quantity,
        target_values=targets,
        target_zone=zone,
    )


def _fill_order(
    order: BacktestOrderView,
    bar: BacktestBar,
    bar_index: int,
    position: _Position,
) -> tuple[BacktestOrderView, BacktestTradeView]:
    cash_before = position.cash
    position_before = position.quantity
    holding_days: int | None = None
    realized_amount: Decimal | None = None
    realized_rate: Decimal | None = None
    round_trip_no = position.round_trip_no
    if order.direction is BacktestOrderDirection.BUY:
        quantity = (position.cash / bar.open_price).quantize(
            _QUANTITY, rounding=ROUND_DOWN
        )
        if quantity <= 0:
            raise ValueError("capital is insufficient for representable quantity")
        position.cash = _money(position.cash - quantity * bar.open_price)
        position.quantity = quantity
        position.entry_price = bar.open_price
        position.entry_index = bar_index
    else:
        quantity = position.quantity
        if position.entry_price is None or position.entry_index is None:
            raise ValueError("sell order requires an open position")
        entry_value = quantity * position.entry_price
        exit_value = quantity * bar.open_price
        realized_amount = _money(exit_value - entry_value)
        realized_rate = _ratio(realized_amount / entry_value)
        holding_days = bar_index - position.entry_index
        position.cash = _money(position.cash + exit_value)
        position.quantity = Decimal("0")
        position.entry_price = None
        position.entry_index = None
        position.round_trip_no += 1
    filled = order.model_copy(
        update={
            "status": BacktestOrderStatus.FILLED,
            "execute_date": bar.trade_date,
            "execution_price": bar.open_price,
            "quantity": quantity,
        }
    )
    trade = BacktestTradeView(
        id=uuid5(order.id, "trade"),
        item_id=order.item_id,
        order_id=order.id,
        execute_date=bar.trade_date,
        direction=order.direction,
        price=bar.open_price,
        quantity=quantity,
        cash_after=position.cash,
        position_after=position.quantity,
        target_values=order.target_values,
        target_zone=order.target_zone,
        round_trip_no=round_trip_no,
        holding_trade_days=holding_days,
        realized_return_amount=realized_amount,
        realized_return_rate=realized_rate,
    )
    assert cash_before == order.cash_before
    assert position_before == order.position_before
    return filled, trade


def _metrics(
    item_id: UUID,
    initial_capital: Decimal,
    daily: list[BacktestDailyResultView],
    trades: list[BacktestTradeView],
    orders: list[BacktestOrderView],
    position: _Position,
) -> BacktestMetricView:
    ending_equity = daily[-1].equity
    total_return = _ratio(ending_equity / initial_capital - 1)
    annualized = _annualized_return(
        ending_equity, initial_capital, trading_days=len(daily)
    )
    daily_returns = [
        float(daily[index].equity / daily[index - 1].equity - 1)
        for index in range(1, len(daily))
    ]
    volatility_value = pstdev(daily_returns) * math.sqrt(252) if daily_returns else 0
    volatility = _ratio(Decimal(str(volatility_value)))
    sharpe = None
    if daily_returns and volatility_value > 0:
        sharpe = _ratio(
            Decimal(str(fmean(daily_returns) / pstdev(daily_returns) * math.sqrt(252)))
        )
    sell_trades = [
        trade for trade in trades if trade.direction is BacktestOrderDirection.SELL
    ]
    returns = [trade.realized_return_rate for trade in sell_trades]
    realized_amount = sum(
        (trade.realized_return_amount or Decimal("0") for trade in sell_trades),
        Decimal("0"),
    )
    positive = sum(value > 0 for value in returns if value is not None)
    negative = sum(value < 0 for value in returns if value is not None)
    breakeven = len(sell_trades) - positive - negative
    holding_days = [trade.holding_trade_days or 0 for trade in sell_trades]
    return BacktestMetricView(
        item_id=item_id,
        ending_equity=ending_equity,
        total_return=total_return,
        realized_return=_ratio(realized_amount / initial_capital),
        annualized_return=annualized,
        max_drawdown=max(result.drawdown for result in daily),
        volatility=volatility,
        sharpe_ratio=sharpe,
        completed_round_trips=len(sell_trades),
        winning_trades=positive,
        losing_trades=negative,
        breakeven_trades=breakeven,
        win_rate=(
            _ratio(Decimal(positive) / Decimal(len(sell_trades)))
            if sell_trades
            else None
        ),
        average_trade_return=(
            _ratio(sum(returns, Decimal("0")) / Decimal(len(returns)))
            if returns
            else None
        ),
        maximum_trade_gain=max(returns) if returns else None,
        maximum_trade_loss=min(returns) if returns else None,
        average_holding_trade_days=(
            _ratio(Decimal(sum(holding_days)) / Decimal(len(holding_days)))
            if holding_days
            else None
        ),
        longest_holding_trade_days=max(holding_days, default=0),
        capital_exposure_ratio=_ratio(
            Decimal(
                sum(
                    result.position_status is BacktestPositionStatus.HOLDING
                    for result in daily
                )
            )
            / Decimal(len(daily))
        ),
        open_position_at_end=position.status is BacktestPositionStatus.HOLDING,
        unfilled_order_count=sum(
            order.status is BacktestOrderStatus.UNFILLED_AT_END for order in orders
        ),
    )


def _annualized_return(
    ending_equity: Decimal, initial_capital: Decimal, *, trading_days: int
) -> Decimal:
    if ending_equity == 0:
        return Decimal("-1.00000000")
    with localcontext() as context:
        context.prec = 80
        exponent = Decimal(252) / Decimal(trading_days)
        compounded = (ending_equity / initial_capital) ** exponent - 1
        return compounded.quantize(_RATIO, rounding=ROUND_HALF_UP)


def _money(value: Decimal) -> Decimal:
    return value.quantize(_CENT, rounding=ROUND_HALF_UP)


def _ratio(value: Decimal) -> Decimal:
    return value.quantize(_RATIO, rounding=ROUND_HALF_UP)
