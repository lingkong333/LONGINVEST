from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import date, datetime, timedelta
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from long_invest.modules.calendar.contracts import (
    SHANGHAI_TZ,
    CalendarAuditContext,
    CalendarCoverage,
    CalendarDayInput,
    CalendarDayStatus,
    CalendarEvent,
    CalendarEventSink,
    CalendarImport,
    CalendarValidationIssue,
    CalendarVersionResult,
    OverrideCalendarDay,
    RestoreCalendarVersion,
    TradingSessionInput,
    validate_calendar_coverage,
    validate_calendar_import,
)
from long_invest.modules.calendar.models import (
    TradingCalendarDay,
    TradingCalendarVersion,
    TradingSession,
)
from long_invest.modules.calendar.repository import CalendarRepository
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.errors import AppError


class TradingCalendarService:
    def __init__(
        self,
        repository: CalendarRepository,
        *,
        audit_service: AuditService | None = None,
        event_sink: CalendarEventSink | None = None,
        today_provider: Callable[[], date] | None = None,
    ) -> None:
        self._repository = repository
        self._audit = audit_service
        self._events = event_sink
        self._today = today_provider or (
            lambda: datetime.now(SHANGHAI_TZ).date()
        )

    async def get_day(self, trade_date: date, market: str = "CN_A"):
        return await self._repository.get_day(market, trade_date)

    async def list_days(
        self, from_date: date, through_date: date, market: str = "CN_A"
    ):
        return await self._repository.list_days(market, from_date, through_date)

    async def next_trading_day(self, after_date: date, market: str = "CN_A"):
        return await self._repository.next_trading_day(market, after_date)

    async def previous_trading_day(
        self, before_date: date, market: str = "CN_A"
    ):
        return await self._repository.previous_trading_day(market, before_date)

    async def list_versions(self, market: str = "CN_A"):
        return await self._repository.list_versions(market)

    async def import_version(
        self, command: CalendarImport
    ) -> CalendarVersionResult:
        self._require_write_ports(command.audit_context, command.idempotency_key)
        static_issues = validate_calendar_import(command)
        if static_issues:
            return CalendarVersionResult(issues=static_issues)
        content_hash = _command_hash(command)
        replay = await self._repository.find_by_idempotency(
            command.market, command.idempotency_key
        )
        if replay is not None:
            _verify_replay(replay, content_hash)
            return _result(replay, created=False)

        activation_issues = _activation_issues(command.days, self._today())
        if activation_issues:
            return CalendarVersionResult(issues=activation_issues)

        current = await self._repository.get_current(command.market)
        expected = current.pointer_version if current is not None else None
        if (
            command.expected_current_version is not None
            and command.expected_current_version != expected
        ):
            raise _optimistic_conflict()
        version = await self._build_version(
            command,
            content_hash=content_hash,
            based_on_version_id=current.version_id if current else None,
        )
        await self._activate(version, expected)
        await self._record_change(
            version,
            "TRADING_CALENDAR_IMPORT",
            command.reason,
            command.audit_context,
        )
        warnings = await self._coverage_warnings(version, command.audit_context)
        return _result(version, created=True, warnings=warnings)

    async def override_day(
        self, command: OverrideCalendarDay
    ) -> CalendarVersionResult:
        self._require_write_ports(command.audit_context, command.idempotency_key)
        desired_hash = _command_hash(command)
        replay = await self._repository.find_by_idempotency(
            command.market, command.idempotency_key
        )
        if replay is not None:
            _verify_replay(replay, desired_hash)
            return _result(replay, created=False)
        current = await self._required_current(command.market)
        if current.pointer_version != command.expected_current_version:
            raise _optimistic_conflict()
        base = await self._required_version(current.version_id)
        days = [_day_contract(item) for item in base.days]
        replacement = CalendarDayInput(
            trade_date=command.trade_date,
            is_trading_day=command.is_trading_day,
            status=CalendarDayStatus.OVERRIDDEN,
            sessions=command.sessions,
            note=command.note,
        )
        matches = [
            i
            for i, item in enumerate(days)
            if item.trade_date == command.trade_date
        ]
        if matches:
            days[matches[0]] = replacement
        else:
            days.append(replacement)
        days.sort(key=lambda item: item.trade_date)
        imported = CalendarImport(
            market=command.market,
            source="manual_override",
            source_version=command.idempotency_key,
            idempotency_key=command.idempotency_key,
            expected_current_version=command.expected_current_version,
            days=tuple(days),
            reason=command.reason,
            audit_context=command.audit_context,
        )
        issues = validate_calendar_import(imported)
        if issues:
            return CalendarVersionResult(issues=issues)
        version = await self._new_version(
            imported,
            content_hash=desired_hash,
            based_on_version_id=base.id,
        )
        version.days = []
        replaced = False
        for item in base.days:
            if item.trade_date == command.trade_date:
                version.days.append(
                    _day_model(
                        version.id,
                        replacement,
                        source="manual_override",
                        override_reason=command.reason,
                    )
                )
                replaced = True
            else:
                version.days.append(_clone_day(version.id, item))
        if not replaced:
            version.days.append(
                _day_model(
                    version.id,
                    replacement,
                    source="manual_override",
                    override_reason=command.reason,
                )
            )
        version.days.sort(key=lambda item: item.trade_date)
        await self._activate(version, command.expected_current_version)
        await self._record_change(
            version,
            "TRADING_CALENDAR_OVERRIDE",
            command.reason,
            command.audit_context,
        )
        warnings = await self._coverage_warnings(version, command.audit_context)
        return _result(version, created=True, warnings=warnings)

    async def restore_version(
        self, command: RestoreCalendarVersion
    ) -> CalendarVersionResult:
        self._require_write_ports(command.audit_context, command.idempotency_key)
        desired_hash = _command_hash(command)
        replay = await self._repository.find_by_idempotency(
            command.market, command.idempotency_key
        )
        if replay is not None:
            _verify_replay(replay, desired_hash)
            return _result(replay, created=False)
        current = await self._required_current(command.market)
        if current.pointer_version != command.expected_current_version:
            raise _optimistic_conflict()
        target = await self._required_version(command.version_id)
        if target.market != command.market:
            raise AppError(
                code="CALENDAR_VERSION_NOT_FOUND",
                message="日历版本不存在",
                status_code=404,
            )
        imported = CalendarImport(
            market=command.market,
            source="restore",
            source_version=command.idempotency_key,
            idempotency_key=command.idempotency_key,
            expected_current_version=command.expected_current_version,
            days=tuple(_day_contract(item) for item in target.days),
            reason=command.reason,
            audit_context=command.audit_context,
        )
        static_issues = validate_calendar_import(imported)
        if static_issues:
            return CalendarVersionResult(issues=static_issues)
        activation_issues = _activation_issues(imported.days, self._today())
        if activation_issues:
            return CalendarVersionResult(issues=activation_issues)
        version = await self._new_version(
            imported,
            content_hash=desired_hash,
            based_on_version_id=target.id,
        )
        version.days = [_clone_day(version.id, item) for item in target.days]
        await self._activate(version, command.expected_current_version)
        await self._record_change(
            version,
            "TRADING_CALENDAR_RESTORE",
            command.reason,
            command.audit_context,
        )
        warnings = await self._coverage_warnings(version, command.audit_context)
        return _result(version, created=True, warnings=warnings)

    async def is_automatic_trading_day(
        self, trade_date: date, market: str = "CN_A"
    ) -> bool:
        item = await self._repository.get_day(market, trade_date)
        return bool(
            item is not None
            and item.is_trading_day
            and item.status
            in (CalendarDayStatus.CONFIRMED, CalendarDayStatus.OVERRIDDEN)
        )

    async def coverage(
        self, from_date: date, market: str = "CN_A"
    ) -> CalendarCoverage:
        current = await self._repository.get_current(market)
        range_end = from_date + timedelta(days=60)
        calendar_days = await self._repository.list_days(
            market, from_date, range_end
        )
        by_date = {item.trade_date: item for item in calendar_days}
        through = None
        confirmed_records = 0
        for offset in range(61):
            wanted = from_date + timedelta(days=offset)
            item = by_date.get(wanted)
            if item is None or item.status not in {
                CalendarDayStatus.CONFIRMED,
                CalendarDayStatus.OVERRIDDEN,
            }:
                break
            confirmed_records += 1
            through = wanted
        days = max(0, confirmed_records - 1)
        level = "ERROR" if days < 30 else "WARNING" if days < 60 else "OK"
        today = by_date.get(from_date)
        missing = today is None or today.status not in {
            CalendarDayStatus.CONFIRMED,
            CalendarDayStatus.OVERRIDDEN,
        }
        aggregate_id = str(current.version_id) if current else market
        if level != "OK":
            await self._emit(
                "trading_calendar.coverage_low",
                aggregate_id,
                f"coverage:{market}:{from_date}:{level}",
                {"market": market, "days": days, "level": level},
            )
        if missing:
            await self._emit(
                "trading_calendar.missing",
                aggregate_id,
                f"missing:{market}:{from_date}",
                {"market": market, "date": from_date.isoformat()},
            )
        return CalendarCoverage(
            market=market,
            from_date=from_date,
            confirmed_through=through,
            future_confirmed_days=days,
            level=level,
            current_version_id=current.version_id if current else None,
            missing_today=missing,
        )

    async def _build_version(
        self,
        command: CalendarImport,
        *,
        content_hash: str,
        based_on_version_id: UUID | None,
    ) -> TradingCalendarVersion:
        version = await self._new_version(
            command,
            content_hash=content_hash,
            based_on_version_id=based_on_version_id,
        )
        version.days = [
            _day_model(
                version.id,
                item,
                source=command.source,
                override_reason=None,
            )
            for item in command.days
        ]
        return version

    async def _new_version(
        self,
        command: CalendarImport,
        *,
        content_hash: str,
        based_on_version_id: UUID | None,
    ) -> TradingCalendarVersion:
        version = TradingCalendarVersion(
            market=command.market,
            version_number=await self._repository.next_version_number(command.market),
            source=command.source,
            source_version=command.source_version,
            idempotency_key=command.idempotency_key,
            content_hash=content_hash,
            based_on_version_id=based_on_version_id,
            reason=command.reason,
        )
        return version

    async def _activate(
        self, version: TradingCalendarVersion, expected: int | None
    ) -> None:
        try:
            await self._repository.add_version(version)
            switched = await self._repository.switch_current(
                market=version.market,
                version_id=version.id,
                expected_pointer_version=expected,
            )
        except IntegrityError as exc:
            raise _optimistic_conflict() from exc
        if not switched:
            raise _optimistic_conflict()

    async def _required_current(self, market: str):
        current = await self._repository.get_current(market)
        if current is None:
            raise AppError(
                code="CALENDAR_CURRENT_NOT_FOUND",
                message="当前日历不存在",
                status_code=404,
            )
        return current

    async def _required_version(self, version_id: UUID) -> TradingCalendarVersion:
        version = await self._repository.get_version(version_id)
        if version is None:
            raise AppError(
                code="CALENDAR_VERSION_NOT_FOUND",
                message="日历版本不存在",
                status_code=404,
            )
        return version

    async def _record_change(
        self,
        version: TradingCalendarVersion,
        action_code: str,
        reason: str | None,
        context: CalendarAuditContext | None,
    ) -> None:
        self._require_write_ports(context, version.idempotency_key)
        assert self._audit is not None
        assert context is not None
        await self._audit.append(
            AuditWrite(
                action_code=action_code,
                object_type="trading_calendar_version",
                object_id=str(version.id),
                result="SUCCESS",
                request_id=context.request_id,
                idempotency_key=_audit_key(
                    action_code, context.idempotency_key
                ),
                risk_level="HIGH",
                reason=reason,
                before_summary={
                    "based_on_version_id": str(version.based_on_version_id)
                    if version.based_on_version_id
                    else None
                },
                after_summary={
                    "version_id": str(version.id),
                    "version_number": version.version_number,
                },
                actor_user_id=context.actor_user_id,
                session_id=context.session_id,
                trusted_ip=context.trusted_ip,
            )
        )
        await self._emit(
            "trading_calendar.updated",
            str(version.id),
            version.idempotency_key,
            {
                "market": version.market,
                "version_number": version.version_number,
                "request_id": context.request_id,
                "actor_user_id": context.actor_user_id,
                "session_id": context.session_id,
                "trusted_ip": context.trusted_ip,
            },
        )

    async def _emit(
        self,
        event_type: str,
        aggregate_id: str,
        idempotency_key: str,
        payload: dict,
    ) -> None:
        if self._events is None:
            raise _ports_unavailable()
        await self._events.append(
            CalendarEvent(
                event_type=event_type,
                aggregate_id=aggregate_id,
                idempotency_key=idempotency_key,
                payload=payload,
            )
        )

    async def _coverage_warnings(
        self,
        version: TradingCalendarVersion,
        context: CalendarAuditContext | None,
    ) -> tuple[CalendarValidationIssue, ...]:
        days = max(
            0,
            _continuous_days(version.days, self._today(), limit=61) - 1,
        )
        if days >= 60:
            return ()
        assert context is not None
        warning = CalendarValidationIssue(
            code="CALENDAR_COVERAGE_LOW",
            path="days",
            message="未来确认日历覆盖低于 60 个自然日",
        )
        await self._emit(
            "trading_calendar.coverage_low",
            str(version.id),
            f"{version.idempotency_key}:coverage-low",
            {
                "market": version.market,
                "days": days,
                "level": "WARNING",
                "request_id": context.request_id,
            },
        )
        return (warning,)

    def _require_write_ports(
        self,
        context: CalendarAuditContext | None,
        idempotency_key: str,
    ) -> None:
        if (
            self._audit is None
            or self._events is None
            or context is None
            or context.idempotency_key != idempotency_key
        ):
            raise _ports_unavailable()


