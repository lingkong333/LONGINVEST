from datetime import UTC, datetime
from uuid import uuid4

import pytest

from long_invest.modules.quotes.contracts import (
    CreateQuoteCycle,
    QuoteCycleStatus,
    QuoteItemStatus,
)

NOW = datetime(2026, 7, 15, 1, 30, tzinfo=UTC)


def command(**overrides: object) -> CreateQuoteCycle:
    values = {
        "symbols": ("600000.SH",),
        "scheduled_at": NOW,
        "timeout_seconds": 30,
        "idempotency_scope": "manual:user-1",
        "idempotency_key": "request-1",
        "universe_snapshot_id": "snapshot-1",
        "universe_snapshot_version": 1,
    }
    values.update(overrides)
    return CreateQuoteCycle(**values)  # type: ignore[arg-type]


def test_cycle_and_item_statuses_are_fixed() -> None:
    assert {status.value for status in QuoteCycleStatus} == {
        "PENDING",
        "FETCHING",
        "FINALIZING",
        "READY",
        "PARTIAL",
        "FAILED",
        "MISSED",
        "CANCELED",
    }
    assert {status.value for status in QuoteItemStatus} == {
        "VALID",
        "MISSING",
        "STALE",
        "CONFLICT",
        "INVALID",
        "TIMEOUT",
        "PROVIDER_FAILED",
        "NOT_EXPECTED_TO_TRADE",
    }


@pytest.mark.parametrize("timeout", [9, 61])
def test_create_cycle_requires_supported_deadline(timeout: int) -> None:
    with pytest.raises(ValueError, match="10.*60"):
        command(timeout_seconds=timeout)


def test_create_cycle_rejects_empty_or_oversized_scope() -> None:
    with pytest.raises(ValueError, match="empty"):
        command(symbols=())
    with pytest.raises(ValueError, match="200"):
        command(symbols=tuple(f"{index:06d}.SH" for index in range(201)))


def test_create_cycle_rejects_duplicate_symbols_and_naive_time() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        command(symbols=("600000.SH", "600000.SH"))
    with pytest.raises(ValueError, match="timezone"):
        command(scheduled_at=datetime(2026, 7, 15, 1, 30))


def test_create_cycle_tracks_optional_schedule_and_subscription_versions() -> None:
    occurrence_id = uuid4()
    value = command(
        schedule_occurrence_id=occurrence_id,
        subscription_snapshot_version=9,
    )
    assert value.schedule_occurrence_id == occurrence_id
    assert value.subscription_snapshot_version == 9
    with pytest.raises(ValueError, match="subscription"):
        command(subscription_snapshot_version=0)
