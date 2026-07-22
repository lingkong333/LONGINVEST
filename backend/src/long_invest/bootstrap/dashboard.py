from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from long_invest.bootstrap.providers import build_provider_service
from long_invest.bootstrap.system_status import build_system_status_application
from long_invest.modules.alerts.application import get_alert_application
from long_invest.modules.calendar.application import get_calendar_application
from long_invest.modules.daily_data.application import get_daily_data_application
from long_invest.modules.dashboard.application import DashboardApplication
from long_invest.modules.dashboard.contracts import (
    SectionSnapshot,
    SectionStatus,
    TimelineEntry,
)
from long_invest.modules.dashboard.service import DashboardService
from long_invest.modules.monitoring.application import (
    get_monitor_subscription_application,
)
from long_invest.modules.notifications.application import (
    get_notification_admin_application,
)
from long_invest.modules.notifications.contracts import NotificationDeliveryStatus
from long_invest.modules.positions.application import get_position_application
from long_invest.modules.providers.contracts import ProviderCode
from long_invest.modules.quotes.application import get_quote_application
from long_invest.modules.signals.api import get_signal_application
from long_invest.modules.signals.contracts import SignalZone
from long_invest.modules.targets.api import get_target_application
from long_invest.modules.targets.contracts import TargetStatus
from long_invest.platform.database.engine import get_database
from long_invest.platform.jobs.application import JobAdminApplication
from long_invest.platform.jobs.contracts import JobStatus

SnapshotReader = Callable[[], Awaitable[SectionSnapshot]]


class SectionProvider:
    def __init__(self, section_name: str, reader: SnapshotReader) -> None:
        self.section_name = section_name
        self._reader = reader

    async def snapshot(self) -> SectionSnapshot:
        return await self._reader()


class TimelineProvider:
    async def timeline(
        self, *, limit: int, before: datetime | None
    ) -> tuple[TimelineEntry, ...]:
        readers = (
            self._signal_entries,
            self._alert_entries,
            self._notification_entries,
            self._job_entries,
        )
        results = await asyncio.gather(
            *(reader(limit) for reader in readers), return_exceptions=True
        )
        entries = [
            item
            for result in results
            if not isinstance(result, BaseException)
            for item in result
            if before is None or item.occurred_at < before
        ]
        if not entries and all(isinstance(result, BaseException) for result in results):
            raise ConnectionError("dashboard timeline sources are unavailable")
        return tuple(
            sorted(entries, key=lambda item: (item.occurred_at, item.id), reverse=True)[
                :limit
            ]
        )

    async def _signal_entries(self, limit: int) -> tuple[TimelineEntry, ...]:
        items, _ = await get_signal_application().list_events(page=1, page_size=limit)
        return tuple(
            TimelineEntry(
                id=f"signal:{item.id}",
                event_type="signal.zone_changed",
                object_type="monitor_subscription",
                object_id=str(item.subscription_id),
                title="价格区间发生变化",
                occurred_at=item.created_at,
                details={
                    "before_zone": item.before_zone.value,
                    "after_zone": item.after_zone.value,
                    "notification_eligible": item.notification_eligible,
                },
            )
            for item in items
        )

    async def _alert_entries(self, limit: int) -> tuple[TimelineEntry, ...]:
        items, _ = await get_alert_application().read("list", page=1, page_size=limit)
        return tuple(
            TimelineEntry(
                id=f"alert:{item.id}",
                event_type="alert.updated",
                object_type=item.object_type,
                object_id=item.object_id,
                title=item.title,
                occurred_at=item.last_seen_at,
                details={"severity": item.severity, "status": item.status},
            )
            for item in items
        )

    async def _notification_entries(self, limit: int) -> tuple[TimelineEntry, ...]:
        page = await get_notification_admin_application().read(
            "list_events", page=1, page_size=limit
        )
        return tuple(
            TimelineEntry(
                id=f"notification:{item.id}",
                event_type="notification.updated",
                object_type=item.business_object_type,
                object_id=item.business_object_id,
                title=item.event_type,
                occurred_at=item.created_at,
                details={"status": item.status},
            )
            for item in page.items
        )

    async def _job_entries(self, limit: int) -> tuple[TimelineEntry, ...]:
        page = await JobAdminApplication(get_database()).list_jobs(
            page=1, page_size=limit
        )
        return tuple(
            TimelineEntry(
                id=f"job:{item.id}",
                event_type="job.updated",
                object_type="job",
                object_id=str(item.id),
                title=item.job_type,
                occurred_at=item.updated_at,
                details={"status": item.status, "queue": item.queue},
            )
            for item in page.items
        )


