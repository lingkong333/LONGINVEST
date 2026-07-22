from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.backtests.contracts import (
    BacktestAction,
    BacktestCreateRequest,
    BacktestDailyResultView,
    BacktestDateRange,
    BacktestErrorCode,
    BacktestForecastSnapshotView,
    BacktestItemStatus,
    BacktestMetricView,
    BacktestMode,
    BacktestOrderDirection,
    BacktestOrderStatus,
    BacktestOrderView,
    BacktestPositionStatus,
    BacktestSignalInput,
    BacktestSummaryView,
    BacktestTargetAdjustmentView,
    BacktestTaskDetailView,
    BacktestTaskSnapshot,
    BacktestTaskStatus,
    BacktestTestDataSnapshotView,
    BacktestTradeView,
    BacktestUniverseEntry,
)
from long_invest.modules.signals.contracts import SignalZone
from long_invest.modules.targets.contracts import TargetValues


def test_backtest_date_range_requires_non_overlapping_training_and_test_dates() -> None:
    date_range = BacktestDateRange(
        training_start_date=date(2024, 1, 1),
        training_end_date=date(2024, 12, 31),
        test_start_date=date(2025, 1, 1),
        test_end_date=date(2025, 12, 31),
    )
    assert date_range.test_start_date == date(2025, 1, 1)
    with pytest.raises(ValidationError):
        BacktestDateRange(
            training_start_date=date(2024, 1, 1),
            training_end_date=date(2024, 12, 31),
            test_start_date=date(2024, 12, 31),
            test_end_date=date(2025, 12, 31),
        )


def test_backtest_signal_input_is_strict_and_uses_decimal_targets() -> None:
    signal = BacktestSignalInput(
        security_id=uuid4(),
        trade_date=date(2025, 1, 2),
        close_price=Decimal("10.00"),
        targets=TargetValues(
            low_strong="8", low_watch="9", high_watch="11", high_strong="12"
        ),
    )
    assert signal.close_price == Decimal("10.00")
    with pytest.raises(ValidationError):
        BacktestSignalInput.model_validate(signal.model_dump() | {"extra": True})


def test_backtest_status_and_error_codes_are_stable() -> None:
    assert BacktestItemStatus.FROZEN.value == "FROZEN"
    assert BacktestErrorCode.ADJUSTMENT_DATA_UNAVAILABLE.value == (
        "ADJUSTMENT_DATA_UNAVAILABLE"
    )
    assert {status.value for status in BacktestTaskStatus} == {
        "PENDING",
        "RUNNING",
        "PAUSING",
        "PAUSED",
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
        "CANCELING",
        "CANCELED",
    }
    assert {action.value for action in BacktestAction} == {
        "PAUSE",
        "RESUME",
        "CANCEL",
        "RETRY_FAILED",
        "RERUN",
    }


def test_backtest_summary_counts_are_server_validated() -> None:
    summary = BacktestSummaryView(
        task_id=uuid4(),
        status=BacktestTaskStatus.FAILED,
        total_items=1,
        completed_items=1,
        succeeded_items=0,
        failed_items=1,
        canceled_items=0,
        pending_items=0,
        failure_codes={"TEST_DATA_INVALID": 1},
        allowed_actions=(BacktestAction.RETRY_FAILED, BacktestAction.RERUN),
    )
    assert summary.failure_codes == {"TEST_DATA_INVALID": 1}

    with pytest.raises(ValidationError):
        BacktestSummaryView(
            task_id=uuid4(),
            status=BacktestTaskStatus.RUNNING,
            total_items=1,
            completed_items=1,
            succeeded_items=0,
            failed_items=0,
            canceled_items=0,
            pending_items=1,
            failure_codes={},
            allowed_actions=(BacktestAction.CANCEL,),
        )


