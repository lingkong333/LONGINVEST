import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import wraps
from uuid import NAMESPACE_DNS, uuid4, uuid5

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


def _command(
    *,
    key="daily-1",
    parent=None,
    symbols=("600000.SH",),
    trading_date=DAY,
    snapshot_id=None,
):
    return CreateDailyBatch(
        trading_date=trading_date,
        universe_snapshot_id=snapshot_id or SNAPSHOT_ID,
        parent_batch_id=parent,
        symbols=symbols,
        security_ids=tuple(uuid5(NAMESPACE_DNS, symbol) for symbol in symbols),
        idempotency_key=key,
    )


def _batch(
    *,
    key="daily-1",
    parent=None,
    symbols=("600000.SH",),
    trading_date=DAY,
    snapshot_id=None,
    status="PENDING",
):
    return DailyDataBatch(
        id=uuid4(),
        trading_date=trading_date,
        universe_snapshot_id=snapshot_id or SNAPSHOT_ID,
        parent_batch_id=parent,
        symbols=list(symbols),
        security_ids=[str(uuid5(NAMESPACE_DNS, symbol)) for symbol in symbols],
        idempotency_key=key,
        status=status,
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
        self.scalar_statements = []

    async def scalar(self, statement):
        assert statement is not None
        self.scalar_statements.append(statement)
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
async def test_claim_rejects_same_symbols_with_different_security_binding() -> None:
    existing = _batch(symbols=("600000.SH",))
    repository = DailyDataRepository(FakeSession([existing]))
    command = _command(symbols=("600000.SH",))
    command = CreateDailyBatch(
        trading_date=command.trading_date,
        universe_snapshot_id=command.universe_snapshot_id,
        symbols=command.symbols,
        security_ids=(uuid4(),),
        idempotency_key=command.idempotency_key,
    )

    with pytest.raises(AppError) as captured:
        await repository.claim_batch(command, NOW)

    assert captured.value.code == "DAILY_BATCH_IDEMPOTENCY_CONFLICT"


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
@pytest.mark.parametrize("status", ["PARTIAL", "FAILED"])
async def test_retry_batch_with_parent_can_claim_same_date_and_snapshot(
    status,
) -> None:
    parent = _batch(
        symbols=("600000.SH", "000001.SZ"),
        status=status,
    )
    session = FakeSession([None, parent])
    repository = DailyDataRepository(session)

    claimed, created = await repository.claim_batch(
        _command(
            key="retry-1",
            parent=parent.id,
            symbols=("000001.SZ",),
        ),
        NOW,
    )

    assert created is True
    assert claimed.parent_batch_id == parent.id
    parent_statement = session.scalar_statements[1]
    assert "FOR UPDATE" in str(
        parent_statement.compile(compile_kwargs={"literal_binds": True})
    )
    assert parent_statement.get_execution_options()["populate_existing"] is True
    assert session.begin_nested_calls == 1


@async_test
async def test_retry_rejects_missing_parent_before_insert() -> None:
    session = FakeSession([None, None])
    repository = DailyDataRepository(session)

    with pytest.raises(AppError) as captured:
        await repository.claim_batch(
            _command(parent=uuid4()),
            NOW,
        )

    assert captured.value.code == "DAILY_PARENT_BATCH_NOT_FOUND"
    assert captured.value.status_code == 404
    assert session.added == []


@async_test
async def test_retry_rejects_different_parent_trading_date() -> None:
    parent = _batch(trading_date=date(2026, 7, 14), status="PARTIAL")
    repository = DailyDataRepository(FakeSession([None, parent]))

    with pytest.raises(AppError) as captured:
        await repository.claim_batch(
            _command(parent=parent.id),
            NOW,
        )

    assert captured.value.code == "DAILY_RETRY_SCOPE_CONFLICT"
    assert captured.value.status_code == 409


@async_test
async def test_retry_rejects_different_parent_snapshot() -> None:
    parent = _batch(snapshot_id=uuid4(), status="PARTIAL")
    repository = DailyDataRepository(FakeSession([None, parent]))

    with pytest.raises(AppError) as captured:
        await repository.claim_batch(
            _command(parent=parent.id),
            NOW,
        )

    assert captured.value.code == "DAILY_RETRY_SCOPE_CONFLICT"
    assert captured.value.status_code == 409


@async_test
async def test_retry_cannot_add_symbol_outside_parent_scope() -> None:
    parent = _batch(symbols=("600000.SH",), status="PARTIAL")
    session = FakeSession([None, parent])
    repository = DailyDataRepository(session)

    with pytest.raises(AppError) as captured:
        await repository.claim_batch(
            _command(
                parent=parent.id,
                symbols=("600000.SH", "000001.SZ"),
            ),
            NOW,
        )

    assert captured.value.code == "DAILY_RETRY_SCOPE_CONFLICT"
    assert captured.value.status_code == 409
    assert session.added == []


@async_test
async def test_retry_cannot_change_parent_symbol_security_binding() -> None:
    parent = _batch(symbols=("600000.SH",), status="FAILED")
    repository = DailyDataRepository(FakeSession([None, parent]))
    command = _command(parent=parent.id, symbols=("600000.SH",))
    command = CreateDailyBatch(
        trading_date=command.trading_date,
        universe_snapshot_id=command.universe_snapshot_id,
        parent_batch_id=command.parent_batch_id,
        symbols=command.symbols,
        security_ids=(uuid4(),),
        idempotency_key=command.idempotency_key,
    )

    with pytest.raises(AppError) as captured:
        await repository.claim_batch(command, NOW)

    assert captured.value.code == "DAILY_RETRY_SCOPE_CONFLICT"


@async_test
@pytest.mark.parametrize("status", ["PENDING", "FETCHING", "SUCCEEDED"])
async def test_retry_rejects_parent_outside_retryable_terminal_states(status) -> None:
    parent = _batch(status=status)
    repository = DailyDataRepository(FakeSession([None, parent]))

    with pytest.raises(AppError) as captured:
        await repository.claim_batch(
            _command(parent=parent.id),
            NOW,
        )

    assert captured.value.code == "DAILY_PARENT_BATCH_STATE_CONFLICT"
    assert captured.value.status_code == 409


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


@async_test
async def test_batch_lock_refreshes_cached_state() -> None:
    batch = _batch(status="VALIDATING")
    session = FakeSession([batch])
    repository = DailyDataRepository(session)

    loaded = await repository.get_batch(batch.id, for_update=True)

    assert loaded is batch
    statement = session.scalar_statements[0]
    assert "FOR UPDATE" in str(
        statement.compile(compile_kwargs={"literal_binds": True})
    )
    assert statement.get_execution_options()["populate_existing"] is True


@async_test
async def test_current_bar_and_latest_revision_reads_request_row_locks() -> None:
    session = FakeSession([None, 7])
    repository = DailyDataRepository(session)
    security_id = uuid4()

    assert await repository.get_bar(security_id, DAY) is None
    assert await repository.next_revision_no(security_id, DAY) == 8

    for statement in session.scalar_statements:
        compiled = str(statement.compile(compile_kwargs={"literal_binds": True}))
        assert "FOR UPDATE" in compiled
        assert statement.get_execution_options()["populate_existing"] is True


@async_test
async def test_bar_key_advisory_lock_is_stable_and_transaction_scoped() -> None:
    session = FakeSession([None, None])
    repository = DailyDataRepository(session)
    security_id = uuid4()

    await repository.lock_bar_key(security_id, DAY)
    await repository.lock_bar_key(security_id, DAY)

    first, second = session.scalar_statements
    first_sql = str(first.compile(compile_kwargs={"literal_binds": True}))
    second_sql = str(second.compile(compile_kwargs={"literal_binds": True}))
    assert "pg_advisory_xact_lock" in first_sql
    assert first_sql == second_sql


@async_test
async def test_previous_close_reads_latest_formal_fact_before_target_date() -> None:
    session = FakeSession([Decimal("9.80")])
    repository = DailyDataRepository(session)
    security_id = uuid4()

    assert await repository.get_previous_close(security_id, DAY) == Decimal("9.80")
    sql = str(
        session.scalar_statements[0].compile(compile_kwargs={"literal_binds": True})
    )
    assert "trade_date <" in sql
    assert "ORDER BY daily_bar_unadjusted.trade_date DESC" in sql
    assert "LIMIT 1" in sql
