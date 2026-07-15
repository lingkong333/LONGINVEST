from __future__ import annotations

import hashlib
import json
from datetime import date
from uuid import UUID

from long_invest.modules.calendar.contracts import (
    CalendarCoverage,
    CalendarDayInput,
    CalendarDayStatus,
    CalendarEvent,
    CalendarEventSink,
    CalendarImport,
    CalendarVersionResult,
    OverrideCalendarDay,
    RestoreCalendarVersion,
    TradingSessionInput,
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
    ) -> None:
        self._repository = repository
        self._audit = audit_service
        self._events = event_sink

    async def import_version(
        self, command: CalendarImport
    ) -> CalendarVersionResult:
        issues = validate_calendar_import(command)
        if issues:
            return CalendarVersionResult(issues=issues)
        content_hash = _command_hash(command)
        replay = await self._repository.find_by_idempotency(
            command.market, command.idempotency_key
        )
        if replay is not None:
            _verify_replay(replay, content_hash)
            return _result(replay, created=False)

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
        await self._record_change(version, "TRADING_CALENDAR_IMPORT", command.reason)
        return _result(version, created=True)

    async def override_day(
        self, command: OverrideCalendarDay
    ) -> CalendarVersionResult:
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
        )
        issues = validate_calendar_import(imported)
        if issues:
            return CalendarVersionResult(issues=issues)
        version = await self._build_version(
            imported,
            content_hash=desired_hash,
            based_on_version_id=base.id,
            override_date=command.trade_date,
            override_reason=command.reason,
        )
        await self._activate(version, command.expected_current_version)
        await self._record_change(
            version, "TRADING_CALENDAR_OVERRIDE", command.reason
        )
        return _result(version, created=True)

    async def restore_version(
        self, command: RestoreCalendarVersion
    ) -> CalendarVersionResult:
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
        )
        version = await self._build_version(
            imported,
            content_hash=desired_hash,
            based_on_version_id=target.id,
        )
        await self._activate(version, command.expected_current_version)
        await self._record_change(
            version, "TRADING_CALENDAR_RESTORE", command.reason
        )
        return _result(version, created=True)

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
        through = await self._repository.confirmed_through(market, from_date)
        days = max(0, (through - from_date).days) if through else 0
        level = "ERROR" if days < 30 else "WARNING" if days < 60 else "OK"
        today = await self._repository.get_day(market, from_date)
        missing = today is None or today.status == CalendarDayStatus.MISSING
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
        override_date: date | None = None,
        override_reason: str | None = None,
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
        version.days = [
            _day_model(
                version.id,
                item,
                source=command.source,
                override_reason=(
                    override_reason if item.trade_date == override_date else None
                ),
            )
            for item in command.days
        ]
        return version

    async def _activate(
        self, version: TradingCalendarVersion, expected: int | None
    ) -> None:
        await self._repository.add_version(version)
        switched = await self._repository.switch_current(
            market=version.market,
            version_id=version.id,
            expected_pointer_version=expected,
        )
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
    ) -> None:
        if self._audit is not None:
            await self._audit.append(
                AuditWrite(
                    action_code=action_code,
                    object_type="trading_calendar_version",
                    object_id=str(version.id),
                    result="SUCCESS",
                    request_id=version.idempotency_key,
                    idempotency_key=version.idempotency_key,
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
                )
            )
        await self._emit(
            "trading_calendar.updated",
            str(version.id),
            version.idempotency_key,
            {"market": version.market, "version_number": version.version_number},
        )

    async def _emit(
        self,
        event_type: str,
        aggregate_id: str,
        idempotency_key: str,
        payload: dict,
    ) -> None:
        if self._events is not None:
            await self._events.append(
                CalendarEvent(
                    event_type=event_type,
                    aggregate_id=aggregate_id,
                    idempotency_key=idempotency_key,
                    payload=payload,
                )
            )


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
    version: TradingCalendarVersion, *, created: bool
) -> CalendarVersionResult:
    return CalendarVersionResult(
        version_id=version.id,
        version_number=version.version_number,
        created=created,
    )


def _optimistic_conflict() -> AppError:
    return AppError(
        code="CALENDAR_OPTIMISTIC_LOCK_CONFLICT",
        message="日历已被其他请求修改，请刷新后重试",
        status_code=409,
    )
