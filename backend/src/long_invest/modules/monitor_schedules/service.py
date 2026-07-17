from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, time
from typing import Any, Protocol
from uuid import UUID, uuid4

from long_invest.modules.monitor_schedules.contracts import ScheduleDefinition
from long_invest.modules.monitor_schedules.models import (
    MonitorSchedule,
    MonitorScheduleRevision,
)
from long_invest.platform.errors import AppError


@dataclass(frozen=True, slots=True)
class ScheduleChangeEvent:
    schedule_id: UUID
    revision_id: UUID
    version: int
    times: tuple[str, ...]
    reason: str
    action: str
    request_id: str
    idempotency_key: str
    request_digest: str
    actor_user_id: str
    session_id: str
    trusted_ip: str
    before_summary: dict[str, Any] | None
    after_summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ScheduleReplay:
    schedule_id: UUID
    revision_id: UUID
    request_digest: str
    after_summary: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ScheduleChangeResult:
    schedule: MonitorSchedule
    revision: MonitorScheduleRevision
    replayed: bool = False


class ScheduleAuditPort(Protocol):
    async def find_replay(
        self, *, action: str, schedule_id: UUID | None, idempotency_key: str
    ) -> ScheduleReplay | ScheduleChangeEvent | None: ...
    async def record(self, event: ScheduleChangeEvent) -> None: ...


class ScheduleEventPort(Protocol):
    async def changed(self, event: ScheduleChangeEvent) -> None: ...