def build_dashboard_application() -> DashboardApplication:
    providers = (
        SectionProvider("system", _system_snapshot),
        SectionProvider("quote_batches", _quote_snapshot),
        SectionProvider("monitoring", _monitoring_snapshot),
        SectionProvider("positions", _position_snapshot),
        SectionProvider("signals", _signal_snapshot),
        SectionProvider("daily_data", _daily_snapshot),
        SectionProvider("targets", _target_snapshot),
        SectionProvider("jobs", _job_snapshot),
        SectionProvider("notifications", _notification_snapshot),
        SectionProvider("providers", _provider_snapshot),
        SectionProvider("infrastructure", _infrastructure_snapshot),
        SectionProvider("alerts", _alert_snapshot),
    )
    return DashboardApplication(DashboardService(providers, TimelineProvider()))


async def _system_snapshot() -> SectionSnapshot:
    health, alerts = await asyncio.gather(
        build_system_status_application().get_health(), _alert_counts()
    )
    return _snapshot(
        {
            "open_alerts": alerts["unresolved"],
            "critical_alerts": alerts["critical"],
        },
        updated_at=health.updated_at,
    )


async def _quote_snapshot() -> SectionSnapshot:
    page = await get_quote_application().list_cycles(status=None, page=1, page_size=1)
    items = page["items"]
    if not items:
        return _empty()
    item = items[0]
    return _snapshot(
        {
            "status": item.status.value,
            "expected_count": item.expected_count,
            "valid_count": item.valid_count,
            "missing_count": item.missing_count,
            "conflict_count": item.conflict_count,
            "failed_count": item.failed_count,
        },
        updated_at=item.finalized_at or item.started_at or item.scheduled_at,
    )


async def _monitoring_snapshot() -> SectionSnapshot:
    subscriptions, states = await asyncio.gather(
        get_monitor_subscription_application().list(include_archived=False),
        _all_signal_states(),
    )
    active_ids = {item.id for item in subscriptions if item.status.value == "ENABLED"}
    with_state = sum(item.subscription_id in active_ids for item in states)
    return _snapshot(
        {
            "active": len(active_ids),
            "with_current_state": with_state,
            "missing_state": max(0, len(active_ids) - with_state),
        }
    )


async def _position_snapshot() -> SectionSnapshot:
    positions, subscriptions, states = await asyncio.gather(
        get_position_application().list(),
        get_monitor_subscription_application().list(include_archived=False),
        _all_signal_states(),
    )
    held = {item.security_id for item in positions if item.status.value == "HOLDING"}
    security_by_subscription = {item.id: item.security_id for item in subscriptions}
    high = {SignalZone.HIGH, SignalZone.STRONG_HIGH}
    high_held = sum(
        item.zone in high and security_by_subscription.get(item.subscription_id) in held
        for item in states
    )
    updated = max(
        (item.updated_at for item in positions if item.updated_at is not None),
        default=None,
    )
    return _snapshot({"held": len(held), "high_zone": high_held}, updated_at=updated)


async def _signal_snapshot() -> SectionSnapshot:
    states, events = await asyncio.gather(_all_signal_states(), _today_signal_events())
    low = {SignalZone.LOW, SignalZone.STRONG_LOW}
    high = {SignalZone.HIGH, SignalZone.STRONG_HIGH}
    return _snapshot(
        {
            "today": len(events),
            "low_zone": sum(item.zone in low for item in states),
            "high_zone": sum(item.zone in high for item in states),
        },
        updated_at=max((item.created_at for item in events), default=None),
    )


async def _daily_snapshot() -> SectionSnapshot:
    items, _ = await get_daily_data_application().list_batches(page=1, page_size=1)
    if not items:
        return _empty()
    item = items[0]
    return _snapshot(
        {
            "trading_date": item.trading_date,
            "status": str(item.status),
            "expected_count": item.expected_count,
            "committed_count": item.committed_count,
            "missing_count": item.missing_count,
            "failed_count": item.failed_count,
        },
        updated_at=item.completed_at or item.started_at or item.created_at,
    )


async def _target_snapshot() -> SectionSnapshot:
    items = await _all_targets()
    attention = {
        TargetStatus.STALE,
        TargetStatus.REVIEW_REQUIRED,
        TargetStatus.FAILED,
        TargetStatus.MISSING,
    }
    return _snapshot(
        {
            "total": len(items),
            "active": sum(item.status is TargetStatus.READY for item in items),
            "attention": sum(item.status in attention for item in items),
        },
        updated_at=max((item.activated_at for item in items), default=None),
    )


async def _job_snapshot() -> SectionSnapshot:
    application = JobAdminApplication(get_database())
    active_statuses = (
        JobStatus.PENDING_DISPATCH,
        JobStatus.QUEUED,
        JobStatus.RUNNING,
        JobStatus.WAITING_RETRY,
        JobStatus.PAUSING,
        JobStatus.CANCEL_REQUESTED,
    )
    pages = await asyncio.gather(
        *(
            application.list_jobs(page=1, page_size=1, status=value)
            for value in active_statuses
        ),
        application.list_jobs(page=1, page_size=1, status=JobStatus.FAILED),
        application.list_jobs(page=1, page_size=1, status=JobStatus.TIMED_OUT),
    )
    return _snapshot(
        {
            "active": sum(page.total for page in pages[:-2]),
            "failed": pages[-2].total,
            "timed_out": pages[-1].total,
        }
    )


