from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from long_invest.modules.auth.models import AppUser, UserSession
from long_invest.platform.config.settings import AppSettings
from long_invest.platform.database.engine import Database


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class _RollbackAuthFixture(Exception):
    pass


@pytest.mark.anyio
async def test_auth_tables_enforce_single_admin_and_session_storage() -> None:
    database = Database(AppSettings(_env_file=None).database_url)
    now = datetime.now(UTC)
    try:
        with pytest.raises(_RollbackAuthFixture):
            async with database.transaction() as session:
                user = await session.scalar(select(AppUser).limit(1))
                if user is None:
                    user = AppUser(
                        username=f"integration-{uuid4().hex}",
                        password_hash="integration-test-hash",
                        password_changed_at=now,
                    )
                    session.add(user)
                    await session.flush()

                with pytest.raises(IntegrityError):
                    async with session.begin_nested():
                        session.add(
                            AppUser(
                                username=f"second-{uuid4().hex}",
                                password_hash="integration-test-hash",
                                password_changed_at=now,
                            )
                        )
                        await session.flush()

                auth_session = UserSession(
                    user_id=user.id,
                    token_digest=uuid4().hex + uuid4().hex,
                    csrf_secret_digest=uuid4().hex + uuid4().hex,
                    password_version=user.password_version,
                    created_at=now,
                    last_request_at=now,
                    last_user_activity_at=now,
                    idle_expires_at=now + timedelta(days=30),
                    absolute_expires_at=now + timedelta(days=90),
                )
                session.add(auth_session)
                await session.flush()
                assert auth_session.id is not None
                raise _RollbackAuthFixture
    finally:
        await database.dispose()


@pytest.mark.anyio
async def test_application_role_can_use_auth_tables() -> None:
    settings = AppSettings(_env_file=None)
    database = Database(settings.database_url)
    try:
        async with database.session() as session:
            privileges = {
                (table_name, privilege): await session.scalar(
                    text(
                        "SELECT has_table_privilege("
                        "current_user, :table_name, :privilege)"
                    ),
                    {"table_name": table_name, "privilege": privilege},
                )
                for table_name in ("app_user", "user_session")
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE")
            }
    finally:
        await database.dispose()

    assert all(privileges.values())
