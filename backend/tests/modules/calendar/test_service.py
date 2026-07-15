from copy import deepcopy
from datetime import date, time, timedelta
from uuid import UUID

import pytest
from sqlalchemy.exc import IntegrityError

from long_invest.modules.calendar.contracts import (
    CalendarAuditContext,
    CalendarDayInput,
    CalendarDayStatus,
    CalendarImport,
    OverrideCalendarDay,
    RestoreCalendarVersion,
    TradingSessionInput,
)
from long_invest.modules.calendar.models import TradingCalendarCurrent
from long_invest.modules.calendar.service import TradingCalendarService
from long_invest.platform.errors import AppError

TODAY = date(2026, 7, 15)


def trading_day(value: date, status=CalendarDayStatus.CONFIRMED) -> CalendarDayInput:
    return CalendarDayInput(
        trade_date=value,
        is_trading_day=True,
        status=status,
        sessions=(TradingSessionInput(starts_at=time(9, 30), ends_at=time(15)),),
    )


class FakeRepository:
    def __init__(self) -> None:
        self.current: TradingCalendarCurrent | None = None
        self.versions = {}
        self.idempotency = {}
        self.add_calls = 0
        self.allow_switch = True
        self.raise_integrity = False
        self.raise_switch_integrity = False

    async def find_by_idempotency(self, market, key):
        return self.idempotency.get((market, key))

    async def get_current(self, _market):
        return self.current

    async def next_version_number(self, _market):
        return len(self.versions) + 1

    async def add_version(self, version):
        if self.raise_integrity:
            raise IntegrityError("insert version", {}, Exception("unique"))
        self.add_calls += 1
        self.versions[version.id] = version
        self.idempotency[(version.market, version.idempotency_key)] = version

    async def switch_current(self, *, market, version_id, expected_pointer_version):
        if self.raise_switch_integrity:
            raise IntegrityError("insert current", {}, Exception("unique"))
        if not self.allow_switch:
            return False
        pointer = (
            1
            if expected_pointer_version is None
            else expected_pointer_version + 1
        )
        self.current = TradingCalendarCurrent(
            market=market, version_id=version_id, pointer_version=pointer
        )
        return True

    async def get_version(self, version_id):
        return self.versions.get(version_id)

    async def get_day(self, _market, wanted):
        if self.current is None:
            return None
        version = self.versions[self.current.version_id]
        return next((day for day in version.days if day.trade_date == wanted), None)

    async def confirmed_through(self, _market, from_date):
        if self.current is None:
            return None
        dates = [
            item.trade_date
            for item in self.versions[self.current.version_id].days
            if item.trade_date >= from_date
            and item.status
            in (CalendarDayStatus.CONFIRMED, CalendarDayStatus.OVERRIDDEN)
        ]
        return max(dates, default=None)

    async def list_days(self, market, from_date, through_date):
        if self.current is None:
            return []
        return [
            item
            for item in self.versions[self.current.version_id].days
            if from_date <= item.trade_date <= through_date
        ]

    async def next_trading_day(self, market, after_date):
        items = await self.list_days(market, after_date, date.max)
        return next((item for item in items if item.trade_date > after_date), None)

    async def previous_trading_day(self, market, before_date):
        items = await self.list_days(market, date.min, before_date)
        eligible = [item for item in items if item.trade_date < before_date]
        return eligible[-1] if eligible else None

    async def list_versions(self, market):
        return [item for item in self.versions.values() if item.market == market]


class Recorder:
    def __init__(self) -> None:
        self.items = []

    async def append(self, value):
        self.items.append(value)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def audit_context(key: str) -> CalendarAuditContext:
    return CalendarAuditContext(
        request_id=f"req-{key}",
        idempotency_key=key,
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )


def import_command(
    *days,
    key="key",
    expected=None,
    fill_coverage=True,
) -> CalendarImport:
    values = list(days)
    if fill_coverage:
        existing = {item.trade_date for item in values}
        for offset in range(31):
            wanted = TODAY + timedelta(days=offset)
            if wanted not in existing:
                values.append(
                    CalendarDayInput(
                        trade_date=wanted,
                        is_trading_day=False,
                        status=CalendarDayStatus.CONFIRMED,
                        sessions=(),
                    )
                )
        values.sort(key=lambda item: item.trade_date)
    return CalendarImport(
        market="CN_A",
        source="git",
        source_version=key,
        idempotency_key=key,
        expected_current_version=expected,
        days=tuple(values),
        audit_context=audit_context(key),
    )


def service_for(
    repository: FakeRepository,
    *,
    events: Recorder | None = None,
    audits: Recorder | None = None,
) -> TradingCalendarService:
    return TradingCalendarService(
        repository,
        audit_service=audits or Recorder(),
        event_sink=events or Recorder(),
        today_provider=lambda: TODAY,
    )