async def _notification_snapshot() -> SectionSnapshot:
    application = get_notification_admin_application()
    statuses = (
        NotificationDeliveryStatus.PENDING,
        NotificationDeliveryStatus.RETRY_WAIT,
        NotificationDeliveryStatus.SENT,
        NotificationDeliveryStatus.FAILED,
        NotificationDeliveryStatus.OUTCOME_UNKNOWN,
    )
    pages = await asyncio.gather(
        *(
            application.read("list_deliveries", page=1, page_size=1, status=status)
            for status in statuses
        )
    )
    return _snapshot(
        {
            "pending": pages[0].total + pages[1].total,
            "sent": pages[2].total,
            "failed": pages[3].total + pages[4].total,
        }
    )


async def _provider_snapshot() -> SectionSnapshot:
    async with get_database().session() as session:
        service = build_provider_service(session)
        providers = await service.list_providers()
        health = await asyncio.gather(
            *(service.health(ProviderCode(item["provider_code"])) for item in providers)
        )
        circuits = await service.list_circuits()
    healthy = sum(
        bool(items) and all(item["status"] == "HEALTHY" for item in items)
        for items in health
    )
    return _snapshot(
        {
            "total": len(providers),
            "healthy": healthy,
            "open_circuits": sum(item["state"] == "OPEN" for item in circuits),
        }
    )


async def _infrastructure_snapshot() -> SectionSnapshot:
    application = build_system_status_application()
    workers, clock, covers_today = await asyncio.gather(
        application.list_workers(),
        application.get_clock_status(),
        _calendar_covers_today(),
    )
    now = datetime.now(UTC)
    stale = sum(
        item.heartbeat_at is None or now - item.heartbeat_at > timedelta(minutes=2)
        for item in workers
    )
    return _snapshot(
        {
            "stale_workers": stale,
            "active_workers": len(workers) - stale,
            "calendar_covers_today": covers_today,
        },
        updated_at=clock.updated_at,
    )


async def _alert_snapshot() -> SectionSnapshot:
    return _snapshot(await _alert_counts())


async def _alert_counts() -> dict[str, int]:
    application = get_alert_application()
    queries = (
        ("OPEN", None),
        ("ACKNOWLEDGED", None),
        ("OPEN", "CRITICAL"),
        ("ACKNOWLEDGED", "CRITICAL"),
        ("OPEN", "ERROR"),
        ("ACKNOWLEDGED", "ERROR"),
    )
    results = await asyncio.gather(
        *(
            application.read(
                "list",
                status=status,
                severity=severity,
                alert_type=None,
                page=1,
                page_size=1,
            )
            for status, severity in queries
        )
    )
    totals = [total for _, total in results]
    return {
        "unresolved": totals[0] + totals[1],
        "critical": totals[2] + totals[3],
        "errors": totals[4] + totals[5],
    }


async def _all_signal_states() -> tuple[Any, ...]:
    application = get_signal_application()
    first, total = await application.list_states(page=1, page_size=200)
    pages = (total + 199) // 200
    rest = await asyncio.gather(
        *(
            application.list_states(page=page, page_size=200)
            for page in range(2, pages + 1)
        )
    )
    return tuple(first) + tuple(item for items, _ in rest for item in items)


async def _today_signal_events() -> tuple[Any, ...]:
    application = get_signal_application()
    today = (datetime.now(UTC) + timedelta(hours=8)).date()
    result: list[Any] = []
    page = 1
    while True:
        items, total = await application.list_events(page=page, page_size=200)
        current = tuple(
            item
            for item in items
            if (item.created_at + timedelta(hours=8)).date() == today
        )
        result.extend(current)
        if len(items) < 200 or len(current) < len(items) or page * 200 >= total:
            return tuple(result)
        page += 1


async def _all_targets() -> tuple[Any, ...]:
    application = get_target_application()
    first, total = await application.list(page=1, page_size=200)
    pages = (total + 199) // 200
    rest = await asyncio.gather(
        *(application.list(page=page, page_size=200) for page in range(2, pages + 1))
    )
    return tuple(first) + tuple(item for items, _ in rest for item in items)


async def _calendar_covers_today() -> bool:
    today = (datetime.now(UTC) + timedelta(hours=8)).date()
    try:
        await get_calendar_application().trading_dates(today, today)
    except Exception:
        return False
    return True


def _snapshot(
    data: dict[str, Any], *, updated_at: datetime | None = None
) -> SectionSnapshot:
    return SectionSnapshot(
        status=SectionStatus.OK,
        updated_at=updated_at or datetime.now(UTC),
        data=data,
    )


def _empty() -> SectionSnapshot:
    return SectionSnapshot(
        status=SectionStatus.EMPTY,
        updated_at=datetime.now(UTC),
        data={},
    )