def test_backtest_contracts_expose_frozen_replay_snapshots() -> None:
    snapshot = BacktestTaskSnapshot(
        id=uuid4(),
        mode=BacktestMode.SINGLE,
        universe_snapshot=(
            BacktestUniverseEntry(
                security_id=uuid4(), symbol="600000.SH", name="浦发银行"
            ),
        ),
        universe_hash="f" * 64,
        date_range=BacktestDateRange(
            training_start_date=date(2024, 1, 1),
            training_end_date=date(2024, 12, 31),
            test_start_date=date(2025, 1, 1),
            test_end_date=date(2025, 12, 31),
        ),
        strategy_version_id=uuid4(),
        draft_id=None,
        draft_version=None,
        draft_source_code=None,
        source_code_hash="a" * 64,
        strategy_metadata={},
        parameter_schema={},
        parameter_snapshot={"window": 20, "nested": {"values": (1, 2)}},
        parameter_hash="b" * 64,
        environment_version="runner-1",
        runner_image_digest="sha256:" + "d" * 64,
        strategy_api_version="1.0",
        rule_version="rules-1",
        hysteresis_ratio=Decimal("0.01"),
        minimum_hysteresis=Decimal("0.02"),
        initial_capital=Decimal("100000"),
        price_basis="UNADJUSTED",
        data_source="provider",
    )
    assert snapshot.price_basis == "UNADJUSTED"
    assert snapshot.model_dump(mode="json")["parameter_snapshot"]["nested"] == {
        "values": [1, 2]
    }
    assert BacktestDailyResultView.__name__ == "BacktestDailyResultView"
    assert SignalZone.UNKNOWN.value == "UNKNOWN"


def test_backtest_modes_are_exact_and_single_scope_contains_one_security() -> None:
    assert {mode.value for mode in BacktestMode} == {"SINGLE", "WATCHLIST", "MARKET"}
    values = {
        "id": uuid4(),
        "mode": BacktestMode.SINGLE,
        "universe_snapshot": (
            BacktestUniverseEntry(
                security_id=uuid4(), symbol="600000.SH", name="浦发银行"
            ),
            BacktestUniverseEntry(
                security_id=uuid4(), symbol="000001.SZ", name="平安银行"
            ),
        ),
        "universe_hash": "f" * 64,
        "date_range": BacktestDateRange(
            training_start_date=date(2024, 1, 1),
            training_end_date=date(2024, 12, 31),
            test_start_date=date(2025, 1, 1),
            test_end_date=date(2025, 12, 31),
        ),
        "strategy_version_id": uuid4(),
        "draft_id": None,
        "draft_version": None,
        "draft_source_code": None,
        "source_code_hash": "a" * 64,
        "strategy_metadata": {},
        "parameter_schema": {},
        "parameter_snapshot": {},
        "parameter_hash": "b" * 64,
        "environment_version": "runner-1",
        "runner_image_digest": "sha256:" + "d" * 64,
        "strategy_api_version": "1.0",
        "rule_version": "rules-1",
        "hysteresis_ratio": Decimal("0.01"),
        "minimum_hysteresis": Decimal("0.01"),
        "initial_capital": Decimal("100000"),
        "price_basis": "QFQ_AS_OF",
        "data_source": "EASTMONEY",
    }
    with pytest.raises(ValidationError):
        BacktestTaskSnapshot(**values)


def test_backtest_create_scope_requires_the_matching_selector() -> None:
    common = {
        "date_range": BacktestDateRange(
            training_start_date=date(2024, 1, 1),
            training_end_date=date(2024, 12, 31),
            test_start_date=date(2025, 1, 1),
            test_end_date=date(2025, 12, 31),
        ),
        "strategy_version_id": uuid4(),
        "parameter_snapshot": {},
        "initial_capital": "100000",
    }
    watchlist = BacktestCreateRequest(
        mode=BacktestMode.WATCHLIST,
        watchlist_id=uuid4(),
        **common,
    )
    market = BacktestCreateRequest(mode=BacktestMode.MARKET, **common)

    assert watchlist.symbol is None
    assert market.watchlist_id is None
    with pytest.raises(ValidationError):
        BacktestCreateRequest(mode=BacktestMode.MARKET, symbol="600000.SH", **common)


def test_backtest_task_requires_published_version_or_frozen_draft_source() -> None:
    fields = BacktestTaskSnapshot.model_fields
    assert fields["strategy_version_id"].is_required()
    assert fields["draft_source_code"].is_required()
    assert fields["parameter_snapshot"].is_required()


def test_forecast_snapshot_freezes_training_provenance_and_diagnostics() -> None:
    frozen_at = datetime(2026, 7, 21, 10, tzinfo=UTC)
    snapshot = BacktestForecastSnapshotView(
        item_id=uuid4(),
        training_start_date=date(2024, 1, 1),
        training_end_date=date(2024, 12, 31),
        training_row_count=240,
        training_fetched_at=frozen_at,
        training_data_hash="a" * 64,
        source_code_hash="b" * 64,
        parameter_hash="c" * 64,
        values=TargetValues(
            low_strong="8", low_watch="9", high_watch="11", high_strong="12"
        ),
        diagnostics={"sample": {"windows": (20, 60)}},
        environment_version="runner-1",
        runner_image_digest="sha256:" + "d" * 64,
        price_basis="QFQ_AS_OF",
        frozen_at=frozen_at,
    )
    assert snapshot.model_dump(mode="json")["diagnostics"] == {
        "sample": {"windows": [20, 60]}
    }
    with pytest.raises(ValidationError):
        BacktestForecastSnapshotView.model_validate(
            snapshot.model_dump() | {"training_fetched_at": datetime(2026, 7, 21)}
        )


