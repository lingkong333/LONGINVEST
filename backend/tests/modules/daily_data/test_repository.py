import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from functools import wraps
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError

from long_invest.modules.daily_data.contracts import CreateDailyBatch
from long_invest.modules.daily_data.models import DailyDataBatch
from long_invest.modules.daily_data.repository import DailyDataRepository
from long_invest.platform.errors import AppError

NOW = datetime(2026, 7, 15, 17, tzinfo=UTC)
DAY = date(2026, 7, 15)


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


def _command(*, key="daily-1", parent=None, symbols=("600000.SH",)):
    return CreateDailyBatch(
        trading_date=DAY,
        universe_snapshot_id=SNAPSHOT_ID,
        parent_batch_id=parent,
        symbols=symbols,
        idempotency_key=key,
    )


def _batch(*, key="daily-1", parent=None, symbols=("600000.SH",)):
    return DailyDataBatch(
        id=uuid4(),
        trading_date=DAY,
        universe_snapshot_id=SNAPSHOT_ID,
        parent_batch_id=parent,
        symbols=list(symbols),
        idempotency_key=key,
        status="PENDING",
        expected_count=len(symbols),
        fetched_count=0,
        validated_count=0,
        committed_count=0,
        missing_count=0,
        failed_count=0,
        created_at=NOW,
    )


class FakeSession:
    def __init__(self, scalar_results=(), *, flush_error=None) -> None:
        self.scalar_results = list(scalar_results)
        self.flush_error = flush_error
        self.added = []
        self.begin_nested_calls = 0

    async def scalar(self, statement):
        assert statement is not None
        return self.scalar_results.pop(0) if self.scalar_results else None

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        if self.flush_error is not None:
            raise self.flush_error

    @asynccontextmanager
    async def begin_nested(self):
        self.begin_nested_calls += 1
        yield


SNAPSHOT_ID = uuid4()


@async_test
async def test_claim_rejects_idempotency_key_with_different_content() -> None:
    existing = _batch(symbols=("600000.SH",))
    repository = DailyDataRepository(FakeSession([existing]))

    with pytest.raises(AppError) as captured:
        await repository.claim_batch(
            _command(symbols=("000001.SZ",)),
            NOW,
        )

    assert captured.value.code == "DAILY_BATCH_IDEMPOTENCY_CONFLICT"
    assert captured.value.status_code == 409


@async_test
async def test_claim_rejects_automatic_scope_collision_with_different_content() -> None:
    existing = _batch(key="other-key", symbols=("600000.SH",))
    integrity_error = IntegrityError("insert", {}, RuntimeError("unique"))
    session = FakeSession([None, None, existing], flush_error=integrity_error)
    repository = DailyDataRepository(session)

    with pytest.raises(AppError) as captured:
        await repository.claim_batch(
            _command(key="new-key", symbols=("000001.SZ",)),
            NOW,
        )

    assert captured.value.code == "DAILY_BATCH_SCOPE_CONFLICT"
    assert captured.value.status_code == 409


@async_test
async def test_retry_batch_with_parent_can_claim_same_date_and_snapshot() -> None:
    parent_id = uuid4()
    session = FakeSession([None])
    repository = DailyDataRepository(session)

    claimed, created = await repository.claim_batch(
        _command(key="retry-1", parent=parent_id),
        NOW,
    )

    assert created is True
    assert claimed.parent_batch_id == parent_id
    assert session.begin_nested_calls == 1


@async_test
async def test_same_automatic_scope_and_content_replays_existing_batch() -> None:
    existing = _batch(key="original-key")
    integrity_error = IntegrityError("insert", {}, RuntimeError("unique"))
    session = FakeSession([None, None, existing], flush_error=integrity_error)
    repository = DailyDataRepository(session)

    claimed, created = await repository.claim_batch(
        _command(key="second-key"),
        NOW,
    )

    assert claimed is existing
    assert created is False


@async_test
async def test_repository_item_path_uses_database_savepoint() -> None:
    session = FakeSession()
    repository = DailyDataRepository(session)

    async with repository.item_savepoint():
        pass

    assert session.begin_nested_calls == 1