@pytest.mark.anyio
async def test_invalid_import_returns_every_issue_and_writes_nothing() -> None:
    repository = FakeRepository()
    service = service_for(repository)
    invalid = import_command(
        CalendarDayInput(
            trade_date=date(2026, 7, 15),
            is_trading_day=True,
            status=CalendarDayStatus.CONFIRMED,
            sessions=(),
        ),
        CalendarDayInput(
            trade_date=date(2026, 7, 15),
            is_trading_day=False,
            status=CalendarDayStatus.CONFIRMED,
            sessions=(TradingSessionInput(starts_at=time(9), ends_at=time(10)),),
        ),
        fill_coverage=False,
    )

    result = await service.import_version(invalid)

    assert len(result.issues) >= 3
    assert {
        "CALENDAR_DATE_DUPLICATE",
        "CALENDAR_TRADING_DAY_SESSIONS_REQUIRED",
        "CALENDAR_NON_TRADING_DAY_HAS_SESSIONS",
    } <= {item.code for item in result.issues}
    assert repository.add_calls == 0
    assert repository.current is None


@pytest.mark.anyio
async def test_import_is_idempotent_and_rejects_key_reuse_with_other_content() -> None:
    repository = FakeRepository()
    service = service_for(repository)
    command = import_command(trading_day(date(2026, 7, 15)))

    first = await service.import_version(command)
    replay = await service.import_version(command)

    assert first.created is True
    assert replay.created is False
    assert replay.version_id == first.version_id
    assert repository.add_calls == 1
    with pytest.raises(AppError) as caught:
        await service.import_version(
            import_command(trading_day(date(2026, 7, 16)))
        )
    assert caught.value.status_code == 409
    assert caught.value.code == "CALENDAR_IDEMPOTENCY_CONFLICT"


@pytest.mark.anyio
@pytest.mark.parametrize(
    "days, expected_code",
    [
        (
            tuple(trading_day(TODAY + timedelta(days=value)) for value in range(29)),
            "CALENDAR_COVERAGE_GAP",
        ),
        (
            tuple(
                trading_day(TODAY + timedelta(days=value))
                for value in range(1, 31)
            ),
            "CALENDAR_TODAY_MISSING",
        ),
        (
            (
                trading_day(TODAY, CalendarDayStatus.PROVISIONAL),
                *tuple(
                    trading_day(TODAY + timedelta(days=value))
                    for value in range(1, 30)
                ),
            ),
            "CALENDAR_TODAY_MISSING",
        ),
        (
            (
                trading_day(TODAY, CalendarDayStatus.MISSING),
                *tuple(
                    trading_day(TODAY + timedelta(days=value))
                    for value in range(1, 31)
                ),
            ),
            "CALENDAR_TODAY_MISSING",
        ),
    ],
)
async def test_import_atomically_rejects_low_or_missing_today_coverage(
    days, expected_code
) -> None:
    repository = FakeRepository()
    service = service_for(repository)

    result = await service.import_version(
        import_command(*days, fill_coverage=False)
    )

    assert expected_code in {item.code for item in result.issues}
    assert repository.add_calls == 0
    assert repository.current is None


@pytest.mark.anyio
async def test_write_fails_closed_before_persistence_without_audit_or_events() -> None:
    repository = FakeRepository()
    service = TradingCalendarService(repository, today_provider=lambda: TODAY)

    with pytest.raises(AppError) as caught:
        await service.import_version(import_command(trading_day(TODAY)))

    assert caught.value.code == "CALENDAR_TRANSACTION_PORT_UNAVAILABLE"
    assert caught.value.status_code == 503
    assert repository.add_calls == 0


@pytest.mark.anyio
async def test_write_records_the_complete_supplied_audit_context() -> None:
    repository = FakeRepository()
    audits = Recorder()
    service = service_for(repository, audits=audits)

    await service.import_version(import_command(trading_day(TODAY), key="audit"))

    record = audits.items[0]
    assert record.request_id == "req-audit"
    assert record.actor_user_id == "user-1"
    assert record.session_id == "session-1"
    assert record.trusted_ip == "127.0.0.1"
    assert record.idempotency_key.startswith("calendar:")


@pytest.mark.anyio
async def test_low_valid_coverage_returns_warning_and_reliable_event() -> None:
    repository = FakeRepository()
    events = Recorder()
    service = service_for(repository, events=events)

    result = await service.import_version(import_command(trading_day(TODAY)))

    assert [item.code for item in result.warnings] == [
        "CALENDAR_COVERAGE_LOW"
    ]
    assert "trading_calendar.coverage_low" in {
        item.event_type for item in events.items
    }


