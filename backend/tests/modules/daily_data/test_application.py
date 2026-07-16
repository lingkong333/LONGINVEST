import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from functools import wraps
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from long_invest.modules.daily_data.application import DailyDataApplication
from long_invest.modules.daily_data.contracts import DailyRetryAuditContext
from long_invest.platform.errors import AppError


def async_test(function):
    @wraps(function)
    def run(*args, **kwargs):
        return asyncio.run(function(*args, **kwargs))

    return run


class FakeSession:
    def __init__(self) -> None:
        self.begin_nested_calls = 0

    @asynccontextmanager
    async def begin_nested(self):
        self.begin_nested_calls += 1
        yield


class FakeDatabase:
    def __init__(self) -> None:
        self.session = FakeSession()
        self.state = {"jobs": [], "audits": []}
        self.rolled_back = False

    @asynccontextmanager
    async def transaction(self):
        before = deepcopy(self.state)
        try:
            yield self.session
        except Exception:
            self.state = before
            self.rolled_back = True
            raise


class FakeRepository:
    batch = SimpleNamespace(
        id=uuid4(),
        universe_snapshot_id=uuid4(),
        trading_date=date(2026, 7, 15),
        known_corporate_action_symbols=["600000.SH", "300001.SZ"],
    )

    def __init__(self, session) -> None:
        self.session = session

    async def get_batch(self, batch_id):
        return self.batch if batch_id == self.batch.id else None


class FakeDomainService:
    def __init__(self, repository) -> None:
        self.repository = repository

    async def retry_scope(self, batch_id):
        assert batch_id == FakeRepository.batch.id
        return ("600000.SH", "000001.SZ")


class RecordingJobService:
    def __init__(self, session, database) -> None:
        self.session = session
        self.database = database
        database.job_session = session

    async def submit(self, command):
        if self.database.state["jobs"]:
            return self.database.job
        self.database.state["jobs"].append(command)
        self.database.job = SimpleNamespace(
            id=uuid4(), job_type=command.job_type, status="PENDING_DISPATCH"
        )
        return self.database.job


class RecordingAuditService:
    def __init__(self, session, database, *, fail=False) -> None:
        self.session = session
        self.database = database
        self.fail = fail
        database.audit_session = session

    async def find_by_idempotency(self, key):
        return next(
            (
                item
                for item in self.database.state["audits"]
                if item.idempotency_key == key
            ),
            None,
        )

    async def append(self, event):
        if self.fail:
            raise RuntimeError("forced audit failure")
        if await self.find_by_idempotency(event.idempotency_key) is not None:
            raise IntegrityError("insert audit", {}, RuntimeError("unique"))
        self.database.state["audits"].append(event)
        return event


class RacingAuditService(RecordingAuditService):
    def __init__(self, session, database, *, different=False) -> None:
        super().__init__(session, database)
        self.different = different

    async def append(self, event):
        concurrent = (
            replace(event, reason="different reason") if self.different else event
        )
        self.database.state["audits"].append(concurrent)
        raise IntegrityError("insert audit", {}, RuntimeError("unique"))


def _context(key="retry-key"):
    return DailyRetryAuditContext(
        request_id="req_12345678",
        idempotency_key=key,
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
        reason="manual retry",
    )


def _application(database, *, audit_fail=False):
    return DailyDataApplication(
        database,
        repository_factory=FakeRepository,
        domain_service_factory=FakeDomainService,
        job_service_factory=lambda session: RecordingJobService(session, database),
        audit_service_factory=lambda session: RecordingAuditService(
            session, database, fail=audit_fail
        ),
    )


class SnapshotDatabase:
    def __init__(self) -> None:
        self.read_session = object()

    @asynccontextmanager
    async def session(self):
        yield self.read_session


class SnapshotRepository:
    bar = None
    error = None
    calls = []

    def __init__(self, session) -> None:
        self.session = session

    async def get_bar_by_symbol_date(self, symbol, trade_date):
        type(self).calls.append((self.session, symbol, trade_date))
        if self.error is not None:
            raise self.error
        return self.bar


def _snapshot_application(database):
    SnapshotRepository.calls = []
    SnapshotRepository.error = None
    SnapshotRepository.bar = None
    return DailyDataApplication(database, repository_factory=SnapshotRepository)


