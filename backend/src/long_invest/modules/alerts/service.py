from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

from long_invest.modules.alerts.contracts import (
    AlertActionType,
    AlertCommand,
    AlertSeverity,
    AlertStatus,
    AutoResolveAlert,
    RemindUnresolvedAlerts,
    ReportAlert,
)
from long_invest.modules.alerts.models import (
    SystemAlert,
    SystemAlertAction,
    SystemAlertOccurrence,
)
from long_invest.modules.alerts.repository import AlertRepository
from long_invest.modules.notifications.security import validate_notification_payload
from long_invest.platform.audit.contracts import AuditWrite
from long_invest.platform.audit.service import AuditService
from long_invest.platform.errors import AppError
from long_invest.platform.jobs.contracts import SubmitJob
from long_invest.platform.jobs.service import JobService
from long_invest.platform.outbox.service import TransactionalOutboxWriter

_RANK = {severity: rank for rank, severity in enumerate(AlertSeverity)}
_AUTO_RESOLVABLE_ALERT_TYPES = frozenset(
    {
        "PROVIDER_UNAVAILABLE",
        "PROVIDER_DEGRADED",
        "QUOTE_MISSING",
        "DAILY_DATA_INCOMPLETE",
        "QFQ_REFRESH_FAILED",
        "TARGET_CALCULATION_FAILED",
        "WORKER_LOST",
        "DISK_USAGE_HIGH",
        "NOTIFICATION_CHANNEL_FAILED",
        "NOTIFICATION_CHANNEL_DEGRADED",
    }
)


