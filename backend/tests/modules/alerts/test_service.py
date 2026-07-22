import asyncio
from datetime import UTC, date, datetime

import pytest

from long_invest.modules.alerts.contracts import (
    AlertActionType,
    AlertCommand,
    AlertSeverity,
    AutoResolveAlert,
    RemindUnresolvedAlerts,
    ReportAlert,
)
from long_invest.modules.alerts.service import AlertService
from long_invest.platform.errors import AppError


class FakeRepository:
    def __init__(self) -> None:
        self.session = object()
        self.alerts = {}
        self.occurrences_by_source = {}
        self.actions_by_key = {}

    async def lock_aggregation_key(self, key):
        del key

    async def find_by_key(self, key, *, lock=False):
        del lock
        return self.alerts.get(key)

    async def get(self, alert_id, *, lock=False):
        del lock
        return next(
            (item for item in self.alerts.values() if item.id == alert_id), None
        )

    async def occurrence_by_source(self, source_event_id):
        return self.occurrences_by_source.get(source_event_id)

    async def action_by_idempotency(self, key):
        return self.actions_by_key.get(key)

    async def unresolved(self):
        return tuple(
            alert for alert in self.alerts.values() if alert.status != "RESOLVED"
        )

    def add_all(self, *items):
        for item in items:
            if hasattr(item, "aggregation_key"):
                self.alerts[item.aggregation_key] = item
            if hasattr(item, "source_event_id"):
                self.occurrences_by_source[item.source_event_id] = item
            if hasattr(item, "idempotency_key"):
                self.actions_by_key[item.idempotency_key] = item

    async def flush(self):
        now = datetime.now(UTC)
        for alert in self.alerts.values():
            alert.created_at = getattr(alert, "created_at", None) or now
            alert.updated_at = now


class Notifications:
    def __init__(self) -> None:
        self.calls = []
        self.daily_calls = []

    async def publish(self, alert, **kwargs):
        self.calls.append((alert.id, kwargs))

    async def publish_daily_unresolved(self, alert, **kwargs):
        self.daily_calls.append((alert.id, kwargs))


def report(*, source="source-1", severity=AlertSeverity.ERROR):
    return ReportAlert(
        aggregation_key="QUOTE_MISSING:cycle-1",
        source_event_id=source,
        alert_type="QUOTE_MISSING",
        object_type="quote_cycle",
        object_id="cycle-1",
        severity=severity,
        title="行情缺失",
        summary="本批次缺少一只股票",
        details={"missing_count": 1},
        request_id="request-1",
    )


def command(alert, *, version=None, key="action-1"):
    return AlertCommand(
        alert_id=alert.id,
        expected_version=version or alert.version,
        reason="人工处理",
        request_id="request-2",
        idempotency_key=key,
        actor_user_id="user-1",
        session_id="session-1",
        trusted_ip="127.0.0.1",
    )


def subject(repository, notifications=None):
    service = AlertService(repository, notifications=notifications)

    async def no_event(*_args, **_kwargs):
        return None

    service._event = no_event
    service._audit = no_event
    return service


def test_report_aggregates_replays_and_escalates_without_duplicate_alerts() -> None:
    async def scenario():
        repository = FakeRepository()
        notifications = Notifications()
        service = subject(repository, notifications)
        opened = await service.report(report())
        replayed = await service.report(report())
        escalated = await service.report(
            report(source="source-2", severity=AlertSeverity.CRITICAL)
        )
        assert opened.id == replayed.id == escalated.id
        assert escalated.occurrence_count == 2
        assert escalated.severity == AlertSeverity.CRITICAL
        assert repository.actions_by_key["alert-source:source-2"].action == (
            AlertActionType.ESCALATED
        )
        assert len(notifications.calls) == 2

    asyncio.run(scenario())


