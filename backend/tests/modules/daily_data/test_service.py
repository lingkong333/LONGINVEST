import asyncio
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import wraps
from types import SimpleNamespace
from uuid import NAMESPACE_DNS, uuid4, uuid5

import pytest

from long_invest.modules.daily_data.contracts import (
    CreateDailyBatch,
    DailyBatchStatus,
    DailyMissingReason,
    DailyStageStatus,
    StageDailyBar,
)
from long_invest.modules.daily_data.service import DailyDataService

NOW = datetime(2026, 7, 15, 17, tzinfo=UTC)
DAY = date(2026, 7, 15)


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class FakeRepository:
    def __init__(self) -> None:
        self.batches = {}
        self.by_key = {}
        self.stages = {}
        self.bars = {}
        self.revisions = []
        self.missing = {}
        self.fail_symbols = set()
        self.batch_lock_requests = []
        self.bar_write_order = []
        self.previous_closes = {}

    async def claim_batch(self, command, now):
        existing = self.by_key.get(command.idempotency_key)
        if existing:
            return existing, False
        from long_invest.modules.daily_data.models import DailyDataBatch

        batch = DailyDataBatch(
            id=uuid4(),
            trading_date=command.trading_date,
            universe_snapshot_id=command.universe_snapshot_id,
            parent_batch_id=command.parent_batch_id,
            symbols=list(command.symbols),
            security_ids=[str(value) for value in command.security_ids],
            idempotency_key=command.idempotency_key,
            status=DailyBatchStatus.PENDING,
            expected_count=len(command.symbols),
            fetched_count=0,
            validated_count=0,
            committed_count=0,
            missing_count=0,
            failed_count=0,
            created_at=now,
            deadline_at=command.deadline_at,
        )
        self.batches[batch.id] = batch
        self.by_key[command.idempotency_key] = batch
        return batch, True

    async def get_batch(self, batch_id, *, for_update=False):
        self.batch_lock_requests.append((batch_id, for_update))
        return self.batches.get(batch_id)

    async def upsert_stage(self, batch_id, item, expires_at):
        from long_invest.modules.daily_data.models import DailyBarStage

        stage = DailyBarStage(
            id=uuid4(),
            batch_id=batch_id,
            security_id=item.security_id,
            symbol=item.symbol,
            trading_date=item.trading_date,
            status=item.status,
            provider_payload=dict(item.provider_payload or {}),
            missing_reason=item.missing_reason,
            error_code=item.error_code,
            quality_code=item.quality_code,
            received_at=item.received_at,
            expires_at=expires_at,
        )
        self.stages[(batch_id, item.symbol)] = stage
        return stage

    async def list_stages(self, batch_id):
        return [v for (bid, _), v in self.stages.items() if bid == batch_id]

    async def replace_missing(self, batch_id, items):
        self.missing[batch_id] = list(items)

    async def list_all_missing(self, batch_id):
        return list(self.missing.get(batch_id, ()))

    async def get_bar(self, security_id, trade_date):
        self.bar_write_order.append(("get", security_id, trade_date))
        return self.bars.get((security_id, trade_date))

    async def lock_bar_key(self, security_id, trade_date):
        self.bar_write_order.append(("lock", security_id, trade_date))

    async def get_previous_close(self, security_id, trading_date):
        return self.previous_closes.get((security_id, trading_date))

    async def add_bar(self, bar):
        if bar.symbol in self.fail_symbols:
            raise RuntimeError("partition unavailable")
        self.bars[(bar.security_id, bar.trade_date)] = bar

    async def next_revision_no(self, security_id, trade_date):
        return 1 + sum(
            r.daily_bar_security_id == security_id
            and r.daily_bar_trade_date == trade_date
            for r in self.revisions
        )

    async def add_revision(self, revision):
        self.revisions.append(revision)

    @asynccontextmanager
    async def item_savepoint(self):
        yield

    async def flush(self):
        return None