@async_test
async def test_snapshot_maps_internal_bar_to_public_contract() -> None:
    database = SnapshotDatabase()
    application = _snapshot_application(database)
    security_id = uuid4()
    updated_at = datetime(2026, 7, 15, 17, 1, tzinfo=UTC)
    SnapshotRepository.bar = SimpleNamespace(
        security_id=security_id,
        symbol="600000.SH",
        trade_date=date(2026, 7, 15),
        close=Decimal("10.123456789012345678"),
        data_version=3,
        source="EASTMONEY",
        updated_at=updated_at,
        internal_state="must not leak",
    )

    result = await application.snapshot("600000.SH", date(2026, 7, 15))

    assert result is not SnapshotRepository.bar
    assert result.security_id == security_id
    assert result.symbol == "600000.SH"
    assert result.trade_date == date(2026, 7, 15)
    assert result.close == Decimal("10.123456789012345678")
    assert result.data_version == 3
    assert result.source == "EASTMONEY"
    assert result.updated_at == updated_at
    assert not hasattr(result, "internal_state")
    assert SnapshotRepository.calls == [
        (database.read_session, "600000.SH", date(2026, 7, 15))
    ]


@async_test
async def test_snapshot_returns_none_when_bar_does_not_exist() -> None:
    application = _snapshot_application(SnapshotDatabase())

    assert await application.snapshot("600000.SH", date(2026, 7, 15)) is None


@pytest.mark.parametrize("symbol", ["invalid", None, 600000])
@async_test
async def test_snapshot_rejects_invalid_symbol_before_database_access(symbol) -> None:
    application = _snapshot_application(SnapshotDatabase())

    with pytest.raises(AppError) as captured:
        await application.snapshot(symbol, date(2026, 7, 15))

    assert captured.value.code == "DAILY_BAR_SYMBOL_INVALID"
    assert captured.value.status_code == 422
    assert SnapshotRepository.calls == []


@async_test
async def test_snapshot_maps_database_failure_to_service_unavailable() -> None:
    application = _snapshot_application(SnapshotDatabase())
    SnapshotRepository.error = SQLAlchemyError("database unavailable")

    with pytest.raises(AppError) as captured:
        await application.snapshot("600000.SH", date(2026, 7, 15))

    assert captured.value.code == "DAILY_DATA_BACKEND_UNAVAILABLE"
    assert captured.value.status_code == 503


@async_test
async def test_retry_job_and_audit_share_transaction_session() -> None:
    database = FakeDatabase()
    application = _application(database)

    await application.retry(
        batch_id=FakeRepository.batch.id,
        audit_context=_context(),
    )

    job = database.state["jobs"][0]
    audit = database.state["audits"][0]
    assert job.job_type == "DAILY_DATA_RETRY"
    assert job.config_snapshot["known_corporate_action_symbols"] == ["600000.SH"]
    assert audit.action_code == "daily_data.batch_retry_requested"
    assert audit.object_type == "daily_data_batch"
    assert audit.object_id == str(FakeRepository.batch.id)
    assert audit.risk_level == "HIGH"
    assert audit.actor_user_id == "user-1"
    assert audit.session_id == "session-1"
    assert audit.trusted_ip == "127.0.0.1"
    assert audit.after_summary == {
        "retry_symbols": ["600000.SH", "000001.SZ"],
        "trading_date": "2026-07-15",
    }
    assert database.job_session is database.session
    assert database.audit_session is database.session


@async_test
async def test_retry_replay_does_not_duplicate_audit() -> None:
    database = FakeDatabase()
    application = _application(database)

    first = await application.retry(
        batch_id=FakeRepository.batch.id, audit_context=_context()
    )
    second = await application.retry(
        batch_id=FakeRepository.batch.id, audit_context=_context()
    )

    assert first is second
    assert len(database.state["jobs"]) == 1
    assert len(database.state["audits"]) == 1


@async_test
async def test_concurrent_audit_replay_uses_savepoint_and_same_job() -> None:
    database = FakeDatabase()
    application = DailyDataApplication(
        database,
        repository_factory=FakeRepository,
        domain_service_factory=FakeDomainService,
        job_service_factory=lambda session: RecordingJobService(session, database),
        audit_service_factory=lambda session: RacingAuditService(session, database),
    )

    job = await application.retry(
        batch_id=FakeRepository.batch.id,
        audit_context=_context(),
    )

    assert job is database.job
    assert len(database.state["jobs"]) == 1
    assert len(database.state["audits"]) == 1
    assert database.session.begin_nested_calls == 1
    assert database.rolled_back is False