def test_task_detail_aggregates_matching_training_and_test_snapshots() -> None:
    item_id = uuid4()
    fetched_at = datetime(2026, 7, 21, 9, tzinfo=UTC)
    task = BacktestTaskSnapshot(
        id=uuid4(),
        mode=BacktestMode.SINGLE,
        universe_snapshot=(
            BacktestUniverseEntry(
                security_id=uuid4(), symbol="600000.SH", name="浦发银行"
            ),
        ),
        universe_hash="f" * 64,
        date_range=BacktestDateRange(
            training_start_date=date(2024, 1, 1),
            training_end_date=date(2024, 12, 31),
            test_start_date=date(2025, 1, 1),
            test_end_date=date(2025, 12, 31),
        ),
        strategy_version_id=uuid4(),
        draft_id=None,
        draft_version=None,
        draft_source_code=None,
        source_code_hash="a" * 64,
        strategy_metadata={},
        parameter_schema={},
        parameter_snapshot={},
        parameter_hash="b" * 64,
        environment_version="runner-1",
        runner_image_digest="sha256:" + "d" * 64,
        strategy_api_version="1.0",
        rule_version="rules-1",
        hysteresis_ratio=Decimal("0.01"),
        minimum_hysteresis=Decimal("0.01"),
        initial_capital=Decimal("100000"),
        price_basis="QFQ_AS_OF",
        data_source="EASTMONEY",
    )
    training = BacktestForecastSnapshotView(
        item_id=item_id,
        training_start_date=date(2024, 1, 1),
        training_end_date=date(2024, 12, 31),
        training_row_count=240,
        training_fetched_at=fetched_at,
        training_data_hash="a" * 64,
        source_code_hash="b" * 64,
        parameter_hash="c" * 64,
        values=TargetValues(
            low_strong="8", low_watch="9", high_watch="11", high_strong="12"
        ),
        diagnostics={},
        environment_version="runner-1",
        runner_image_digest="sha256:" + "d" * 64,
        price_basis="QFQ_AS_OF",
        frozen_at=fetched_at,
    )
    test_data = BacktestTestDataSnapshotView(
        item_id=item_id,
        fetched_at=fetched_at,
        start_date=date(2025, 1, 1),
        end_date=date(2025, 12, 31),
        row_count=240,
        data_hash="e" * 64,
        price_basis="QFQ_AS_OF",
    )
    detail = BacktestTaskDetailView(
        task_snapshot=task,
        training_snapshots=(training,),
        test_snapshots=(test_data,),
    )

    assert detail.training_snapshots[0].item_id == detail.test_snapshots[0].item_id
    with pytest.raises(ValidationError):
        BacktestTaskDetailView(
            task_snapshot=task,
            training_snapshots=(training,),
            test_snapshots=(
                BacktestTestDataSnapshotView(
                    **(test_data.model_dump() | {"item_id": uuid4()})
                ),
            ),
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"fetched_at": datetime(2026, 7, 21, 9)},
        {"start_date": date(2025, 2, 1), "end_date": date(2025, 1, 1)},
        {"row_count": 0},
        {"data_hash": "not-a-hash"},
        {"price_basis": ""},
    ],
)
def test_test_data_snapshot_is_strict(overrides: dict[str, object]) -> None:
    values = {
        "item_id": uuid4(),
        "fetched_at": datetime(2026, 7, 21, 9, tzinfo=UTC),
        "start_date": date(2025, 1, 1),
        "end_date": date(2025, 12, 31),
        "row_count": 240,
        "data_hash": "e" * 64,
        "price_basis": "QFQ_AS_OF",
    }
    with pytest.raises(ValidationError):
        BacktestTestDataSnapshotView(**(values | overrides))


def test_target_adjustment_rejects_publication_after_effective_time() -> None:
    with pytest.raises(ValidationError):
        BacktestTargetAdjustmentView(
            item_id=uuid4(),
            event_date=date(2025, 6, 1),
            before_values=TargetValues(
                low_strong="8", low_watch="9", high_watch="11", high_strong="12"
            ),
            after_values=TargetValues(
                low_strong="4", low_watch="4.5", high_watch="5.5", high_strong="6"
            ),
            adjustment_factor=Decimal("0.5"),
            source="EASTMONEY",
            data_hash="a" * 64,
            published_at=datetime(2025, 6, 2, tzinfo=UTC),
            effective_at=datetime(2025, 6, 1, tzinfo=UTC),
        )