def test_acknowledge_does_not_resolve_and_resolve_requires_fresh_version() -> None:
    async def scenario():
        repository = FakeRepository()
        service = subject(repository)
        alert = await service.report(report())
        acknowledged, replayed = await service.acknowledge(command(alert))
        assert acknowledged.status == "ACKNOWLEDGED"
        assert acknowledged.resolved_at is None
        assert replayed is False
        with pytest.raises(AppError, match="其他操作更新"):
            await service.resolve(command(alert, version=1, key="resolve-old"))
        resolved, _ = await service.resolve(
            command(alert, version=alert.version, key="resolve")
        )
        assert resolved.status == "RESOLVED"
        assert resolved.resolution_reason == "人工处理"

    asyncio.run(scenario())


def test_retry_is_rejected_when_alert_has_no_recovery_job() -> None:
    async def scenario():
        repository = FakeRepository()
        service = subject(repository)
        alert = await service.report(report())
        with pytest.raises(AppError, match="不支持重试"):
            await service.retry(command(alert))

    asyncio.run(scenario())


def test_notification_payload_rejects_sensitive_details() -> None:
    async def scenario():
        repository = FakeRepository()
        service = subject(repository)
        unsafe = report()
        object.__setattr__(unsafe, "details", {"password": "secret"})
        with pytest.raises(ValueError):
            await service.report(unsafe)

    asyncio.run(scenario())


def test_objective_recovery_auto_resolves_and_is_idempotent() -> None:
    async def scenario():
        repository = FakeRepository()
        notifications = Notifications()
        service = subject(repository, notifications)
        alert = await service.report(report())
        recovered, replayed = await service.auto_resolve(
            AutoResolveAlert(
                aggregation_key=alert.aggregation_key,
                source_event_id="quote-recovered-1",
                reason="行情已经恢复",
                request_id="request-recovered",
            )
        )
        assert recovered.status == "RESOLVED"
        assert recovered.resolved_by_user_id is None
        assert replayed is False
        assert repository.actions_by_key[
            "alert-recovery:quote-recovered-1"
        ].action == AlertActionType.AUTO_RESOLVED
        replay, replayed = await service.auto_resolve(
            AutoResolveAlert(
                aggregation_key=alert.aggregation_key,
                source_event_id="quote-recovered-1",
                reason="行情已经恢复",
                request_id="request-recovered",
            )
        )
        assert replay.id == alert.id
        assert replayed is True
        assert len(notifications.calls) == 2

    asyncio.run(scenario())


def test_manual_judgment_alert_cannot_auto_resolve() -> None:
    async def scenario():
        repository = FakeRepository()
        service = subject(repository)
        command = report()
        object.__setattr__(command, "alert_type", "QUOTE_CONFLICT")
        alert = await service.report(command)
        with pytest.raises(AppError) as error:
            await service.auto_resolve(
                AutoResolveAlert(
                    aggregation_key=alert.aggregation_key,
                    source_event_id="conflict-cleared-1",
                    reason="来源恢复",
                    request_id="request-recovered",
                )
            )
        assert error.value.code == "ALERT_AUTO_RESOLVE_NOT_ALLOWED"
        assert alert.status == "OPEN"

    asyncio.run(scenario())


def test_daily_unresolved_includes_acknowledged_and_uses_stable_date() -> None:
    async def scenario():
        repository = FakeRepository()
        notifications = Notifications()
        service = subject(repository, notifications)
        opened = await service.report(report())
        acknowledged, _ = await service.acknowledge(command(opened))
        second = await service.report(report(source="source-2"))
        assert second.id == acknowledged.id

        reminder = RemindUnresolvedAlerts(
            reminder_date=date(2026, 7, 22),
            request_id="daily-reminder",
        )
        assert await service.remind_unresolved(reminder) == 1
        assert notifications.daily_calls == [
            (
                acknowledged.id,
                {
                    "reminder_date": date(2026, 7, 22),
                    "request_id": "daily-reminder",
                },
            )
        ]

    asyncio.run(scenario())