class AlertService:
    def __init__(self, repository: AlertRepository, *, notifications=None) -> None:
        self._repository = repository
        self._notifications = notifications

    async def report(self, command: ReportAlert) -> SystemAlert:
        existing_occurrence = await self._repository.occurrence_by_source(
            command.source_event_id
        )
        if existing_occurrence is not None:
            alert = await self._repository.get(existing_occurrence.alert_id)
            if alert is None:
                raise _error("ALERT_NOT_FOUND", "告警不存在", 404)
            return alert
        await self._repository.lock_aggregation_key(command.aggregation_key)
        alert = await self._repository.find_by_key(command.aggregation_key, lock=True)
        now = datetime.now(UTC)
        action = AlertActionType.UPDATED
        notify = False
        if alert is None:
            alert = SystemAlert(
                id=uuid4(),
                aggregation_key=command.aggregation_key,
                alert_type=command.alert_type,
                object_type=command.object_type,
                object_id=command.object_id,
                severity=command.severity,
                status=AlertStatus.OPEN,
                title=command.title,
                summary=command.summary,
                details=validate_notification_payload(command.details),
                occurrence_count=1,
                first_seen_at=now,
                last_seen_at=now,
                retry_job_type=command.retry_job_type,
                retry_queue=command.retry_queue,
                retry_config=(
                    validate_notification_payload(command.retry_config)
                    if command.retry_config is not None
                    else None
                ),
                version=1,
            )
            action = AlertActionType.OPENED
            notify = command.severity is not AlertSeverity.INFO
        else:
            previous = AlertSeverity(alert.severity)
            if AlertStatus(alert.status) is AlertStatus.RESOLVED:
                alert.status = AlertStatus.OPEN
                alert.resolved_at = None
                alert.resolved_by_user_id = None
                alert.resolution_reason = None
                action = AlertActionType.REOPENED
                notify = command.severity is not AlertSeverity.INFO
            elif _RANK[command.severity] > _RANK[previous]:
                notify = True
                action = AlertActionType.ESCALATED
            alert.severity = max((previous, command.severity), key=_RANK.get)
            alert.summary = command.summary
            alert.details = validate_notification_payload(command.details)
            alert.occurrence_count += 1
            alert.last_seen_at = now
            alert.version += 1
            alert.retry_job_type = command.retry_job_type or alert.retry_job_type
            alert.retry_queue = command.retry_queue or alert.retry_queue
            alert.retry_config = (
                validate_notification_payload(command.retry_config)
                if command.retry_config is not None
                else alert.retry_config
            )
        occurrence = SystemAlertOccurrence(
            alert_id=alert.id,
            source_event_id=command.source_event_id,
            severity=command.severity,
            summary=command.summary,
            details=validate_notification_payload(command.details),
            request_id=command.request_id,
            occurred_at=now,
        )
        action_record = SystemAlertAction(
            alert_id=alert.id,
            action=action,
            reason=None,
            actor_user_id=None,
            request_id=command.request_id,
            idempotency_key=f"alert-source:{command.source_event_id}",
        )
        self._repository.add_all(alert, occurrence, action_record)
        await self._repository.flush()
        await self._event(alert, action.value, command.request_id)
        if notify and self._notifications is not None:
            await self._notifications.publish(
                alert, recovered=False, request_id=command.request_id
            )
        return alert

    async def list(self, **filters):
        return await self._repository.list_alerts(**filters)

    async def get(self, alert_id):
        alert = await self._repository.get(alert_id)
        if alert is None:
            raise _error("ALERT_NOT_FOUND", "告警不存在", 404)
        return alert

    async def occurrences(self, alert_id, **page):
        await self.get(alert_id)
        return await self._repository.occurrences(alert_id, **page)

    async def actions(self, alert_id, **page):
        await self.get(alert_id)
        return await self._repository.actions(alert_id, **page)

    async def acknowledge(self, command: AlertCommand):
        return await self._transition(command, resolve=False)

    async def resolve(self, command: AlertCommand):
        return await self._transition(command, resolve=True)

    async def auto_resolve(self, command: AutoResolveAlert):
        idempotency_key = f"alert-recovery:{command.source_event_id}"
        replay = await self._repository.action_by_idempotency(idempotency_key)
        if replay is not None:
            replayed_alert = await self.get(replay.alert_id)
            if (
                replay.action != AlertActionType.AUTO_RESOLVED
                or replayed_alert.aggregation_key != command.aggregation_key
            ):
                raise _error(
                    "ALERT_IDEMPOTENCY_CONFLICT",
                    "恢复事件已用于其他告警操作",
                    409,
                )
            return replayed_alert, True

        await self._repository.lock_aggregation_key(command.aggregation_key)
        alert = await self._repository.find_by_key(command.aggregation_key, lock=True)
        if alert is None:
            raise _error("ALERT_NOT_FOUND", "告警不存在", 404)
        if alert.alert_type not in _AUTO_RESOLVABLE_ALERT_TYPES:
            raise _error(
                "ALERT_AUTO_RESOLVE_NOT_ALLOWED",
                "该告警需要人工判断，不能自动解决",
                409,
            )
        if AlertStatus(alert.status) is AlertStatus.RESOLVED:
            return alert, True

        now = datetime.now(UTC)
        alert.status = AlertStatus.RESOLVED
        alert.resolved_at = now
        alert.resolved_by_user_id = None
        alert.resolution_reason = command.reason
        alert.version += 1
        action = SystemAlertAction(
            alert_id=alert.id,
            action=AlertActionType.AUTO_RESOLVED,
            reason=command.reason,
            actor_user_id=None,
            request_id=command.request_id,
            idempotency_key=idempotency_key,
        )
        self._repository.add_all(action)
        await self._event(
            alert, AlertActionType.AUTO_RESOLVED.value, command.request_id
        )
        await self._repository.flush()
        if self._notifications is not None:
            await self._notifications.publish(
                alert, recovered=True, request_id=command.request_id
            )
        return alert, False

    async def remind_unresolved(self, command: RemindUnresolvedAlerts) -> int:
        if self._notifications is None:
            return 0
        alerts = await self._repository.unresolved()
        for alert in alerts:
            await self._notifications.publish_daily_unresolved(
                alert,
                reminder_date=command.reminder_date,
                request_id=command.request_id,
            )
        return len(alerts)

    async def retry(self, command: AlertCommand):
        replay = await self._repository.action_by_idempotency(command.idempotency_key)
        if replay is not None:
            if (
                replay.alert_id != command.alert_id
                or replay.action != AlertActionType.RETRY_REQUESTED
            ):
                raise _error("ALERT_IDEMPOTENCY_CONFLICT", "重复请求内容不一致", 409)
            return await self.get(command.alert_id), replay.job_id, True
        alert = await self._repository.get(command.alert_id, lock=True)
        self._check_version(alert, command)
        if (
            not alert.retry_job_type
            or not alert.retry_queue
            or alert.retry_config is None
        ):
            raise _error("ALERT_RETRY_UNAVAILABLE", "该告警不支持重试", 409)
        job = await JobService(self._repository.session).submit(
            SubmitJob(
                job_type=alert.retry_job_type,
                queue=alert.retry_queue,
                idempotency_scope=f"alert-retry:{alert.id}",
                idempotency_key=hashlib.sha256(
                    command.idempotency_key.encode()
                ).hexdigest(),
                request_id=command.request_id,
                config_snapshot=alert.retry_config,
                business_object_type="system_alert",
                business_object_id=str(alert.id),
                created_by_user_id=command.actor_user_id,
            )
        )
        action = SystemAlertAction(
            alert_id=alert.id,
            action=AlertActionType.RETRY_REQUESTED,
            reason=command.reason,
            actor_user_id=command.actor_user_id,
            request_id=command.request_id,
            idempotency_key=command.idempotency_key,
            job_id=job.id,
        )
        self._repository.add_all(action)
        await self._audit(alert, command, "ALERT_RETRY_REQUESTED")
        await self._repository.flush()
        return alert, job.id, False

    async def _transition(self, command: AlertCommand, *, resolve: bool):
        replay = await self._repository.action_by_idempotency(command.idempotency_key)
        if replay is not None:
            expected_action = (
                AlertActionType.RESOLVED if resolve else AlertActionType.ACKNOWLEDGED
            )
            if replay.alert_id != command.alert_id or replay.action != expected_action:
                raise _error("ALERT_IDEMPOTENCY_CONFLICT", "重复请求内容不一致", 409)
            return await self.get(command.alert_id), True
        alert = await self._repository.get(command.alert_id, lock=True)
        self._check_version(alert, command)
        status = AlertStatus(alert.status)
        now = datetime.now(UTC)
        if resolve:
            if status is AlertStatus.RESOLVED:
                raise _error("ALERT_ALREADY_RESOLVED", "告警已经解决", 409)
            alert.status = AlertStatus.RESOLVED
            alert.resolved_at = now
            alert.resolved_by_user_id = command.actor_user_id
            alert.resolution_reason = command.reason
            action_type = AlertActionType.RESOLVED
        else:
            if status is AlertStatus.RESOLVED:
                raise _error("ALERT_STATE_INVALID", "已解决告警不能确认", 409)
            if status is AlertStatus.ACKNOWLEDGED:
                return alert, True
            alert.status = AlertStatus.ACKNOWLEDGED
            alert.acknowledged_at = now
            alert.acknowledged_by_user_id = command.actor_user_id
            action_type = AlertActionType.ACKNOWLEDGED
        alert.version += 1
        action = SystemAlertAction(
            alert_id=alert.id,
            action=action_type,
            reason=command.reason,
            actor_user_id=command.actor_user_id,
            request_id=command.request_id,
            idempotency_key=command.idempotency_key,
        )
        self._repository.add_all(action)
        await self._audit(alert, command, f"ALERT_{action_type.value}")
        await self._event(alert, action_type.value, command.request_id)
        await self._repository.flush()
        if resolve and self._notifications is not None:
            await self._notifications.publish(
                alert, recovered=True, request_id=command.request_id
            )
        return alert, False

    @staticmethod
    def _check_version(alert, command):
        if alert is None:
            raise _error("ALERT_NOT_FOUND", "告警不存在", 404)
        if alert.version != command.expected_version:
            raise _error("ALERT_VERSION_CONFLICT", "告警已被其他操作更新", 409)

    async def _audit(self, alert, command, action):
        await AuditService(self._repository.session).append(
            AuditWrite(
                action_code=action,
                object_type="SYSTEM_ALERT",
                object_id=str(alert.id),
                result="SUCCESS",
                request_id=command.request_id,
                idempotency_key=command.idempotency_key,
                risk_level="HIGH",
                reason=command.reason,
                before_summary=None,
                after_summary={"status": alert.status, "version": alert.version},
                actor_user_id=command.actor_user_id,
                session_id=command.session_id,
                trusted_ip=command.trusted_ip,
            )
        )

    async def _event(self, alert, action, request_id):
        await TransactionalOutboxWriter().append(
            session=self._repository.session,
            topic=f"alert.{action.lower()}.v1",
            aggregate_type="SYSTEM_ALERT",
            aggregate_id=str(alert.id),
            queue="maintenance",
            payload={
                "alert_id": str(alert.id),
                "status": alert.status,
                "version": alert.version,
                "request_id": request_id,
            },
            dedupe_key=f"alert:{alert.id}:{alert.version}:{action}",
        )


def _error(code: str, message: str, status: int) -> AppError:
    return AppError(code=code, message=message, status_code=status)
