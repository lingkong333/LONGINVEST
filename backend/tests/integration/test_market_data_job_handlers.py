from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

import long_invest.bootstrap.jobs as jobs_module
from long_invest.bootstrap.jobs import (
    DatabaseQuoteCycles,
    _corporate_action_scope,
    _daily_finalize_job,
    _daily_item_job,
    _known_daily_absence,
    _quote_command,
    daily_data_coordinate,
    daily_data_finalize,
    daily_data_item,
    daily_data_retry,
    qfq_refresh,
    quote_diagnostic,
    realtime_quote_cycle,
    security_master_refresh,
    signal_evaluate_batch,
    signal_reevaluate,
)
from long_invest.entrypoints.job_worker import HANDLERS
from long_invest.modules.daily_data.contracts import DailyMissingReason
from long_invest.platform.jobs.contracts import JobExecutionContext


def context(config=None):
    return JobExecutionContext(job_id=uuid4(), fence_token=uuid4(), config=config or {})


def test_market_data_handlers_are_registered_on_the_real_worker() -> None:
    expected = {
        **{key: value for key, value in HANDLERS.items() if key == "SYSTEM_NOOP"},
        "SECURITY_MASTER_REFRESH": security_master_refresh,
        "REALTIME_QUOTE_CYCLE": realtime_quote_cycle,
        "QUOTE_DIAGNOSTIC": quote_diagnostic,
        "DAILY_DATA_COORDINATE": daily_data_coordinate,
        "DAILY_DATA_ITEM": daily_data_item,
        "DAILY_DATA_FINALIZE": daily_data_finalize,
        "DAILY_DATA_RETRY": daily_data_retry,
        "QFQ_REFRESH": qfq_refresh,
        "SIGNAL_EVALUATE_BATCH": signal_evaluate_batch,
        "SIGNAL_REEVALUATE": signal_reevaluate,
    }
    assert expected.items() <= HANDLERS.items()


@pytest.mark.parametrize(
    ("values", "reason"),
    [
        ({"listed_on": "2026-07-17"}, DailyMissingReason.NOT_YET_LISTED),
        ({"delisted_on": "2026-07-15"}, DailyMissingReason.DELISTED),
        ({"is_suspended": True}, DailyMissingReason.SUSPENDED),
    ],
)
def test_frozen_security_state_explains_expected_missing_daily_bar(
    values, reason
) -> None:
    stage = _known_daily_absence(
        values,
        uuid4(),
        "600000.SH",
        date(2026, 7, 16),
    )
    assert stage is not None
    assert stage.missing_reason is reason


def test_each_daily_symbol_gets_an_independent_bounded_job() -> None:
    parent = context()
    item = SimpleNamespace(
        symbol="600000.SH",
        security_id=uuid4(),
        is_suspended=False,
        is_st=False,
        listed_on=date(1999, 11, 10),
        delisted_on=None,
    )
    command = _daily_item_job(parent, uuid4(), date(2026, 7, 16), item)
    assert command.job_type == "DAILY_DATA_ITEM"
    assert command.queue == "daily-market-data"
    assert command.soft_timeout_seconds == 240
    assert command.hard_timeout_seconds == 300
    assert command.config_snapshot["parent_job_id"] == str(parent.job_id)


def test_daily_item_job_freezes_known_corporate_action_context() -> None:
    parent = context()
    item = SimpleNamespace(
        symbol="600000.SH",
        security_id=uuid4(),
        is_suspended=False,
        is_st=False,
        listed_on=date(1999, 11, 10),
        delisted_on=None,
    )

    command = _daily_item_job(
        parent,
        uuid4(),
        date(2026, 7, 16),
        item,
        has_known_corporate_action=True,
    )

    assert command.config_snapshot["has_known_corporate_action"] is True

    other = SimpleNamespace(
        symbol="000001.SZ",
        security_id=uuid4(),
        is_suspended=False,
        is_st=False,
        listed_on=date(1991, 4, 3),
        delisted_on=None,
    )
    unaffected = _daily_item_job(
        parent,
        uuid4(),
        date(2026, 7, 16),
        other,
        has_known_corporate_action=False,
    )
    assert unaffected.config_snapshot["has_known_corporate_action"] is False