class RecordingEvents:
    def __init__(self, fail=False) -> None:
        self.items = []
        self.fail = fail

    async def append(self, *, topic, aggregate_id, payload, dedupe_key):
        if self.fail:
            raise RuntimeError("outbox failed")
        self.items.append((topic, aggregate_id, payload, dedupe_key))


class RecordingQuality:
    def __init__(self) -> None:
        self.commands = []

    async def open(self, command):
        self.commands.append(command)


def _command(
    symbols=("600000.SH",),
    key="daily:2026-07-15",
    security_ids=None,
):
    return CreateDailyBatch(
        trading_date=DAY,
        universe_snapshot_id=uuid4(),
        symbols=symbols,
        security_ids=security_ids
        or tuple(uuid5(NAMESPACE_DNS, symbol) for symbol in symbols),
        idempotency_key=key,
    )


def _stage(
    symbol="600000.SH",
    *,
    security_id=None,
    status=DailyStageStatus.FETCHED,
    reason=None,
    close="10.20",
):
    payload = None
    if status is DailyStageStatus.FETCHED:
        payload = {
            "symbol": symbol,
            "trading_date": DAY,
            "open": "10.00",
            "high": "10.50",
            "low": "9.90",
            "close": close,
            "previous_close": "10.00",
            "volume": 100,
            "amount": "1020.00",
            "source": "EASTMONEY",
        }
    return StageDailyBar(
        symbol=symbol,
        security_id=security_id or uuid5(NAMESPACE_DNS, symbol),
        trading_date=DAY,
        status=status,
        provider_payload=payload,
        missing_reason=reason,
        received_at=NOW,
    )


def _service(repo=None, events=None, quality=None):
    return DailyDataService(
        repo or FakeRepository(),
        events=events or RecordingEvents(),
        quality_issues=quality or RecordingQuality(),
        now_provider=lambda: NOW,
    )


async def _validate_and_commit(service, batch_id):
    await service.validate(batch_id)
    return await service.commit(batch_id)


@async_test
async def test_create_is_idempotent_and_empty_scope_is_rejected_by_contract() -> None:
    repo = FakeRepository()
    service = _service(repo)
    command = _command()
    first = await service.create(command)
    second = await service.create(command)
    assert first.id == second.id
    assert len(repo.batches) == 1


@async_test
async def test_successful_batch_commits_valid_bar_and_completed_event() -> None:
    repo, events = FakeRepository(), RecordingEvents()
    service = _service(repo, events)
    batch = await service.create(_command())
    item = _stage()
    await service.stage(batch.id, item)
    result = await _validate_and_commit(service, batch.id)
    assert result.status is DailyBatchStatus.SUCCEEDED
    assert result.committed_count == 1
    assert repo.bars[(item.security_id, DAY)].close == Decimal("10.20")
    assert [event[0] for event in events.items] == ["daily_batch.completed"]


@async_test
async def test_explained_missing_does_not_degrade_batch() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    await service.stage(
        batch.id,
        _stage(status=DailyStageStatus.MISSING, reason=DailyMissingReason.SUSPENDED),
    )
    result = await _validate_and_commit(service, batch.id)
    assert result.status is DailyBatchStatus.SUCCEEDED
    assert result.committed_count == 0


@async_test
async def test_unexplained_missing_makes_partial_when_one_bar_commits() -> None:
    repo, quality = FakeRepository(), RecordingQuality()
    service = _service(repo, quality=quality)
    batch = await service.create(_command(("600000.SH", "000001.SZ")))
    await service.stage(batch.id, _stage())
    await service.stage(
        batch.id,
        _stage(
            "000001.SZ",
            status=DailyStageStatus.MISSING,
            reason=DailyMissingReason.UNEXPLAINED,
        ),
    )
    result = await _validate_and_commit(service, batch.id)
    assert result.status is DailyBatchStatus.PARTIAL
    assert result.committed_count == 1
    assert len(quality.commands) == 1


