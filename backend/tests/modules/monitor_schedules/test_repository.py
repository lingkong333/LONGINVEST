from sqlalchemy.dialects import postgresql

from long_invest.modules.monitor_schedules.repository import MonitorScheduleRepository


def test_repository_mutations_are_version_fenced_and_locks_rows() -> None:
    lock_sql = str(
        MonitorScheduleRepository.lock_statement().compile(dialect=postgresql.dialect())
    )
    switch_sql = str(
        MonitorScheduleRepository.switch_statement().compile(
            dialect=postgresql.dialect()
        )
    )
    archive_sql = str(
        MonitorScheduleRepository.archive_statement().compile(
            dialect=postgresql.dialect()
        )
    )
    assert "FOR UPDATE" in lock_sql
    assert "monitor_schedule.version" in switch_sql
    assert "monitor_schedule.version" in archive_sql
    assert "archived_at IS NULL" in archive_sql


def test_create_idempotency_uses_a_transaction_advisory_lock() -> None:
    statement = MonitorScheduleRepository.idempotency_lock_statement("create-1")
    sql = str(
        statement.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "pg_advisory_xact_lock" in sql
    assert "monitor-schedule:create:create-1" in sql
