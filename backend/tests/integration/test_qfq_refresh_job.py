from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest

import long_invest.bootstrap.jobs as jobs_module
from long_invest.bootstrap.jobs import qfq_refresh
from long_invest.modules.daily_data.contracts import DailyBarSnapshot
from long_invest.modules.providers.contracts import (
    DailyBar,
    ProviderBatchResult,
    ProviderCapability,
    ProviderCode,
    ProviderItemFailure,
)
from long_invest.platform.jobs.contracts import JobExecutionContext

START = date(2026, 7, 15)
END = date(2026, 7, 16)
SECURITY_ID = uuid4()


def context(**changes) -> JobExecutionContext:
    config = {
        "refresh_run_id": str(uuid4()),
        "security_id": str(SECURITY_ID),
        "symbol": "600000.SH",
        "start": START.isoformat(),
        "end": END.isoformat(),
        "as_of_date": END.isoformat(),
        "expected_trade_dates": [START.isoformat(), END.isoformat()],
        "input_daily_version": 3,
        "unadjusted_close": "10.500000",
        "trigger_reason": "MANUAL",
        "provider": "EASTMONEY",
    }
    config.update(changes)
    return JobExecutionContext(job_id=uuid4(), fence_token=uuid4(), config=config)


def bars() -> tuple[DailyBar, ...]:
    return tuple(
        DailyBar(
            symbol="600000.SH",
            trading_date=trading_date,
            open=Decimal("10"),
            high=Decimal("11"),
            low=Decimal("9"),
            close=Decimal("10.5"),
            volume=100,
            amount=Decimal("1050"),
            source=ProviderCode.EASTMONEY,
            capability=ProviderCapability.HISTORICAL_DAILY_QFQ,
        )
        for trading_date in (START, END)
    )


class FakeDatabase:
    def __init__(self):
        self.session_calls = 0
        self.transaction_calls = 0

    @asynccontextmanager
    async def session(self):
        self.session_calls += 1
        yield object()

    @asynccontextmanager
    async def transaction(self):
        self.transaction_calls += 1
        raise AssertionError("Provider HTTP must not run in an explicit transaction")
        yield


class FakeQfqApplication:
    def __init__(self, begin_result=None):
        self.calls = []
        self.begin_result = begin_result

    async def begin_fetch(self, refresh_run_id, *, now=None):
        self.calls.append(("begin_fetch", refresh_run_id, {"now": now}))
        return self.begin_result

    async def begin_validation(self, refresh_run_id, *, now=None):
        self.calls.append(("begin_validation", refresh_run_id, {"now": now}))

    async def activate(self, refresh_run_id, window, **kwargs):
        self.calls.append(("activate", refresh_run_id, window, kwargs))
        if kwargs["current_input_daily_version"] != 3:
            return None
        return SimpleNamespace(
            id=uuid4(), version=2, row_count=window.row_count, checksum=window.checksum
        )

    async def fail(self, refresh_run_id, **kwargs):
        self.calls.append(("fail", refresh_run_id, kwargs))


class FakeDailyApplication:
    def __init__(self, snapshot=None):
        self.calls = []
        self._snapshot = snapshot or DailyBarSnapshot(
            security_id=SECURITY_ID,
            symbol="600000.SH",
            trade_date=END,
            close=Decimal("10.500000"),
            data_version=3,
            source="eastmoney",
            updated_at=datetime.fromisoformat("2026-07-16T10:00:00+00:00"),
        )

    async def snapshot(self, symbol, trade_date):
        self.calls.append((symbol, trade_date))
        return self._snapshot


class FakeProvider:
    def __init__(self, result=None, error=None):
        self.calls = []
        self.result = result or ProviderBatchResult(items=bars())
        self.error = error

    async def daily_bars(self, request, deadline):
        self.calls.append((request, deadline))
        if self.error is not None:
            raise self.error
        return self.result

    async def get_provider(self, provider):
        self.calls.append(("get_provider", provider))
        if self.error is not None:
            raise self.error
        return {"provider_code": provider.value, "version": 7}


