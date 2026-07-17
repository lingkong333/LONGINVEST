from sqlalchemy.dialects import postgresql

from long_invest.modules.monitoring.repository import MonitorSubscriptionRepository


def test_repository_locks_security_and_fences_transitions() -> None:
    lock = str(
        MonitorSubscriptionRepository.security_lock_statement("security").compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    transition = str(
        MonitorSubscriptionRepository.transition_statement().compile(
            dialect=postgresql.dialect()
        )
    )
    assert "pg_advisory_xact_lock" in lock
    assert "monitor_subscription.version" in transition
    assert "monitor_subscription.status" in transition


def test_enabled_schedule_query_uses_current_revision_and_enabled_status() -> None:
    statement = str(
        MonitorSubscriptionRepository.enabled_schedule_statement().compile(
            dialect=postgresql.dialect()
        )
    )

    assert "monitor_subscription_revision" in statement
    assert "current_revision_id" in statement
    assert "schedule_id IS NOT NULL" in statement
    assert "monitor_subscription.status" in statement
