from sqlalchemy import CheckConstraint, UniqueConstraint

from long_invest.modules.backtests.models import (
    BacktestForecastSnapshot,
    BacktestItem,
    BacktestTargetAdjustment,
    BacktestTask,
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