@async_test
async def test_zero_commits_with_unexplained_missing_is_failed() -> None:
    events = RecordingEvents()
    service = _service(events=events)
    batch = await service.create(_command())
    await service.stage(
        batch.id,
        _stage(status=DailyStageStatus.MISSING, reason=DailyMissingReason.UNEXPLAINED),
    )
    result = await _validate_and_commit(service, batch.id)
    assert result.status is DailyBatchStatus.FAILED
    assert events.items == []


@async_test
async def test_same_value_replay_has_no_revision_and_changed_value_revises() -> None:
    repo, events = FakeRepository(), RecordingEvents()
    service = _service(repo, events)
    security_id = uuid4()
    first = await service.create(_command(key="first", security_ids=(security_id,)))
    await service.stage(first.id, _stage(security_id=security_id))
    await _validate_and_commit(service, first.id)
    replay = await service.create(_command(key="replay", security_ids=(security_id,)))
    await service.stage(replay.id, _stage(security_id=security_id))
    await _validate_and_commit(service, replay.id)
    assert repo.revisions == []

    changed = await service.create(_command(key="changed", security_ids=(security_id,)))
    await service.stage(changed.id, _stage(security_id=security_id, close="10.30"))
    await _validate_and_commit(service, changed.id)
    assert len(repo.revisions) == 1
    assert repo.bars[(security_id, DAY)].data_version == 2
    assert "daily_bar.corrected" in [event[0] for event in events.items]


@async_test
async def test_commit_locks_bar_key_before_reading_current_fact() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    staged = _stage()
    await service.stage(batch.id, staged)

    await _validate_and_commit(service, batch.id)

    assert repo.bar_write_order[:2] == [
        ("lock", staged.security_id, DAY),
        ("get", staged.security_id, DAY),
    ]


@async_test
async def test_one_symbol_failure_does_not_rollback_another_symbol() -> None:
    repo = FakeRepository()
    repo.fail_symbols.add("000001.SZ")
    service = _service(repo)
    batch = await service.create(_command(("600000.SH", "000001.SZ")))
    good, bad = _stage(), _stage("000001.SZ")
    await service.stage(batch.id, good)
    await service.stage(batch.id, bad)
    result = await _validate_and_commit(service, batch.id)
    assert result.status is DailyBatchStatus.PARTIAL
    assert (good.security_id, DAY) in repo.bars
    assert (bad.security_id, DAY) not in repo.bars


@async_test
async def test_retry_scope_only_contains_original_unexplained_or_failed_symbols() -> (
    None
):
    service = _service()
    batch = await service.create(_command(("600000.SH", "000001.SZ")))
    await service.stage(
        batch.id,
        _stage(status=DailyStageStatus.MISSING, reason=DailyMissingReason.SUSPENDED),
    )
    await service.stage(
        batch.id,
        _stage(
            "000001.SZ",
            status=DailyStageStatus.MISSING,
            reason=DailyMissingReason.UNEXPLAINED,
        ),
    )
    await _validate_and_commit(service, batch.id)
    assert await service.retry_scope(batch.id) == ("000001.SZ",)


@async_test
async def test_stage_rejects_symbol_or_date_outside_frozen_contract() -> None:
    from long_invest.platform.errors import AppError

    service = _service()
    batch = await service.create(_command())
    with pytest.raises(AppError, match="冻结范围"):
        await service.stage(batch.id, _stage("000001.SZ"))
    wrong = StageDailyBar(
        symbol="600000.SH",
        security_id=uuid5(NAMESPACE_DNS, "600000.SH"),
        trading_date=date(2026, 7, 14),
        status=DailyStageStatus.MISSING,
        missing_reason=DailyMissingReason.UNEXPLAINED,
        received_at=NOW,
    )
    with pytest.raises(AppError, match="日期"):
        await service.stage(batch.id, wrong)


