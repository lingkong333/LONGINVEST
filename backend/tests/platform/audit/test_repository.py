from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from long_invest.platform.audit.models import AuditEvent
from long_invest.platform.audit.repository import AuditRepository, NewAuditEvent
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def new_audit_event() -> NewAuditEvent:
    unique = uuid4().hex
    return NewAuditEvent(
        action_code="FOUNDATION_TEST",
        object_type="test_case",
        object_id=unique,
        result="SUCCESS",
        request_id=f"req_{unique}",
        idempotency_key=f"audit_{unique}",
        risk_level="LOW",
        reason="验证只追加审计",
        before_summary=None,
        after_summary={"status": "created"},
    )


@pytest.mark.anyio
async def test_audit_repository_appends_an_immutable_event() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    event_data = new_audit_event()
    try:
        async with database.transaction() as session:
            created = await AuditRepository(session).append(event_data)

        async with database.session() as session:
            stored = await session.scalar(
                select(AuditEvent).where(AuditEvent.id == created.id)
            )
        assert stored is not None
        assert stored.action_code == event_data.action_code
        assert stored.request_id == event_data.request_id
        assert stored.after_summary == {"status": "created"}
        assert stored.occurred_at.tzinfo is not None
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_database_rejects_audit_event_updates() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    try:
        async with database.transaction() as session:
            created = await AuditRepository(session).append(new_audit_event())

        with pytest.raises(DBAPIError):
            async with database.transaction() as session:
                await session.execute(
                    update(AuditEvent)
                    .where(AuditEvent.id == created.id)
                    .values(result="FAILED")
                )
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_application_role_has_only_safe_audit_permissions() -> None:
    settings = AppSettings(_env_file=None)
    database = Database(settings.database_url)
    try:
        async with database.session() as session:
            current_user = await session.scalar(text("SELECT current_user"))
            can_select = await session.scalar(
                text(
                    "SELECT has_table_privilege("
                    "current_user, 'audit_event', 'SELECT')"
                )
            )
            can_insert = await session.scalar(
                text(
                    "SELECT has_table_privilege("
                    "current_user, 'audit_event', 'INSERT')"
                )
            )
            can_update = await session.scalar(
                text(
                    "SELECT has_table_privilege("
                    "current_user, 'audit_event', 'UPDATE')"
                )
            )
            can_delete = await session.scalar(
                text(
                    "SELECT has_table_privilege("
                    "current_user, 'audit_event', 'DELETE')"
                )
            )
    finally:
        await database.dispose()

    assert current_user == settings.database_app_role
    assert can_select is True
    assert can_insert is True
    assert can_update is False
    assert can_delete is False