class MonitorScheduleService:
    def __init__(
        self,
        repository: Any,
        *,
        audit: ScheduleAuditPort,
        events: ScheduleEventPort,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._repository = repository
        self._audit = audit
        self._events = events
        self._now = now or (lambda: datetime.now(UTC))

    async def list(self, *, include_archived: bool = False):
        return await self._repository.list(include_archived=include_archived)

    async def get(self, schedule_id: UUID):
        row = await self._repository.get(schedule_id)
        if row is None:
            raise _not_found()
        return row

    async def current_revision(self, schedule_id: UUID):
        row = await self.get(schedule_id)
        self._ensure_active(row)
        return await self._current_revision(row)

    async def revisions(self, schedule_id: UUID):
        await self.get(schedule_id)
        return await self._repository.list_revisions(schedule_id)

    async def create(
        self, definition: ScheduleDefinition, **context: str
    ) -> ScheduleChangeResult:
        await self._repository.lock_idempotency(definition.idempotency_key)
        digest = _request_digest(
            "created",
            name=definition.name,
            times=_times(definition.times),
            timezone="Asia/Shanghai",
            reason=definition.reason,
            expected_version=None,
        )
        existing = await self._audit.find_replay(
            action="created",
            schedule_id=None,
            idempotency_key=definition.idempotency_key,
        )
        if existing is not None:
            self._verify_digest(existing, digest)
            schedule = await self.get(existing.schedule_id)
            return await self._current_result(schedule, replayed=True)

        schedule = MonitorSchedule(id=uuid4(), name=definition.name, version=1)
        await self._repository.create_schedule(schedule)
        revision = _revision(
            schedule.id,
            1,
            definition,
            _content_hash(definition.name, definition.times),
            digest,
            "created",
            context,
        )
        await self._repository.add_revision(revision)
        await self._repository.initialize_current(schedule.id, revision.id)
        schedule.current_revision_id = revision.id
        await self._publish(
            schedule, revision, "created", digest, None, definition.reason, context
        )
        return ScheduleChangeResult(schedule, revision)

    async def update(
        self, schedule_id: UUID, definition: ScheduleDefinition, **context: str
    ) -> ScheduleChangeResult:
        if definition.expected_version is None:
            raise AppError(
                code="MONITOR_SCHEDULE_EXPECTED_VERSION_REQUIRED",
                message="修改调度需要当前版本",
                status_code=422,
            )
        schedule = await self._locked(schedule_id)
        self._ensure_active(schedule)
        digest = _request_digest(
            "updated",
            schedule_id=str(schedule_id),
            name=definition.name,
            times=_times(definition.times),
            timezone="Asia/Shanghai",
            reason=definition.reason,
            expected_version=definition.expected_version,
        )
        replay = await self._find_replay(
            "updated", schedule_id, definition.idempotency_key, digest
        )
        if replay is not None:
            return await self._current_result(schedule, replayed=True)
        if schedule.version != definition.expected_version:
            raise _version_conflict()

        current = await self._current_revision(schedule)
        before = _summary(schedule, current)
        revisions = await self._repository.list_revisions(schedule_id)
        revision = _revision(
            schedule_id,
            max((row.revision_no for row in revisions), default=0) + 1,
            definition,
            _content_hash(definition.name, definition.times),
            digest,
            "updated",
            context,
        )
        await self._repository.add_revision(revision)
        if not await self._repository.switch_current(
            schedule_id,
            revision_id=revision.id,
            name=definition.name,
            expected_version=definition.expected_version,
        ):
            raise _version_conflict()
        schedule.current_revision_id = revision.id
        schedule.name = definition.name
        schedule.version = definition.expected_version + 1
        await self._publish(
            schedule, revision, "updated", digest, before, definition.reason, context
        )
        return ScheduleChangeResult(schedule, revision)

    async def restore(
        self,
        schedule_id: UUID,
        *,
        source_revision_id: UUID,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        **context: str,
    ) -> ScheduleChangeResult:
        reason = _required_reason(reason)
        schedule = await self._locked(schedule_id)
        self._ensure_active(schedule)
        digest = _request_digest(
            "restored",
            schedule_id=str(schedule_id),
            source_revision_id=str(source_revision_id),
            reason=reason,
            expected_version=expected_version,
        )
        replay = await self._find_replay(
            "restored", schedule_id, idempotency_key, digest
        )
        if replay is not None:
            return await self._current_result(schedule, replayed=True)
        if schedule.version != expected_version:
            raise _version_conflict()
        source = await self._repository.get_revision(schedule_id, source_revision_id)
        if source is None:
            raise AppError(
                code="MONITOR_SCHEDULE_REVISION_NOT_FOUND",
                message="调度修订不存在",
                status_code=404,
            )

        current = await self._current_revision(schedule)
        before = _summary(schedule, current)
        definition = ScheduleDefinition(
            name=schedule.name,
            times=tuple(time.fromisoformat(value) for value in source.times),
            reason=reason,
            idempotency_key=idempotency_key,
            expected_version=expected_version,
        )
        revisions = await self._repository.list_revisions(schedule_id)
        revision = _revision(
            schedule_id,
            max((row.revision_no for row in revisions), default=0) + 1,
            definition,
            _content_hash(definition.name, definition.times),
            digest,
            "restored",
            context,
        )
        await self._repository.add_revision(revision)
        if not await self._repository.switch_current(
            schedule_id,
            revision_id=revision.id,
            name=schedule.name,
            expected_version=expected_version,
        ):
            raise _version_conflict()
        schedule.current_revision_id = revision.id
        schedule.version = expected_version + 1
        await self._publish(
            schedule, revision, "restored", digest, before, reason, context
        )
        return ScheduleChangeResult(schedule, revision)

    async def archive(
        self,
        schedule_id: UUID,
        *,
        expected_version: int,
        reason: str,
        idempotency_key: str,
        **context: str,
    ) -> ScheduleChangeResult:
        reason = _required_reason(reason)
        schedule = await self._locked(schedule_id)
        digest = _request_digest(
            "archived",
            schedule_id=str(schedule_id),
            reason=reason,
            expected_version=expected_version,
        )
        replay = await self._find_replay(
            "archived", schedule_id, idempotency_key, digest
        )
        if replay is not None:
            return await self._current_result(schedule, replayed=True)
        self._ensure_active(schedule)
        if schedule.version != expected_version:
            raise _version_conflict()

        revision = await self._current_revision(schedule)
        before = _summary(schedule, revision)
        archived_at = self._now()
        if not await self._repository.archive(
            schedule_id, expected_version=expected_version, archived_at=archived_at
        ):
            raise _version_conflict()
        schedule.archived_at = archived_at
        schedule.version = expected_version + 1
        await self._publish(
            schedule,
            revision,
            "archived",
            digest,
            before,
            reason,
            {**context, "idempotency_key": idempotency_key},
        )
        return ScheduleChangeResult(schedule, revision)

    async def _find_replay(
        self, action: str, schedule_id: UUID, idempotency_key: str, digest: str
    ):
        existing = await self._audit.find_replay(
            action=action, schedule_id=schedule_id, idempotency_key=idempotency_key
        )
        if existing is not None:
            self._verify_digest(existing, digest)
        return existing

    @staticmethod
    def _verify_digest(existing: Any, digest: str) -> None:
        if existing.request_digest != digest:
            raise _idempotency_conflict()

    async def _current_result(
        self, schedule: Any, *, replayed: bool
    ) -> ScheduleChangeResult:
        revision = await self._current_revision(schedule)
        return ScheduleChangeResult(schedule, revision, replayed)

    async def _current_revision(self, schedule: Any):
        if schedule.current_revision_id is None:
            raise _not_found()
        revision = await self._repository.get_revision(
            schedule.id, schedule.current_revision_id
        )
        if revision is None:
            raise _not_found()
        return revision

    async def _locked(self, schedule_id: UUID):
        row = await self._repository.get(schedule_id, for_update=True)
        if row is None:
            raise _not_found()
        return row

    @staticmethod
    def _ensure_active(schedule: Any) -> None:
        if schedule.archived_at is not None:
            raise AppError(
                code="MONITOR_SCHEDULE_ARCHIVED",
                message="已归档调度不能修改",
                status_code=409,
            )

    async def _publish(
        self,
        schedule: Any,
        revision: Any,
        action: str,
        request_digest: str,
        before_summary: dict[str, Any] | None,
        reason: str,
        context: dict[str, str],
    ) -> None:
        event = ScheduleChangeEvent(
            schedule_id=schedule.id,
            revision_id=revision.id,
            version=schedule.version,
            times=tuple(revision.times),
            reason=reason,
            action=action,
            request_id=context.get("request_id", revision.request_id),
            idempotency_key=context.get("idempotency_key", revision.idempotency_key),
            request_digest=request_digest,
            actor_user_id=context.get("actor_user_id", revision.created_by_user_id),
            session_id=context.get("session_id", "unknown"),
            trusted_ip=context.get("trusted_ip", "unknown"),
            before_summary=before_summary,
            after_summary=_summary(schedule, revision),
        )
        await self._audit.record(event)
        await self._events.changed(event)


def _revision(
    schedule_id: UUID,
    revision_no: int,
    definition: ScheduleDefinition,
    content_hash: str,
    request_digest: str,
    action: str,
    context: dict[str, str],
) -> MonitorScheduleRevision:
    return MonitorScheduleRevision(
        id=uuid4(),
        schedule_id=schedule_id,
        revision_no=revision_no,
        times=_times(definition.times),
        timezone="Asia/Shanghai",
        reason=definition.reason,
        created_by_user_id=context.get("actor_user_id", "unknown"),
        request_id=context.get("request_id", "unknown"),
        idempotency_key=definition.idempotency_key,
        content_hash=content_hash,
        metadata_snapshot={
            "name": definition.name,
            "request_digest": request_digest,
            "action": action,
        },
    )


def _summary(schedule: Any, revision: Any) -> dict[str, Any]:
    return {
        "name": schedule.name,
        "version": schedule.version,
        "revision_id": str(revision.id),
        "times": list(revision.times),
        "archived": schedule.archived_at is not None,
    }


def _times(values: tuple[time, ...]) -> tuple[str, ...]:
    return tuple(value.strftime("%H:%M") for value in values)


def _request_digest(action: str, **payload: Any) -> str:
    body = {"action": action, **payload}
    return hashlib.sha256(
        json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()


def _content_hash(name: str, times: tuple[time, ...]) -> str:
    return _request_digest(
        "content", name=name, times=_times(times), timezone="Asia/Shanghai"
    )


def _required_reason(reason: str) -> str:
    value = reason.strip()
    if not value:
        raise AppError(
            code="MONITOR_SCHEDULE_REASON_REQUIRED",
            message="调度操作原因不能为空",
            status_code=422,
        )
    if len(value) > 500:
        raise AppError(
            code="VALIDATION_ERROR",
            message="调度操作原因不能超过 500 个字符",
            status_code=422,
        )
    return value


def _not_found() -> AppError:
    return AppError(
        code="MONITOR_SCHEDULE_NOT_FOUND", message="调度不存在", status_code=404
    )


def _version_conflict() -> AppError:
    return AppError(
        code="MONITOR_SCHEDULE_VERSION_CONFLICT",
        message="调度已被其他操作修改",
        status_code=409,
    )


def _idempotency_conflict() -> AppError:
    return AppError(
        code="MONITOR_SCHEDULE_IDEMPOTENCY_CONFLICT",
        message="幂等键已用于不同内容",
        status_code=409,
    )