@async_test
async def test_outbox_failure_rolls_back_completion_by_propagating() -> None:
    service = _service(events=RecordingEvents(fail=True))
    batch = await service.create(_command())
    await service.stage(batch.id, _stage())
    await service.validate(batch.id)
    with pytest.raises(RuntimeError, match="outbox"):
        await service.commit(batch.id)


@async_test
async def test_validate_opens_review_issue_for_unexplained_price_jump() -> None:
    quality = RecordingQuality()
    service = _service(quality=quality)
    batch = await service.create(_command())
    fetched = _stage()
    fetched = StageDailyBar(
        symbol=fetched.symbol,
        security_id=fetched.security_id,
        trading_date=fetched.trading_date,
        status=DailyStageStatus.FETCHED,
        provider_payload={
            **dict(fetched.provider_payload),
            "open": "20",
            "high": "21",
            "low": "19",
            "close": "20",
        },
        received_at=fetched.received_at,
    )
    await service.stage(batch.id, fetched)
    result = await service.validate(batch.id)
    assert result.validated_count == 1
    assert len(quality.commands) == 1
    assert quality.commands[0].requires_review is True


@async_test
async def test_empty_staging_cannot_skip_validation_and_preserves_scope() -> None:
    from long_invest.platform.errors import AppError

    service = _service()
    batch = await service.create(_command(("600000.SH", "000001.SZ")))
    with pytest.raises(AppError) as captured:
        await service.commit(batch.id)
    assert captured.value.code == "DAILY_BATCH_STATE_CONFLICT"
    with pytest.raises(AppError) as retry_error:
        await service.retry_scope(batch.id)
    assert retry_error.value.code == "DAILY_RETRY_STATE_CONFLICT"
    assert retry_error.value.details == {"status": "PENDING"}


@async_test
async def test_stage_rejects_security_id_outside_frozen_symbol_binding() -> None:
    from long_invest.platform.errors import AppError

    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())

    with pytest.raises(AppError) as captured:
        await service.stage(batch.id, _stage(security_id=uuid4()))

    assert captured.value.code == "DAILY_BAR_SECURITY_MISMATCH"
    assert repo.stages == {}


@async_test
async def test_provider_timeout_isolated_as_unexplained_failure() -> None:
    service = _service()
    batch = await service.create(_command())
    await service.stage(
        batch.id,
        StageDailyBar(
            symbol="600000.SH",
            security_id=uuid5(NAMESPACE_DNS, "600000.SH"),
            trading_date=DAY,
            status=DailyStageStatus.FAILED,
            error_code="DAILY_PROVIDER_TIMEOUT",
            received_at=NOW,
        ),
    )
    result = await _validate_and_commit(service, batch.id)
    assert result.status is DailyBatchStatus.FAILED
    assert result.failed_count == 1


@async_test
async def test_new_service_instance_recovers_persisted_staging() -> None:
    repo, events, quality = FakeRepository(), RecordingEvents(), RecordingQuality()
    first_worker = _service(repo, events, quality)
    batch = await first_worker.create(_command())
    fetched = _stage()
    await first_worker.stage(batch.id, fetched)

    recovered_worker = _service(repo, events, quality)
    result = await _validate_and_commit(recovered_worker, batch.id)
    assert result.status is DailyBatchStatus.SUCCEEDED
    assert (fetched.security_id, DAY) in repo.bars


@async_test
async def test_validate_terminal_batch_is_an_idempotent_replay() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    await service.stage(batch.id, _stage())
    committed = await _validate_and_commit(service, batch.id)

    replayed = await service.validate(batch.id)

    assert committed.status is DailyBatchStatus.SUCCEEDED
    assert replayed.status is DailyBatchStatus.SUCCEEDED
    assert repo.batches[batch.id].status is DailyBatchStatus.SUCCEEDED