def wire(monkeypatch, *, app=None, daily=None, provider=None, database=None):
    app = app or FakeQfqApplication()
    daily = daily or FakeDailyApplication()
    provider = provider or FakeProvider()
    database = database or FakeDatabase()
    monkeypatch.setattr(jobs_module, "_qfq_application", lambda: app)
    monkeypatch.setattr(jobs_module, "_daily_data_application", lambda: daily)
    monkeypatch.setattr(jobs_module, "get_database", lambda: database)
    monkeypatch.setattr(
        jobs_module, "build_provider_service", lambda _session: provider
    )
    return app, daily, provider, database


@pytest.mark.anyio
@pytest.mark.parametrize(
    "changes",
    [
        {"refresh_run_id": "bad"},
        {"symbol": "600000.SZ"},
        {"start": "2026-07-17"},
        {"expected_trade_dates": []},
        {"expected_trade_dates": [END.isoformat(), START.isoformat()]},
        {"input_daily_version": 0},
        {"unadjusted_close": "NaN"},
        {"provider": "SINA"},
    ],
)
async def test_qfq_handler_rejects_invalid_frozen_config(monkeypatch, changes) -> None:
    app, _daily, provider, _database = wire(monkeypatch)

    result = await qfq_refresh(context(**changes))

    assert result.success is False
    assert result.code == "QFQ_REFRESH_CONFIG_INVALID"
    assert result.retryable is False
    assert app.calls == []
    assert provider.calls == []


@pytest.mark.anyio
async def test_qfq_handler_fetches_validates_rechecks_and_activates(
    monkeypatch,
) -> None:
    app, daily, provider, database = wire(monkeypatch)
    job = context()

    result = await qfq_refresh(job)

    assert provider.calls[0] == ("get_provider", ProviderCode.EASTMONEY)
    request, _deadline = provider.calls[1]
    assert request.symbol == "600000.SH"
    assert request.start == START and request.end == END
    assert request.capability is ProviderCapability.HISTORICAL_DAILY_QFQ
    assert database.session_calls == 1
    assert database.transaction_calls == 0
    assert daily.calls == [("600000.SH", END)]
    assert [call[0] for call in app.calls] == [
        "begin_fetch",
        "begin_validation",
        "activate",
    ]
    activation = app.calls[-1]
    for call in app.calls:
        assert call[-1]["now"].tzinfo is not None
        assert call[-1]["now"].utcoffset() is not None
    assert activation[3]["current_input_daily_version"] == 3
    assert activation[3]["provider_contract_version"] == "EASTMONEY:config-v7"
    assert result.success is True
    assert result.data["refresh_run_id"] == job.config["refresh_run_id"]
    assert result.data["version"] == 2


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("provider", "expected_code", "retryable"),
    [
        (FakeProvider(error=TimeoutError("late")), "QFQ_REFRESH_TIMED_OUT", True),
        (
            FakeProvider(
                result=ProviderBatchResult(
                    failures=(
                        ProviderItemFailure(
                            symbol="600000.SH",
                            code="PROVIDER_CIRCUIT_OPEN",
                            message="open",
                            provider=ProviderCode.EASTMONEY,
                        ),
                    ),
                    batch_error_code="PROVIDER_CIRCUIT_OPEN",
                )
            ),
            "QFQ_PROVIDER_FAILED",
            True,
        ),
    ],
)
async def test_qfq_handler_persists_provider_failure(
    monkeypatch, provider, expected_code, retryable
) -> None:
    app, _daily, _provider, _database = wire(monkeypatch, provider=provider)
    job = context()

    result = await qfq_refresh(job)

    assert result.code == expected_code
    assert result.retryable is retryable
    assert app.calls[-1][0] == "fail"
    assert app.calls[-1][2]["code"] == expected_code
    assert app.calls[-1][2]["retryable"] is retryable
    assert app.calls[-1][2]["now"].tzinfo is not None
    assert str(app.calls[-1][1]) == job.config["refresh_run_id"]