def test_unfilled_order_allows_no_date_and_filled_order_requires_d_plus_one() -> None:
    targets = TargetValues(
        low_strong="8", low_watch="9", high_watch="11", high_strong="12"
    )
    order = BacktestOrderView(
        id=uuid4(),
        item_id=uuid4(),
        signal_date=date(2025, 1, 2),
        execute_date=None,
        status=BacktestOrderStatus.UNFILLED_AT_END,
        direction=BacktestOrderDirection.BUY,
        execution_price=None,
        quantity=None,
        cash_before=Decimal("1000"),
        position_before=Decimal("0"),
        target_values=targets,
        target_zone=SignalZone.LOW,
    )
    assert order.execute_date is None
    with pytest.raises(ValidationError):
        BacktestOrderView.model_validate(
            order.model_dump()
            | {
                "status": BacktestOrderStatus.FILLED,
                "execute_date": order.signal_date,
                "execution_price": Decimal("10"),
                "quantity": Decimal("100"),
            }
        )


def test_trade_metric_and_daily_result_expose_complete_replay_values() -> None:
    targets = TargetValues(
        low_strong="8", low_watch="9", high_watch="11", high_strong="12"
    )
    trade = BacktestTradeView(
        id=uuid4(),
        item_id=uuid4(),
        order_id=uuid4(),
        execute_date=date(2025, 1, 3),
        direction=BacktestOrderDirection.SELL,
        price=Decimal("12"),
        quantity=Decimal("100"),
        cash_after=Decimal("1200"),
        position_after=Decimal("0"),
        target_values=targets,
        target_zone=SignalZone.HIGH,
        round_trip_no=1,
        holding_trade_days=5,
        realized_return_amount=Decimal("200"),
        realized_return_rate=Decimal("0.2"),
    )
    metric = BacktestMetricView(
        item_id=trade.item_id,
        ending_equity=Decimal("1200"),
        total_return=Decimal("0.2"),
        realized_return=Decimal("0.2"),
        annualized_return=Decimal("0.25"),
        max_drawdown=Decimal("0.1"),
        volatility=Decimal("0.15"),
        sharpe_ratio=Decimal("1.5"),
        completed_round_trips=1,
        winning_trades=1,
        losing_trades=0,
        breakeven_trades=0,
        win_rate=Decimal("1"),
        average_trade_return=Decimal("0.2"),
        maximum_trade_gain=Decimal("0.2"),
        maximum_trade_loss=Decimal("0"),
        average_holding_trade_days=Decimal("5"),
        longest_holding_trade_days=5,
        capital_exposure_ratio=Decimal("0.5"),
        open_position_at_end=False,
        unfilled_order_count=0,
    )
    daily = BacktestDailyResultView(
        item_id=trade.item_id,
        trade_date=date(2025, 1, 3),
        cash=Decimal("1200"),
        position_quantity=Decimal("0"),
        close_price=Decimal("12"),
        position_market_value=Decimal("0"),
        equity=Decimal("1200"),
        drawdown=Decimal("0"),
        target_values=targets,
        zone=SignalZone.HIGH,
        position_status=BacktestPositionStatus.FLAT,
    )
    assert metric.winning_trades == 1
    assert trade.holding_trade_days == 5
    assert daily.cash + daily.position_market_value == daily.equity


def test_zero_return_round_trip_is_a_valid_breakeven_trade() -> None:
    metric = BacktestMetricView(
        item_id=uuid4(),
        ending_equity=Decimal("100000"),
        total_return=Decimal("0"),
        realized_return=Decimal("0"),
        annualized_return=Decimal("0"),
        max_drawdown=Decimal("0"),
        volatility=Decimal("0"),
        sharpe_ratio=None,
        completed_round_trips=1,
        winning_trades=0,
        losing_trades=0,
        breakeven_trades=1,
        win_rate=Decimal("0"),
        average_trade_return=Decimal("0"),
        maximum_trade_gain=Decimal("0"),
        maximum_trade_loss=Decimal("0"),
        average_holding_trade_days=Decimal("1"),
        longest_holding_trade_days=1,
        capital_exposure_ratio=Decimal("0.5"),
        open_position_at_end=False,
        unfilled_order_count=0,
    )

    assert metric.breakeven_trades == 1
