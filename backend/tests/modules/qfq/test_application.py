import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.calendar.contracts import TradingDateWindow
from long_invest.modules.daily_data.contracts import DailyBarSnapshot
from long_invest.modules.qfq.application import QfqApplication
from long_invest.modules.qfq.contracts import (
    QfqBarInput,
    QfqFreshness,
    QfqRefreshStatus,
    ValidatedQfqWindow,
)
from long_invest.modules.securities.contracts import ListingStatus, SecurityIdentity
from long_invest.platform.errors import AppError

TODAY = date(2026, 7, 16)
NOW = datetime(2026, 7, 16, 8, tzinfo=UTC)
DEFAULT = object()


class FakeSession:
    def __init__(self, database) -> None:
        self.database = database

    @asynccontextmanager
    async def begin_nested(self):
        yield


class FakeDatabase:
    def __init__(self) -> None:
        self.state = {"jobs": [], "runs": [], "audits": []}
        self.job_objects = {}
        self.request_locks = []
        self.read_session = FakeSession(self)
        self.write_session = FakeSession(self)
        self.rolled_back = False

    @asynccontextmanager
    async def session(self):
        yield self.read_session

    @asynccontextmanager
    async def transaction(self):
        before = deepcopy(self.state)
        try:
            yield self.write_session
        except Exception:
            self.state = before
            self.rolled_back = True
            raise


class FakeSecurityApplication:
    def __init__(self, identity) -> None:
        self.identity = identity
        self.calls = []

    async def resolve_identity(self, symbol):
        self.calls.append(symbol)
        if isinstance(self.identity, Exception):
            raise self.identity
        return self.identity


class FakeCalendarApplication:
    def __init__(self, window) -> None:
        self.window = window
        self.calls = []

    async def trading_dates(self, start, end, market="CN_A"):
        self.calls.append((start, end, market))
        if isinstance(self.window, Exception):
            raise self.window
        return self.window


class FakeDailyApplication:
    def __init__(self, snapshot) -> None:
        self.value = snapshot
        self.calls = []

    async def snapshot(self, symbol, trade_date):
        self.calls.append((symbol, trade_date))
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class FakeRepository:
    current = None
    bars = []
    total = 0
    calls = []

    def __init__(self, session) -> None:
        self.session = session

    async def lock_request(self, security_id, request_hash):
        self.session.database.request_locks.append((security_id, request_hash))

    async def find_run_by_request_hash(self, security_id, request_hash):
        return next(
            (
                item
                for item in self.session.database.state["runs"]
                if item.security_id == security_id and item.request_hash == request_hash
            ),
            None,
        )

    async def current_dataset(self, security_id):
        type(self).calls.append(("current", self.session, security_id))
        return self.current

    async def list_current_bars(self, dataset_id, *, start, end, page, page_size):
        type(self).calls.append(
            ("list", self.session, dataset_id, start, end, page, page_size)
        )
        return self.bars

    async def count_current_bars(self, dataset_id, *, start, end):
        type(self).calls.append(("count", self.session, dataset_id, start, end))
        return self.total

    async def claim_run(self, candidate):
        existing = next(
            (
                item
                for item in self.session.database.state["runs"]
                if item.id == candidate.id
            ),
            None,
        )
        if existing is not None:
            return existing, False
        self.session.database.state["runs"].append(candidate)
        return candidate, True

    async def get_run(self, run_id, *, for_update=False):
        return next(
            (item for item in self.session.database.state["runs"] if item.id == run_id),
            None,
        )

    async def transition_run(self, run_id, *, expected_status, status, **changes):
        run = await self.get_run(run_id)
        if run is None or str(run.status) != str(expected_status):
            raise AppError(
                code="QFQ_REFRESH_CONFLICT", message="conflict", status_code=409
            )
        run.status = status
        for key, value in changes.items():
            setattr(run, key, value)
        return run