def _day_model(
    version_id: UUID,
    item: CalendarDayInput,
    *,
    source: str,
    override_reason: str | None,
) -> TradingCalendarDay:
    result = TradingCalendarDay(
        version_id=version_id,
        trade_date=item.trade_date,
        is_trading_day=item.is_trading_day,
        status=item.status,
        source=source,
        note=item.note,
        override_reason=override_reason,
    )
    result.sessions = [
        TradingSession(
            calendar_day_id=result.id,
            sequence=index,
            starts_at=session.starts_at,
            ends_at=session.ends_at,
        )
        for index, session in enumerate(item.sessions, start=1)
    ]
    return result


def _day_contract(item: TradingCalendarDay) -> CalendarDayInput:
    return CalendarDayInput(
        trade_date=item.trade_date,
        is_trading_day=item.is_trading_day,
        status=CalendarDayStatus(item.status),
        sessions=tuple(
            TradingSessionInput(
                starts_at=session.starts_at,
                ends_at=session.ends_at,
            )
            for session in item.sessions
        ),
        note=item.note,
    )


def _clone_day(
    version_id: UUID,
    item: TradingCalendarDay,
) -> TradingCalendarDay:
    result = TradingCalendarDay(
        version_id=version_id,
        trade_date=item.trade_date,
        is_trading_day=item.is_trading_day,
        status=item.status,
        source=item.source,
        note=item.note,
        override_reason=item.override_reason,
    )
    result.sessions = [
        TradingSession(
            calendar_day_id=result.id,
            sequence=session.sequence,
            starts_at=session.starts_at,
            ends_at=session.ends_at,
        )
        for session in item.sessions
    ]
    return result