@async_test
async def test_commit_savepoint_failure_remains_in_retry_scope() -> None:
    repo = FakeRepository()
    repo.fail_symbols.add("600000.SH")
    service = _service(repo)
    batch = await service.create(_command())
    await service.stage(batch.id, _stage())

    result = await _validate_and_commit(service, batch.id)

    assert result.status is DailyBatchStatus.FAILED
    assert await service.retry_scope(batch.id) == ("600000.SH",)


@async_test
async def test_validate_preserves_every_terminal_status() -> None:
    repo = FakeRepository()
    service = _service(repo)
    for status in (
        DailyBatchStatus.SUCCEEDED,
        DailyBatchStatus.PARTIAL,
        DailyBatchStatus.FAILED,
    ):
        batch = await service.create(_command(key=f"terminal-{status.value}"))
        repo.batches[batch.id].status = status
        assert (await service.validate(batch.id)).status is status


@async_test
async def test_validate_rejects_states_before_fetch_and_during_commit() -> None:
    from long_invest.platform.errors import AppError

    repo = FakeRepository()
    service = _service(repo)
    for status in (DailyBatchStatus.PENDING, DailyBatchStatus.COMMITTING):
        batch = await service.create(_command(key=f"invalid-{status.value}"))
        repo.batches[batch.id].status = status
        with pytest.raises(AppError) as captured:
            await service.validate(batch.id)
        assert captured.value.code == "DAILY_BATCH_STATE_CONFLICT"


@async_test
async def test_retry_scope_is_deduplicated_in_frozen_order_and_never_expands() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command(("600000.SH", "000001.SZ", "430047.BJ")))
    await service.stage(
        batch.id,
        _stage(
            "000001.SZ",
            status=DailyStageStatus.MISSING,
            reason=DailyMissingReason.UNEXPLAINED,
        ),
    )
    await _validate_and_commit(service, batch.id)
    repo.missing[batch.id].extend(
        [
            repo.missing[batch.id][0],
            SimpleNamespace(
                symbol="300001.SZ",
                explained=False,
                reason=DailyMissingReason.UNEXPLAINED,
            ),
        ]
    )

    assert await service.retry_scope(batch.id) == (
        "600000.SH",
        "000001.SZ",
        "430047.BJ",
    )
    assert repo.batch_lock_requests[-1] == (batch.id, True)


@async_test
@pytest.mark.parametrize(
    "status",
    [
        DailyBatchStatus.PENDING,
        DailyBatchStatus.FETCHING,
        DailyBatchStatus.VALIDATING,
        DailyBatchStatus.COMMITTING,
        DailyBatchStatus.SUCCEEDED,
    ],
)
async def test_retry_scope_locks_and_rejects_non_retryable_batch_states(status) -> None:
    from long_invest.platform.errors import AppError

    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command(key=f"retry-state-{status.value}"))
    repo.batches[batch.id].status = status

    with pytest.raises(AppError) as captured:
        await service.retry_scope(batch.id)

    assert captured.value.code == "DAILY_RETRY_STATE_CONFLICT"
    assert captured.value.status_code == 409
    assert captured.value.details == {"status": status.value}
    assert repo.batch_lock_requests[-1] == (batch.id, True)


@async_test
@pytest.mark.parametrize("status", [DailyBatchStatus.PARTIAL, DailyBatchStatus.FAILED])
async def test_retry_scope_allows_terminal_failure_states(status) -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command(key=f"retry-allowed-{status.value}"))
    repo.batches[batch.id].status = status
    repo.missing[batch.id] = [
        SimpleNamespace(
            symbol="600000.SH",
            explained=False,
            reason=DailyMissingReason.UNEXPLAINED,
        )
    ]

    assert await service.retry_scope(batch.id) == ("600000.SH",)
    assert repo.batch_lock_requests[-1] == (batch.id, True)