@async_test
async def test_concurrent_audit_with_different_content_is_conflict_not_503() -> None:
    database = FakeDatabase()
    application = DailyDataApplication(
        database,
        repository_factory=FakeRepository,
        domain_service_factory=FakeDomainService,
        job_service_factory=lambda session: RecordingJobService(session, database),
        audit_service_factory=lambda session: RacingAuditService(
            session, database, different=True
        ),
    )

    with pytest.raises(AppError) as captured:
        await application.retry(
            batch_id=FakeRepository.batch.id,
            audit_context=_context(),
        )

    assert captured.value.code == "DAILY_RETRY_AUDIT_CONFLICT"
    assert captured.value.status_code == 409


@async_test
async def test_audit_failure_rolls_back_job_and_propagates() -> None:
    database = FakeDatabase()
    application = _application(database, audit_fail=True)

    with pytest.raises(RuntimeError, match="forced audit failure"):
        await application.retry(
            batch_id=FakeRepository.batch.id,
            audit_context=_context(),
        )

    assert database.rolled_back is True
    assert database.state == {"jobs": [], "audits": []}


@async_test
async def test_same_job_key_with_different_reason_keeps_job_conflict() -> None:
    database = FakeDatabase()

    class ConflictingJobService(RecordingJobService):
        async def submit(self, command):
            existing = self.database.state["jobs"]
            if existing and existing[0].config_snapshot != command.config_snapshot:
                raise AppError(
                    code="IDEMPOTENCY_KEY_REUSED",
                    message="幂等键已用于不同任务",
                    status_code=409,
                )
            return await super().submit(command)

    application = DailyDataApplication(
        database,
        repository_factory=FakeRepository,
        domain_service_factory=FakeDomainService,
        job_service_factory=lambda session: ConflictingJobService(session, database),
        audit_service_factory=lambda session: RecordingAuditService(session, database),
    )
    await application.retry(
        batch_id=FakeRepository.batch.id,
        audit_context=_context(),
    )

    with pytest.raises(AppError) as captured:
        await application.retry(
            batch_id=FakeRepository.batch.id,
            audit_context=_context().__class__(
                request_id="req_87654321",
                idempotency_key="retry-key",
                actor_user_id="user-1",
                session_id="session-1",
                trusted_ip="127.0.0.1",
                reason="different reason",
            ),
        )

    assert captured.value.code == "IDEMPOTENCY_KEY_REUSED"
    assert len(database.state["audits"]) == 1


@async_test
async def test_retry_state_conflict_does_not_create_job_or_audit() -> None:
    database = FakeDatabase()

    class RejectingDomainService(FakeDomainService):
        async def retry_scope(self, batch_id):
            raise AppError(
                code="DAILY_RETRY_STATE_CONFLICT",
                message="batch is not retryable",
                status_code=409,
                details={"status": "FETCHING"},
            )

    application = DailyDataApplication(
        database,
        repository_factory=FakeRepository,
        domain_service_factory=RejectingDomainService,
        job_service_factory=lambda session: RecordingJobService(session, database),
        audit_service_factory=lambda session: RecordingAuditService(session, database),
    )

    with pytest.raises(AppError) as captured:
        await application.retry(
            batch_id=FakeRepository.batch.id,
            audit_context=_context(),
        )

    assert captured.value.code == "DAILY_RETRY_STATE_CONFLICT"
    assert captured.value.status_code == 409
    assert database.state == {"jobs": [], "audits": []}
    assert database.rolled_back is True


@async_test
async def test_empty_retry_scope_does_not_create_job_or_audit() -> None:
    database = FakeDatabase()

    class EmptyDomainService(FakeDomainService):
        async def retry_scope(self, batch_id):
            return ()

    application = DailyDataApplication(
        database,
        repository_factory=FakeRepository,
        domain_service_factory=EmptyDomainService,
        job_service_factory=lambda session: RecordingJobService(session, database),
        audit_service_factory=lambda session: RecordingAuditService(session, database),
    )

    with pytest.raises(AppError) as captured:
        await application.retry(
            batch_id=FakeRepository.batch.id,
            audit_context=_context(),
        )

    assert captured.value.code == "DAILY_RETRY_SCOPE_EMPTY"
    assert captured.value.status_code == 409
    assert database.state == {"jobs": [], "audits": []}