def _activation_issues(
    days: tuple[CalendarDayInput, ...],
    today: date,
) -> tuple[CalendarValidationIssue, ...]:
    issues = validate_calendar_coverage(
        days,
        from_date=today,
        required_days=31,
    )
    today_item = next((item for item in days if item.trade_date == today), None)
    if today_item is not None and today_item.status in {
        CalendarDayStatus.CONFIRMED,
        CalendarDayStatus.OVERRIDDEN,
    }:
        return issues
    return (
        CalendarValidationIssue(
            code="CALENDAR_TODAY_MISSING",
            path=f"days[{today.isoformat()}]",
            message="当天日历缺失或尚未确认",
        ),
        *issues,
    )


def _command_hash(command: object) -> str:
    content = command.model_dump(
        mode="json", exclude={"idempotency_key", "expected_current_version"}
    )
    encoded = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _verify_replay(version: TradingCalendarVersion, content_hash: str) -> None:
    if version.content_hash != content_hash:
        raise AppError(
            code="CALENDAR_IDEMPOTENCY_CONFLICT",
            message="同一幂等键已用于不同日历内容",
            status_code=409,
        )


def _result(
    version: TradingCalendarVersion,
    *,
    created: bool,
    warnings: tuple[CalendarValidationIssue, ...] = (),
) -> CalendarVersionResult:
    return CalendarVersionResult(
        version_id=version.id,
        version_number=version.version_number,
        created=created,
        warnings=warnings,
    )


def _optimistic_conflict() -> AppError:
    return AppError(
        code="CALENDAR_OPTIMISTIC_LOCK_CONFLICT",
        message="日历已被其他请求修改，请刷新后重试",
        status_code=409,
    )


def _ports_unavailable() -> AppError:
    return AppError(
        code="CALENDAR_TRANSACTION_PORT_UNAVAILABLE",
        message="日历审计或可靠事件服务不可用",
        status_code=503,
    )


def _audit_key(action_code: str, idempotency_key: str) -> str:
    digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()
    return f"calendar:{action_code.lower()}:{digest}"


def _continuous_days(
    days: list[TradingCalendarDay],
    from_date: date,
    *,
    limit: int,
) -> int:
    by_date = {item.trade_date: item for item in days}
    count = 0
    for offset in range(limit):
        item = by_date.get(from_date + timedelta(days=offset))
        if item is None or item.status not in {
            CalendarDayStatus.CONFIRMED,
            CalendarDayStatus.OVERRIDDEN,
        }:
            break
        count += 1
    return count