@async_test
async def test_validate_restores_json_payload_types_after_restart() -> None:
    repo = FakeRepository()
    first_worker = _service(repo)
    batch = await first_worker.create(_command())
    fetched = _stage(status=DailyStageStatus.FETCHED)
    await first_worker.stage(batch.id, fetched)
    persisted = repo.stages[(batch.id, fetched.symbol)]
    persisted.provider_payload = {
        **persisted.provider_payload,
        "trading_date": DAY.isoformat(),
        "open": "10.00",
        "high": "10.50",
        "low": "9.90",
        "close": "10.20",
        "previous_close": "10.00",
        "volume": "100",
        "amount": "1020.00",
    }

    recovered_worker = _service(repo)
    result = await recovered_worker.validate(batch.id)

    assert result.validated_count == 1
    assert persisted.status is DailyStageStatus.VALID
    assert persisted.validated_at == NOW


@async_test
async def test_missing_previous_close_uses_last_formal_close_and_persists_it() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    fetched = _stage()
    payload = dict(fetched.provider_payload)
    del payload["previous_close"]
    fetched = StageDailyBar(
        symbol=fetched.symbol,
        security_id=fetched.security_id,
        trading_date=fetched.trading_date,
        status=fetched.status,
        provider_payload=payload,
        received_at=fetched.received_at,
    )
    repo.previous_closes[(fetched.security_id, DAY)] = Decimal("9.80")
    await service.stage(batch.id, fetched)

    await service.validate(batch.id)
    result = await service.commit(batch.id)

    stage = repo.stages[(batch.id, fetched.symbol)]
    assert stage.provider_payload["previous_close"] == "9.80"
    assert result.status is DailyBatchStatus.SUCCEEDED
    assert repo.bars[(fetched.security_id, DAY)].previous_close == Decimal("9.80")


@async_test
async def test_normal_stock_without_previous_close_is_invalid() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    fetched = _stage()
    payload = dict(fetched.provider_payload)
    del payload["previous_close"]
    await service.stage(
        batch.id,
        StageDailyBar(
            symbol=fetched.symbol,
            security_id=fetched.security_id,
            trading_date=fetched.trading_date,
            status=fetched.status,
            provider_payload=payload,
            received_at=fetched.received_at,
        ),
    )

    result = await service.validate(batch.id)

    stage = repo.stages[(batch.id, fetched.symbol)]
    assert result.validated_count == 0
    assert stage.status is DailyStageStatus.INVALID
    assert stage.error_code == "DAILY_BAR_PREVIOUS_CLOSE_MISSING"


@async_test
async def test_new_listing_without_previous_close_is_explicitly_allowed() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    fetched = _stage()
    payload = dict(fetched.provider_payload)
    del payload["previous_close"]
    payload["is_new_listing"] = True
    await service.stage(
        batch.id,
        StageDailyBar(
            symbol=fetched.symbol,
            security_id=fetched.security_id,
            trading_date=fetched.trading_date,
            status=fetched.status,
            provider_payload=payload,
            received_at=fetched.received_at,
        ),
    )

    await service.validate(batch.id)
    result = await service.commit(batch.id)

    assert result.status is DailyBatchStatus.SUCCEEDED
    assert repo.bars[(fetched.security_id, DAY)].previous_close is None


@async_test
@pytest.mark.parametrize("previous_close", ["bad", "NaN", "Infinity", "0", "-1"])
async def test_invalid_previous_close_marks_only_that_stage_invalid(
    previous_close,
) -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    fetched = _stage()
    payload = {**dict(fetched.provider_payload), "previous_close": previous_close}
    await service.stage(
        batch.id,
        StageDailyBar(
            symbol=fetched.symbol,
            security_id=fetched.security_id,
            trading_date=fetched.trading_date,
            status=fetched.status,
            provider_payload=payload,
            received_at=fetched.received_at,
        ),
    )

    result = await service.validate(batch.id)

    stage = repo.stages[(batch.id, fetched.symbol)]
    assert result.validated_count == 0
    assert stage.status is DailyStageStatus.INVALID
    assert stage.error_code == "DAILY_BAR_PREVIOUS_CLOSE_INVALID"


