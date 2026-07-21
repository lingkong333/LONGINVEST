from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Numeric, UniqueConstraint

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


def test_backtest_models_own_frozen_snapshot_and_adjustment_records() -> None:
    assert BacktestTask.__tablename__ == "backtest_task"
    assert BacktestItem.__tablename__ == "backtest_item"
    assert BacktestForecastSnapshot.__tablename__ == "backtest_forecast_snapshot"
    assert BacktestTargetAdjustment.__tablename__ == "backtest_target_adjustment"
    assert {
        "training_start_date",
        "training_end_date",
        "test_start_date",
        "test_end_date",
    } <= set(BacktestTask.__table__.c.keys())
    assert {
        "training_data_hash",
        "source_code_hash",
        "parameter_hash",
        "frozen_at",
    } <= set(BacktestForecastSnapshot.__table__.c.keys())
    assert {
        "event_date",
        "adjustment_factor",
        "before_low_strong",
        "after_high_strong",
    } <= set(BacktestTargetAdjustment.__table__.c.keys())
    assert any(
        isinstance(item, UniqueConstraint)
        for item in BacktestForecastSnapshot.__table__.constraints
    )
    assert any(
        isinstance(item, CheckConstraint) for item in BacktestTask.__table__.constraints
    )


def _constraint_names(model: type) -> set[str | None]:
    return {constraint.name for constraint in model.__table__.constraints}


def _foreign_key_targets(model: type) -> set[str]:
    return {
        element.target_fullname
        for constraint in model.__table__.constraints
        if isinstance(constraint, ForeignKeyConstraint)
        for element in constraint.elements
    }


def test_backtest_task_and_item_save_complete_replay_provenance() -> None:
    assert {
        "mode",
        "status",
        "universe_hash",
        "strategy_version_id",
        "draft_source_code",
        "parameter_snapshot",
        "strategy_api_version",
        "runner_image_digest",
        "hysteresis_ratio",
        "minimum_hysteresis",
    } <= set(BacktestTask.__table__.c.keys())
    assert {
        "training_data_fetched_at",
        "training_data_start_date",
        "training_data_end_date",
        "training_data_row_count",
        "training_data_hash",
        "training_price_basis",
        "test_data_fetched_at",
        "test_data_start_date",
        "test_data_end_date",
        "test_data_row_count",
        "test_data_hash",
        "test_price_basis",
    } <= set(BacktestItem.__table__.c.keys())
    assert "ck_backtest_task_mode_valid" in _constraint_names(BacktestTask)
    assert "ck_backtest_task_status_valid" in _constraint_names(BacktestTask)
    assert "ck_backtest_task_strategy_source_valid" in _constraint_names(BacktestTask)
    assert "ck_backtest_item_status_valid" in _constraint_names(BacktestItem)
    for column in BacktestTask.__table__.columns:
        assert not (column.default is not None and column.default.arg == "")


def test_backtest_forecast_and_result_models_have_complete_fields() -> None:
    assert {
        "training_start_date",
        "training_end_date",
        "training_row_count",
        "training_fetched_at",
        "training_data_hash",
        "source_code_hash",
        "parameter_hash",
        "diagnostics",
        "environment_version",
        "runner_image_digest",
        "price_basis",
        "frozen_at",
    } <= set(BacktestForecastSnapshot.__table__.c.keys())
    assert {
        "execute_date",
        "execution_price",
        "cash_before",
        "position_before",
        "target_low_strong",
        "target_zone",
    } <= set(BacktestOrder.__table__.c.keys())
    assert {
        "item_id",
        "execute_date",
        "direction",
        "round_trip_no",
        "holding_trade_days",
        "realized_return_amount",
        "realized_return_rate",
        "target_high_strong",
        "target_zone",
    } <= set(BacktestTrade.__table__.c.keys())
    assert {
        "ending_equity",
        "realized_return",
        "annualized_return",
        "volatility",
        "sharpe_ratio",
        "winning_trades",
        "losing_trades",
        "breakeven_trades",
        "win_rate",
        "average_trade_return",
        "maximum_trade_gain",
        "maximum_trade_loss",
        "average_holding_trade_days",
        "longest_holding_trade_days",
        "capital_exposure_ratio",
        "open_position_at_end",
        "unfilled_order_count",
    } <= set(BacktestMetric.__table__.c.keys())
    assert {
        "cash",
        "position_quantity",
        "close_price",
        "position_market_value",
        "target_low_strong",
        "zone",
        "position_status",
    } <= set(BacktestDailyResult.__table__.c.keys())


def test_backtest_models_enforce_constraints_and_references() -> None:
    models = (
        BacktestTask,
        BacktestUniverseSnapshot,
        BacktestItem,
        BacktestForecastSnapshot,
        BacktestTargetAdjustment,
        BacktestOrder,
        BacktestTrade,
        BacktestMetric,
        BacktestDailyResult,
    )
    all_names = set().union(*(_constraint_names(model) for model in models))
    assert {
        "ck_backtest_task_date_range_valid",
        "ck_backtest_task_hashes_sha256",
        "ck_backtest_task_initial_capital_positive",
        "ck_backtest_forecast_snapshot_training_range_valid",
        "ck_backtest_forecast_snapshot_targets_ordered",
        "ck_backtest_target_adjustment_factor_positive",
        "ck_backtest_order_status_valid",
        "ck_backtest_order_execution_consistent",
        "ck_backtest_order_quantity_positive",
        "ck_backtest_order_target_zone_valid",
        "ck_backtest_trade_values_valid",
        "ck_backtest_trade_target_zone_valid",
        "ck_backtest_metric_counts_nonnegative",
        "ck_backtest_daily_result_values_valid",
        "ck_backtest_daily_result_zone_valid",
    } <= all_names
    assert "backtest_task.id" in _foreign_key_targets(BacktestUniverseSnapshot)
    assert "backtest_item.id" in _foreign_key_targets(BacktestOrder)
    assert "backtest_order.id" in _foreign_key_targets(BacktestTrade)
    assert any(
        isinstance(constraint, UniqueConstraint)
        for constraint in BacktestMetric.__table__.constraints
    )


def test_every_critical_backtest_numeric_column_rejects_nonfinite_values() -> None:
    models = (
        BacktestTask,
        BacktestForecastSnapshot,
        BacktestTargetAdjustment,
        BacktestOrder,
        BacktestTrade,
        BacktestMetric,
        BacktestDailyResult,
    )
    for model in models:
        constraint_sql = " ".join(
            str(constraint.sqltext)
            for constraint in model.__table__.constraints
            if isinstance(constraint, CheckConstraint)
        )
        numeric_fields = (
            column.name
            for column in model.__table__.columns
            if isinstance(column.type, Numeric)
        )
        for field in numeric_fields:
            assert f"{field} <> 'NaN'::numeric" in constraint_sql
            assert f"{field} < 'Infinity'::numeric" in constraint_sql
            assert f"{field} > '-Infinity'::numeric" in constraint_sql


def test_metric_trade_counts_include_breakeven_trades() -> None:
    counts_constraint = next(
        constraint
        for constraint in BacktestMetric.__table__.constraints
        if constraint.name == "ck_backtest_metric_counts_nonnegative"
    )
    sql = str(counts_constraint.sqltext)
    assert "breakeven_trades >= 0" in sql
    assert (
        "winning_trades + losing_trades + breakeven_trades "
        "= completed_round_trips"
    ) in sql
