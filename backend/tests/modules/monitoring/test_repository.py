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