@pytest.mark.anyio
async def test_concurrent_version_number_collision_is_stable_409() -> None:
    repository = FakeRepository()
    repository.raise_integrity = True
    service = service_for(repository)

    with pytest.raises(AppError) as caught:
        await service.import_version(import_command(trading_day(TODAY)))

    assert caught.value.code == "CALENDAR_OPTIMISTIC_LOCK_CONFLICT"
    assert caught.value.status_code == 409


@pytest.mark.anyio
async def test_concurrent_initial_pointer_collision_is_stable_409() -> None:
    repository = FakeRepository()
    repository.raise_switch_integrity = True
    service = service_for(repository)

    with pytest.raises(AppError) as caught:
        await service.import_version(import_command(trading_day(TODAY)))

    assert caught.value.code == "CALENDAR_OPTIMISTIC_LOCK_CONFLICT"
    assert caught.value.status_code == 409


@pytest.mark.anyio
async def test_coverage_levels_and_missing_today_block_automatic_trading() -> None:
    repository = FakeRepository()
    events = Recorder()
    service = service_for(repository, events=events)
    await service.import_version(import_command(trading_day(TODAY)))
    version = repository.versions[repository.current.version_id]
    gap = TODAY + timedelta(days=2)
    version.days = [item for item in version.days if item.trade_date != gap]

    coverage = await service.coverage(TODAY)

    assert coverage.level == "ERROR"
    assert coverage.future_confirmed_days == 1
    assert coverage.missing_today is False
    assert await service.is_automatic_trading_day(TODAY) is True
    assert {item.event_type for item in events.items} >= {
        "trading_calendar.updated",
        "trading_calendar.coverage_low",
    }


@pytest.mark.anyio
async def test_override_creates_new_version_without_changing_old_version() -> None:
    repository = FakeRepository()
    service = service_for(repository)
    imported = await service.import_version(
        import_command(trading_day(date(2026, 7, 15)))
    )
    old_version = deepcopy(repository.versions[imported.version_id])

    result = await service.override_day(
        OverrideCalendarDay(
            market="CN_A",
            trade_date=date(2026, 7, 15),
            is_trading_day=False,
            sessions=(),
            expected_current_version=1,
            reason="临时休市",
            idempotency_key="override-1",
            audit_context=audit_context("override-1"),
        )
    )

    assert result.created is True
    assert repository.versions[result.version_id].days[0].status == "OVERRIDDEN"
    assert old_version.days[0].is_trading_day is True
    assert repository.versions[imported.version_id].days[0].is_trading_day is True
    changed = repository.versions[result.version_id]
    assert next(item for item in changed.days if item.trade_date == TODAY).source == (
        "manual_override"
    )
    assert all(
        item.source == "git" for item in changed.days if item.trade_date != TODAY
    )


@pytest.mark.anyio
async def test_stale_override_conflicts_and_restore_creates_a_new_fact() -> None:
    repository = FakeRepository()
    service = service_for(repository)
    imported = await service.import_version(
        import_command(trading_day(date(2026, 7, 15)))
    )
    repository.allow_switch = False
    with pytest.raises(AppError) as caught:
        await service.override_day(
            OverrideCalendarDay(
                market="CN_A",
                trade_date=date(2026, 7, 15),
                is_trading_day=False,
                sessions=(),
                expected_current_version=1,
                reason="冲突测试",
                idempotency_key="stale",
                audit_context=audit_context("stale"),
            )
        )
    assert caught.value.code == "CALENDAR_OPTIMISTIC_LOCK_CONFLICT"

    repository.allow_switch = True
    repository.current.pointer_version = 1
    restored = await service.restore_version(
        RestoreCalendarVersion(
            market="CN_A",
            version_id=UUID(str(imported.version_id)),
            expected_current_version=1,
            reason="恢复基线",
            idempotency_key="restore-1",
            audit_context=audit_context("restore-1"),
        )
    )
    assert restored.version_id != imported.version_id
    assert (
        repository.versions[restored.version_id].based_on_version_id
        == imported.version_id
    )


@pytest.mark.anyio
async def test_public_queries_read_only_through_the_calendar_repository() -> None:
    repository = FakeRepository()
    service = service_for(repository)
    wanted = date(2026, 7, 15)
    await service.import_version(import_command(trading_day(wanted)))

    assert (await service.get_day(wanted)).trade_date == wanted
    assert await service.list_days(wanted, wanted)
    assert (await service.next_trading_day(date(2026, 7, 14))).trade_date == wanted
    assert (await service.previous_trading_day(date(2026, 7, 16))).trade_date == wanted
    assert await service.list_versions()