@async_test
async def test_bad_previous_close_does_not_interrupt_other_symbols() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command(("600000.SH", "000001.SZ")))
    bad = _stage()
    good = _stage("000001.SZ")
    await service.stage(
        batch.id,
        StageDailyBar(
            symbol=bad.symbol,
            security_id=bad.security_id,
            trading_date=bad.trading_date,
            status=bad.status,
            provider_payload={**dict(bad.provider_payload), "previous_close": "NaN"},
            received_at=bad.received_at,
        ),
    )
    await service.stage(batch.id, good)

    validated = await service.validate(batch.id)
    committed = await service.commit(batch.id)

    assert validated.validated_count == 1
    assert committed.status is DailyBatchStatus.PARTIAL
    assert (good.security_id, DAY) in repo.bars
    assert (bad.security_id, DAY) not in repo.bars


@async_test
async def test_commit_rejects_batch_that_has_not_completed_validation() -> None:
    from long_invest.platform.errors import AppError

    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    fetched = _stage(status=DailyStageStatus.FETCHED)
    await service.stage(batch.id, fetched)

    with pytest.raises(AppError) as captured:
        await service.commit(batch.id)

    assert captured.value.code == "DAILY_BATCH_STATE_CONFLICT"
    assert repo.bars == {}
    assert repo.batches[batch.id].status is DailyBatchStatus.FETCHING


@async_test
async def test_mismatched_provider_payload_cannot_reach_formal_daily_bars() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    fetched = _stage(status=DailyStageStatus.FETCHED)
    malicious = StageDailyBar(
        symbol=fetched.symbol,
        security_id=fetched.security_id,
        trading_date=fetched.trading_date,
        status=DailyStageStatus.FETCHED,
        provider_payload={
            **dict(fetched.provider_payload),
            "symbol": "000001.SZ",
            "trading_date": date(2026, 7, 14),
        },
        received_at=fetched.received_at,
    )
    await service.stage(batch.id, malicious)

    validated = await service.validate(batch.id)
    committed = await service.commit(batch.id)

    stage = repo.stages[(batch.id, fetched.symbol)]
    assert validated.validated_count == 0
    assert stage.status is DailyStageStatus.INVALID
    assert stage.error_code == "DAILY_BAR_SYMBOL_MISMATCH"
    assert committed.status is DailyBatchStatus.FAILED
    assert repo.bars == {}


@async_test
async def test_commit_refuses_valid_status_without_validation_timestamp() -> None:
    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command())
    fetched = _stage(status=DailyStageStatus.FETCHED)
    await service.stage(batch.id, fetched)
    stage = repo.stages[(batch.id, fetched.symbol)]
    stage.status = DailyStageStatus.VALID
    stage.validated_at = None
    repo.batches[batch.id].status = DailyBatchStatus.VALIDATING

    result = await service.commit(batch.id)

    assert result.status is DailyBatchStatus.FAILED
    assert repo.missing[batch.id][0].error_code == "DAILY_BAR_NOT_VALIDATED"
    assert repo.bars == {}


@async_test
@pytest.mark.parametrize(
    "status",
    [
        DailyBatchStatus.VALIDATING,
        DailyBatchStatus.COMMITTING,
        DailyBatchStatus.SUCCEEDED,
        DailyBatchStatus.PARTIAL,
        DailyBatchStatus.FAILED,
    ],
)
async def test_stage_locks_batch_and_cannot_reopen_late_states(status) -> None:
    from long_invest.platform.errors import AppError

    repo = FakeRepository()
    service = _service(repo)
    batch = await service.create(_command(key=f"late-{status.value}"))
    repo.batches[batch.id].status = status

    with pytest.raises(AppError) as captured:
        await service.stage(batch.id, _stage(status=DailyStageStatus.FETCHED))

    assert captured.value.code == "DAILY_BATCH_STATE_CONFLICT"
    assert repo.batch_lock_requests[-1] == (batch.id, True)
    assert repo.batches[batch.id].status is status