@pytest.mark.anyio
async def test_qfq_handler_persists_validation_failure(monkeypatch) -> None:
    provider = FakeProvider(result=ProviderBatchResult(items=bars()[:1]))
    app, _daily, _provider, _database = wire(monkeypatch, provider=provider)

    result = await qfq_refresh(context())

    assert result.success is False
    assert result.code == "QFQ_WINDOW_INCOMPLETE"
    assert result.retryable is False
    assert app.calls[-1][0] == "fail"
    assert app.calls[-1][2] == {
        "code": "QFQ_WINDOW_INCOMPLETE",
        "retryable": False,
        "now": app.calls[-1][2]["now"],
    }
    assert app.calls[-1][2]["now"].tzinfo is not None


@pytest.mark.anyio
async def test_qfq_handler_rejects_changed_daily_input_before_activation(
    monkeypatch,
) -> None:
    daily = FakeDailyApplication()
    daily._snapshot = SimpleNamespace(
        security_id=SECURITY_ID,
        symbol="600000.SH",
        trade_date=END,
        close=Decimal("10.6"),
        data_version=4,
    )
    app, _daily, _provider, _database = wire(monkeypatch, daily=daily)

    result = await qfq_refresh(context())

    assert result.code == "QFQ_INPUT_SUPERSEDED"
    assert app.calls[-1][0] == "activate"
    assert app.calls[-1][3]["current_input_daily_version"] == 4
    assert app.calls[-1][3]["now"].tzinfo is not None


@pytest.mark.anyio
async def test_qfq_handler_duplicate_delivery_returns_without_provider_call(
    monkeypatch,
) -> None:
    run_id = uuid4()
    app = FakeQfqApplication(
        SimpleNamespace(
            status="SUCCEEDED",
            activated_dataset_id=uuid4(),
            row_count=2,
            checksum="a" * 64,
        )
    )
    app, _daily, provider, database = wire(monkeypatch, app=app)

    result = await qfq_refresh(context(refresh_run_id=str(run_id)))

    assert result.success is True
    assert result.data["replayed"] is True
    assert provider.calls == []
    assert database.session_calls == 0
    assert [call[0] for call in app.calls] == ["begin_fetch"]


@pytest.mark.anyio
@pytest.mark.parametrize("status", ["FAILED", "TIMED_OUT", "SUPERSEDED"])
async def test_qfq_handler_terminal_failure_replay_never_calls_provider(
    monkeypatch, status
) -> None:
    app = FakeQfqApplication(
        SimpleNamespace(
            status=status,
            error_code="QFQ_PROVIDER_FAILED",
            retryable=True,
        )
    )
    app, _daily, provider, database = wire(monkeypatch, app=app)

    result = await qfq_refresh(context())

    assert result.success is False
    assert result.code == "QFQ_PROVIDER_FAILED"
    assert result.retryable is True
    assert result.data["replayed"] is True
    assert provider.calls == []
    assert database.session_calls == 0
    assert [call[0] for call in app.calls] == ["begin_fetch"]


@pytest.mark.anyio
async def test_qfq_handler_rejects_daily_snapshot_for_other_security(
    monkeypatch,
) -> None:
    daily = FakeDailyApplication()
    daily._snapshot = SimpleNamespace(
        security_id=uuid4(),
        symbol="600000.SH",
        trade_date=END,
        close=Decimal("10.500000"),
        data_version=3,
    )
    app, _daily, _provider, _database = wire(monkeypatch, daily=daily)

    result = await qfq_refresh(context())

    assert result.code == "QFQ_DAILY_GATE_NOT_MET"
    assert app.calls[-1][0] == "fail"
    assert all(call[0] != "activate" for call in app.calls)


def test_qfq_handler_is_registered_on_the_isolated_job_entrypoint() -> None:
    from long_invest.entrypoints import job_worker

    assert job_worker.HANDLERS["QFQ_REFRESH"] is qfq_refresh