class FakeJobService:
    error = None

    def __init__(self, session, database) -> None:
        self.session = session
        self.database = database

    async def submit(self, command):
        if self.error is not None:
            raise self.error
        self.database.state["jobs"].append(command)
        request_hash = hashlib.sha256(
            json.dumps(
                command.config_snapshot,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        job = SimpleNamespace(
            id=uuid4(),
            job_type=command.job_type,
            status="PENDING_DISPATCH",
            request_hash=request_hash,
        )
        self.database.job_objects[job.id] = job
        return job

    async def get(self, job_id):
        return self.database.job_objects.get(job_id)


class FakeAuditService:
    error = None

    def __init__(self, session, database) -> None:
        self.session = session
        self.database = database

    async def find_by_idempotency(self, key):
        return next(
            (
                item
                for item in self.database.state["audits"]
                if item.idempotency_key == key
            ),
            None,
        )

    async def append(self, item):
        if self.error is not None:
            raise self.error
        self.database.state["audits"].append(item)
        return item


class FakeEventAdapter:
    def __init__(self, session) -> None:
        self.session = session


class FakeDomainService:
    calls = []

    def __init__(self, repository, *, events) -> None:
        assert repository.session is events.session
        self.session = repository.session

    async def activate(self, run_id, window, **kwargs):
        type(self).calls.append(("activate", self.session, run_id, window, kwargs))
        return "activated"

    async def fail(self, run_id, **kwargs):
        type(self).calls.append(("fail", self.session, run_id, kwargs))
        return "failed"


@pytest.fixture(autouse=True)
def reset_fakes() -> None:
    FakeRepository.current = None
    FakeRepository.bars = []
    FakeRepository.total = 0
    FakeRepository.calls = []
    FakeJobService.error = None
    FakeAuditService.error = None
    FakeDomainService.calls = []


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def identity() -> SecurityIdentity:
    return SecurityIdentity(
        security_id=uuid4(),
        symbol="600000.SH",
        listing_status=ListingStatus.LISTED,
        is_suspended=False,
        is_st=False,
        listed_on=date(1999, 11, 10),
        delisted_on=None,
        master_version=8,
    )


def calendar_window(*, dates=(date(2026, 7, 15), TODAY)) -> TradingDateWindow:
    return TradingDateWindow(
        market="CN_A",
        start=date(2026, 7, 14),
        end=TODAY,
        version_id=uuid4(),
        version_number=6,
        dates=dates,
    )


def daily_snapshot(security: SecurityIdentity) -> DailyBarSnapshot:
    return DailyBarSnapshot(
        security_id=security.security_id,
        symbol=security.symbol,
        trade_date=TODAY,
        close=Decimal("10.25"),
        data_version=11,
        source="eastmoney",
        updated_at=NOW,
    )


def application(
    database: FakeDatabase,
    *,
    security=DEFAULT,
    calendar=DEFAULT,
    daily=DEFAULT,
) -> QfqApplication:
    security = identity() if security is DEFAULT else security
    return QfqApplication(
        database,
        security_application=FakeSecurityApplication(security),
        calendar_application=FakeCalendarApplication(
            calendar_window() if calendar is DEFAULT else calendar
        ),
        daily_application=FakeDailyApplication(
            daily_snapshot(security) if daily is DEFAULT else daily
        ),
        repository_factory=FakeRepository,
        job_service_factory=lambda session: FakeJobService(session, database),
        audit_service_factory=lambda session: FakeAuditService(session, database),
        event_factory=FakeEventAdapter,
        domain_service_factory=FakeDomainService,
    )


@pytest.mark.anyio
async def test_submit_freezes_inputs_and_audits_atomically() -> None:
    database = FakeDatabase()
    security = identity()
    app = application(database, security=security, daily=daily_snapshot(security))

    job = await app.submit_refresh(
        symbol=security.symbol,
        start=date(2026, 7, 14),
        end=TODAY,
        as_of_date=TODAY,
        reason="manual refresh",
        idempotency_key="qfq-1",
        request_id="req-qfq-1",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )

    command = database.state["jobs"][0]
    assert job.job_type == "QFQ_REFRESH"
    assert command.queue == "qfq-refresh"
    assert command.idempotency_scope == f"qfq-refresh:{security.security_id}"
    assert command.soft_timeout_seconds == 240
    assert command.hard_timeout_seconds == 300
    assert command.config_snapshot["expected_trade_dates"] == [
        "2026-07-15",
        "2026-07-16",
    ]
    assert command.config_snapshot["input_daily_version"] == 11
    assert command.config_snapshot["security_master_version"] == 8
    assert command.config_snapshot["calendar_version_number"] == 6
    run = database.state["runs"][0]
    assert command.config_snapshot["refresh_run_id"] == str(run.id)
    assert run.job_id == job.id
    assert str(run.status) == "PENDING"
    assert len(run.request_hash) == 64
    audit = database.state["audits"][0]
    assert audit.action_code == "qfq.refresh_requested"
    assert audit.risk_level == "HIGH"
    assert audit.object_id == str(security.security_id)


@pytest.mark.anyio
async def test_submit_refresh_rejects_empty_calendar_and_missing_daily_gate() -> None:
    database = FakeDatabase()
    with pytest.raises(AppError) as empty:
        await application(database, calendar=calendar_window(dates=())).submit_refresh(
            symbol="600000.SH",
            start=date(2026, 7, 14),
            end=TODAY,
            as_of_date=TODAY,
            reason="manual refresh",
            idempotency_key="qfq-empty",
            request_id="req-empty",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
        )
    assert empty.value.code == "QFQ_WINDOW_INVALID"

    security = identity()
    with pytest.raises(AppError) as missing:
        await application(database, security=security, daily=None).submit_refresh(
            symbol=security.symbol,
            start=date(2026, 7, 14),
            end=TODAY,
            as_of_date=TODAY,
            reason="manual refresh",
            idempotency_key="qfq-missing",
            request_id="req-missing",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
        )
    assert missing.value.code == "QFQ_DAILY_GATE_NOT_MET"
    assert database.state["jobs"] == []


@pytest.mark.anyio
async def test_submit_maps_conflict_and_rolls_back_audit_failure() -> None:
    database = FakeDatabase()
    FakeJobService.error = AppError(
        code="IDEMPOTENCY_KEY_REUSED",
        message="different content",
        status_code=409,
    )
    with pytest.raises(AppError) as conflict:
        await application(database).submit_refresh(
            symbol="600000.SH",
            start=date(2026, 7, 14),
            end=TODAY,
            as_of_date=TODAY,
            reason="manual refresh",
            idempotency_key="qfq-conflict",
            request_id="req-conflict",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
        )
    assert conflict.value.code == "QFQ_REFRESH_CONFLICT"

    FakeJobService.error = None
    FakeAuditService.error = SQLAlchemyError("audit unavailable")
    with pytest.raises(AppError) as unavailable:
        await application(database).submit_refresh(
            symbol="600000.SH",
            start=date(2026, 7, 14),
            end=TODAY,
            as_of_date=TODAY,
            reason="manual refresh",
            idempotency_key="qfq-audit-fail",
            request_id="req-audit-fail",
            actor_user_id="user-1",
            session_id="session-1",
            trusted_ip="127.0.0.1",
        )
    assert unavailable.value.code == "QFQ_BACKEND_UNAVAILABLE"
    assert database.rolled_back is True
    assert database.state == {"jobs": [], "runs": [], "audits": []}


@pytest.mark.anyio
async def test_frozen_request_hash_deduplicates_content_across_idempotency_keys() -> (
    None
):
    security = identity()
    window = calendar_window()
    snapshot = daily_snapshot(security)
    first_database = FakeDatabase()
    second_database = FakeDatabase()
    first = application(
        first_database, security=security, calendar=window, daily=snapshot
    )
    second = application(
        second_database, security=security, calendar=window, daily=snapshot
    )

    common = {
        "symbol": security.symbol,
        "start": date(2026, 7, 14),
        "end": TODAY,
        "as_of_date": TODAY,
        "reason": "manual refresh",
        "request_id": "req-content",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
    }
    await first.submit_refresh(idempotency_key="key-one", **common)
    await second.submit_refresh(idempotency_key="key-two", **common)

    first_run = first_database.state["runs"][0]
    second_run = second_database.state["runs"][0]
    assert first_run.id != second_run.id
    assert first_run.request_hash == second_run.request_hash


@pytest.mark.anyio
async def test_concurrent_content_replay_returns_original_job() -> None:
    database = FakeDatabase()
    security = identity()
    window = calendar_window()
    snapshot = daily_snapshot(security)
    app = application(database, security=security, calendar=window, daily=snapshot)
    common = {
        "symbol": security.symbol,
        "start": date(2026, 7, 14),
        "end": TODAY,
        "as_of_date": TODAY,
        "reason": "manual refresh",
        "request_id": "req-concurrent",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
    }

    first, second = await asyncio.gather(
        app.submit_refresh(idempotency_key="key-one", **common),
        app.submit_refresh(idempotency_key="key-two", **common),
    )

    assert first.id == second.id
    assert len(database.state["jobs"]) == 1
    assert len(database.state["runs"]) == 1
    assert len(database.state["audits"]) == 1
    assert len(database.request_locks) == 2


@pytest.mark.anyio
async def test_content_replay_rejects_missing_linked_job() -> None:
    database = FakeDatabase()
    security = identity()
    window = calendar_window()
    snapshot = daily_snapshot(security)
    app = application(database, security=security, calendar=window, daily=snapshot)
    common = {
        "symbol": security.symbol,
        "start": date(2026, 7, 14),
        "end": TODAY,
        "as_of_date": TODAY,
        "reason": "manual refresh",
        "request_id": "req-missing-job",
        "actor_user_id": "user-1",
        "session_id": "session-1",
        "trusted_ip": "127.0.0.1",
    }
    await app.submit_refresh(idempotency_key="key-one", **common)
    database.job_objects.clear()

    with pytest.raises(AppError) as caught:
        await app.submit_refresh(idempotency_key="key-two", **common)

    assert caught.value.code == "QFQ_REFRESH_CONFLICT"
    assert caught.value.details["refresh_run_id"] == str(database.state["runs"][0].id)


@pytest.mark.anyio
async def test_get_data_returns_frozen_views_and_pagination() -> None:
    database = FakeDatabase()
    security = identity()
    dataset_id = uuid4()
    FakeRepository.current = SimpleNamespace(
        id=dataset_id,
        security_id=security.security_id,
        symbol=security.symbol,
        version=3,
        requested_start=date(2026, 7, 1),
        requested_end=TODAY,
        actual_start=date(2026, 7, 2),
        actual_end=TODAY,
        as_of_date=TODAY,
        provider="eastmoney",
        provider_contract_version="v1",
        anchor_date=TODAY,
        anchor_close=Decimal("10.25"),
        row_count=2,
        checksum="a" * 64,
        lifecycle="CURRENT",
        freshness="STALE",
        stale_reason="QFQ_PROVIDER_FAILED",
        created_at=NOW,
        activated_at=NOW,
        superseded_at=None,
    )
    FakeRepository.bars = [
        SimpleNamespace(
            trade_date=TODAY,
            open=Decimal("10.10"),
            high=Decimal("10.50"),
            low=Decimal("10.00"),
            close=Decimal("10.25"),
            volume=100,
            amount=Decimal("1025.0000"),
        )
    ]
    FakeRepository.total = 1

    dataset, page = await application(database, security=security).get_data(
        security.symbol,
        start=None,
        end=None,
        page=2,
        page_size=20,
    )

    assert dataset.id == dataset_id
    assert dataset.freshness is QfqFreshness.STALE
    assert dataset.anchor_close == "10.25"
    assert page.items[0].amount == "1025.0000"
    assert (page.page, page.page_size, page.total) == (2, 20, 1)
    assert FakeRepository.calls[1][3:5] == (
        date(2026, 7, 2),
        TODAY,
    )


@pytest.mark.anyio
async def test_get_data_rejects_invalid_window_and_missing_dataset() -> None:
    database = FakeDatabase()
    with pytest.raises(AppError) as invalid:
        await application(database).get_data(
            "600000.SH",
            start=TODAY,
            end=date(2026, 7, 15),
            page=1,
            page_size=50,
        )
    assert invalid.value.code == "QFQ_WINDOW_INVALID"
    assert FakeRepository.calls == []

    with pytest.raises(AppError) as missing:
        await application(database).get_data(
            "600000.SH", start=None, end=None, page=1, page_size=50
        )
    assert missing.value.code == "QFQ_DATA_NOT_FOUND"
    assert missing.value.status_code == 404


@pytest.mark.anyio
async def test_worker_activate_and_fail_use_public_transaction_boundaries() -> None:
    database = FakeDatabase()
    app = application(database)
    run_id = uuid4()
    bar = QfqBarInput(
        trade_date=TODAY,
        open=Decimal("10.10"),
        high=Decimal("10.50"),
        low=Decimal("10.00"),
        close=Decimal("10.25"),
        volume=100,
        amount=Decimal("1025.0000"),
    )
    window = ValidatedQfqWindow(
        bars=(bar,),
        anchor_date=TODAY,
        anchor_close=Decimal("10.25"),
        row_count=1,
        checksum="a" * 64,
    )

    activated = await app.activate(
        run_id,
        window,
        current_input_daily_version=11,
        provider_contract_version="eastmoney-v1",
        now=NOW,
    )
    failed = await app.fail(
        run_id,
        code="QFQ_PROVIDER_FAILED",
        retryable=True,
        now=NOW,
    )

    assert activated == "activated"
    assert failed == "failed"
    assert [item[0] for item in FakeDomainService.calls] == ["activate", "fail"]
    assert all(item[1] is database.write_session for item in FakeDomainService.calls)


@pytest.mark.anyio
async def test_worker_state_progression_is_transactional_and_replay_safe() -> None:
    database = FakeDatabase()
    app = application(database)
    job = await app.submit_refresh(
        symbol="600000.SH",
        start=date(2026, 7, 14),
        end=TODAY,
        as_of_date=TODAY,
        reason="manual refresh",
        idempotency_key="qfq-state",
        request_id="req-state",
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )
    run_id = next(item.id for item in database.state["runs"] if item.job_id == job.id)

    fetching = await app.begin_fetch(run_id, now=NOW)
    fetching_replay = await app.begin_fetch(run_id, now=NOW)
    validating = await app.begin_validation(run_id, now=NOW)
    late_fetch_replay = await app.begin_fetch(run_id, now=NOW)

    assert fetching.status is QfqRefreshStatus.FETCHING
    assert fetching_replay.status is QfqRefreshStatus.FETCHING
    assert validating.status is QfqRefreshStatus.VALIDATING
    assert late_fetch_replay.status is QfqRefreshStatus.VALIDATING