def test_daily_parent_rejects_corporate_action_context_outside_scope() -> None:
    config = {"known_corporate_action_symbols": ["600000.SH"]}
    assert _corporate_action_scope(config, ("600000.SH", "000001.SZ")) == {"600000.SH"}

    with pytest.raises(ValueError):
        _corporate_action_scope(config, ("000001.SZ",))


def test_daily_finalize_job_links_parent_for_failure_reconciliation() -> None:
    parent_job_id = uuid4()
    command = _daily_finalize_job(parent_job_id, uuid4())

    assert command.config_snapshot["linked_parent_job_id"] == str(parent_job_id)


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("handler", "code"),
    [
        (daily_data_coordinate, "DAILY_COORDINATE_CONFIG_INVALID"),
        (daily_data_item, "DAILY_ITEM_CONFIG_INVALID"),
        (daily_data_finalize, "DAILY_FINALIZE_CONFIG_INVALID"),
        (daily_data_retry, "DAILY_RETRY_CONFIG_INVALID"),
        (quote_diagnostic, "QUOTE_DIAGNOSTIC_CONFIG_INVALID"),
        (realtime_quote_cycle, "QUOTE_CYCLE_CONFIG_INVALID"),
    ],
)
async def test_market_data_handlers_reject_incomplete_frozen_config(
    handler, code
) -> None:
    result = await handler(context())
    assert result.success is False
    assert result.code == code


def test_realtime_job_recovery_reuses_the_frozen_request_time() -> None:
    job_id = uuid4()
    config = {
        "symbols": ["600000.SH"],
        "timeout_seconds": 30,
        "universe_snapshot_id": str(uuid4()),
        "universe_snapshot_version": 7,
        "requested_at": "2026-07-16T02:00:00+00:00",
    }
    first = _quote_command(
        JobExecutionContext(job_id=job_id, fence_token=uuid4(), config=config)
    )
    recovered = _quote_command(
        JobExecutionContext(job_id=job_id, fence_token=uuid4(), config=config)
    )
    assert recovered.scheduled_at == first.scheduled_at


@pytest.mark.anyio
async def test_database_quote_cycles_delegates_cancellation(monkeypatch) -> None:
    cycle_id = uuid4()
    now = datetime(2026, 7, 16, 2, tzinfo=UTC)
    calls = []

    class Database:
        @asynccontextmanager
        async def transaction(self):
            yield object()

    class Service:
        async def cancel(self, requested_id, requested_at, reason):
            calls.append((requested_id, requested_at, reason))
            return SimpleNamespace(id=requested_id)

    monkeypatch.setattr(jobs_module, "_quote_cycle_service", lambda _session: Service())

    result = await DatabaseQuoteCycles(Database()).cancel(
        cycle_id, now, "JOB_EXECUTION_CANCELED"
    )

    assert result.id == cycle_id
    assert calls == [(cycle_id, now, "JOB_EXECUTION_CANCELED")]


@pytest.mark.anyio
async def test_expired_monitor_quote_job_is_missed_before_collection(
    monkeypatch,
) -> None:
    now = datetime(2026, 7, 17, 2, 16, 1, tzinfo=UTC)
    job_id = uuid4()
    calls = []

    class Occurrences:
        async def mark_job_missed(self, requested_job_id, requested_at):
            calls.append((requested_job_id, requested_at))

    monkeypatch.setattr(jobs_module, "_utc_now", lambda: now)
    monkeypatch.setattr(
        jobs_module,
        "get_monitor_occurrence_application",
        lambda: Occurrences(),
    )
    monkeypatch.setattr(
        jobs_module,
        "QuoteCollectionService",
        lambda *_args, **_kwargs: pytest.fail("expired job must not collect quotes"),
    )
    result = await realtime_quote_cycle(
        JobExecutionContext(
            job_id=job_id,
            fence_token=uuid4(),
            config={
                "symbols": ["600000.SH"],
                "timeout_seconds": 30,
                "universe_snapshot_id": str(uuid4()),
                "universe_snapshot_version": 1,
                "requested_at": "2026-07-17T02:15:00+00:00",
                "claim_deadline_at": "2026-07-17T02:16:00+00:00",
            },
        )
    )

    assert result.code == "SCHEDULE_OCCURRENCE_MISSED"
    assert result.retryable is False
    assert calls == [(job_id, now)]
