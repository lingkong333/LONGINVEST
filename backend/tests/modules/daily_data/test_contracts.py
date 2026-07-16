from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from long_invest.modules.daily_data.contracts import (
    CreateDailyBatch,
    DailyBarSnapshot,
    DailyBatchStatus,
    DailyMissingReason,
    DailyStageStatus,
    StageDailyBar,
)


def _snapshot(**changes) -> DailyBarSnapshot:
    values = {
        "security_id": uuid4(),
        "symbol": "600000.SH",
        "trade_date": date(2026, 7, 15),
        "close": Decimal("10.123456"),
        "data_version": 2,
        "source": "EASTMONEY",
        "updated_at": datetime(2026, 7, 15, 17, tzinfo=UTC),
    }
    values.update(changes)
    return DailyBarSnapshot(**values)


def test_daily_bar_snapshot_is_frozen_and_preserves_decimal_precision() -> None:
    snapshot = _snapshot(close=Decimal("10.123456789012345678"))

    assert snapshot.close == Decimal("10.123456789012345678")
    with pytest.raises(FrozenInstanceError):
        snapshot.close = Decimal("11")


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"symbol": "invalid"}, "股票代码"),
        ({"symbol": None}, "股票代码"),
        ({"close": Decimal("0")}, "收盘价"),
        ({"close": Decimal("-0.01")}, "收盘价"),
        ({"close": Decimal("NaN")}, "收盘价"),
        ({"close": "10.00"}, "收盘价"),
        ({"data_version": 0}, "数据版本"),
        ({"data_version": 1.0}, "数据版本"),
        ({"source": "   "}, "数据来源"),
        ({"source": None}, "数据来源"),
        ({"updated_at": datetime(2026, 7, 15, 17)}, "时区"),
    ],
)
def test_daily_bar_snapshot_rejects_invalid_values(changes, message) -> None:
    with pytest.raises(ValueError, match=message):
        _snapshot(**changes)


def test_daily_batch_has_seven_states() -> None:
    assert {item.value for item in DailyBatchStatus} == {
        "PENDING",
        "FETCHING",
        "VALIDATING",
        "COMMITTING",
        "SUCCEEDED",
        "PARTIAL",
        "FAILED",
    }


def test_daily_batch_requires_snapshot() -> None:
    with pytest.raises(ValueError, match="范围"):
        CreateDailyBatch(
            trading_date=date(2026, 7, 15),
            universe_snapshot_id=None,
            symbols=("600000.SH",),
            security_ids=(uuid4(),),
            idempotency_key="daily:2026-07-15",
        )


@pytest.mark.parametrize("value", ["", " ", "x" * 161])
def test_daily_batch_rejects_invalid_idempotency_key(value: str) -> None:
    with pytest.raises(ValueError, match="幂等键"):
        CreateDailyBatch(
            trading_date=date(2026, 7, 15),
            universe_snapshot_id=uuid4(),
            symbols=("600000.SH",),
            security_ids=(uuid4(),),
            idempotency_key=value,
        )


def test_daily_batch_rejects_empty_or_duplicate_scope() -> None:
    with pytest.raises(ValueError, match="范围"):
        CreateDailyBatch(
            trading_date=date(2026, 7, 15),
            universe_snapshot_id=uuid4(),
            symbols=(),
            security_ids=(),
            idempotency_key="daily:2026-07-15",
        )
    with pytest.raises(ValueError, match="重复"):
        CreateDailyBatch(
            trading_date=date(2026, 7, 15),
            universe_snapshot_id=uuid4(),
            symbols=("600000.SH", "600000.SH"),
            security_ids=(uuid4(), uuid4()),
            idempotency_key="daily:2026-07-15",
        )


def test_daily_batch_requires_one_unique_security_id_per_symbol() -> None:
    security_id = uuid4()
    common = {
        "trading_date": date(2026, 7, 15),
        "universe_snapshot_id": uuid4(),
        "idempotency_key": "daily:2026-07-15",
    }
    with pytest.raises(ValueError, match="绑定"):
        CreateDailyBatch(
            symbols=("600000.SH", "000001.SZ"),
            security_ids=(security_id,),
            **common,
        )
    with pytest.raises(ValueError):
        CreateDailyBatch(
            symbols=("600000.SH", "000001.SZ"),
            security_ids=(security_id, security_id),
            **common,
        )


def test_daily_batch_freezes_known_corporate_actions_inside_scope() -> None:
    command = CreateDailyBatch(
        trading_date=date(2026, 7, 15),
        universe_snapshot_id=uuid4(),
        symbols=("600000.SH", "000001.SZ"),
        security_ids=(uuid4(), uuid4()),
        idempotency_key="daily:2026-07-15",
        known_corporate_action_symbols=("600000.SH",),
    )

    assert command.known_corporate_action_symbols == ("600000.SH",)

    with pytest.raises(ValueError):
        CreateDailyBatch(
            trading_date=date(2026, 7, 15),
            universe_snapshot_id=uuid4(),
            symbols=("600000.SH",),
            security_ids=(uuid4(),),
            idempotency_key="daily:2026-07-15:invalid",
            known_corporate_action_symbols=("000001.SZ",),
        )


def test_stage_contract_validates_uuid_date_symbol_and_aware_time() -> None:
    item = StageDailyBar(
        symbol="600000.SH",
        security_id=uuid4(),
        trading_date=date(2026, 7, 15),
        status=DailyStageStatus.FETCHED,
        provider_payload={
            "open": "10.00",
            "high": "10.50",
            "low": "9.90",
            "close": "10.20",
            "volume": 100,
            "amount": "1020.00",
            "source": "EASTMONEY",
        },
        received_at=datetime(2026, 7, 15, 9, tzinfo=UTC),
    )
    assert isinstance(item.security_id, UUID)
    assert item.received_at.tzinfo is UTC

    with pytest.raises(ValueError, match="时区"):
        StageDailyBar(
            symbol="600000.SH",
            security_id=uuid4(),
            trading_date=date(2026, 7, 15),
            status=DailyStageStatus.MISSING,
            missing_reason=DailyMissingReason.UNEXPLAINED,
            received_at=datetime(2026, 7, 15, 9),
        )


def test_missing_stage_requires_reason_and_valid_stage_requires_payload() -> None:
    common = {
        "symbol": "600000.SH",
        "security_id": uuid4(),
        "trading_date": date(2026, 7, 15),
        "received_at": datetime(2026, 7, 15, 9, tzinfo=UTC),
    }
    with pytest.raises(ValueError, match="缺失原因"):
        StageDailyBar(status=DailyStageStatus.MISSING, **common)
    with pytest.raises(ValueError, match="日线数据"):
        StageDailyBar(status=DailyStageStatus.FETCHED, **common)


@pytest.mark.parametrize(
    "status",
    [
        DailyStageStatus.VALID,
        DailyStageStatus.REVIEW_REQUIRED,
        DailyStageStatus.INVALID,
    ],
)
def test_stage_contract_rejects_internal_quality_statuses(status) -> None:
    with pytest.raises(ValueError):
        StageDailyBar(
            symbol="600000.SH",
            security_id=uuid4(),
            trading_date=date(2026, 7, 15),
            status=status,
            provider_payload={"close": "10.20"},
            received_at=datetime(2026, 7, 15, 9, tzinfo=UTC),
        )
