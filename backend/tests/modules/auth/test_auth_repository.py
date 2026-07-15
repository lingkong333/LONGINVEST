from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from long_invest.modules.auth.repository import SqlAlchemyAuthRepository


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_sqlalchemy_repository_owns_auth_persistence_operations() -> None:
    user = MagicMock(id=uuid4())
    updated_user = MagicMock(id=user.id, password_version=2)
    auth_session = MagicMock(id=uuid4(), user_id=user.id)
    database_session = MagicMock()
    database_session.scalar = AsyncMock(
        side_effect=[user, auth_session, updated_user, user.id]
    )
    database_session.scalars = AsyncMock(return_value=[auth_session])
    database_session.flush = AsyncMock()
    nested = MagicMock()
    nested.__aenter__ = AsyncMock(return_value=None)
    nested.__aexit__ = AsyncMock(return_value=None)
    database_session.begin_nested.return_value = nested
    repository = SqlAlchemyAuthRepository(database_session)

    assert await repository.find_user_by_username("admin") is user
    assert await repository.get_session(auth_session.id) is auth_session
    changed = await repository.advance_password_version(
        user.id,
        expected_version=1,
        password_hash="new hash",
        changed_at=MagicMock(),
    )
    assert changed is updated_user
    update_statement = database_session.scalar.await_args_list[2].args[0]
    assert "UPDATE app_user" in str(update_statement)
    assert "app_user.password_version" in str(update_statement)
    replaced = await repository.replace_password_hash(
        user.id,
        expected_version=1,
        expected_hash="old hash",
        replacement_hash="upgraded hash",
    )
    assert replaced is True
    replace_statement = database_session.scalar.await_args_list[3].args[0]
    assert "app_user.password_version" in str(replace_statement)
    assert "app_user.password_hash" in str(replace_statement)
    new_admin = MagicMock(id=uuid4())
    assert await repository.add_admin_if_absent(new_admin) is True
    assert await repository.list_sessions(user.id) == [auth_session]
    assert await repository.add_user(user) is user
    assert await repository.add_session(auth_session) is auth_session
    await repository.flush()

    assert database_session.add.call_args_list == [
        ((new_admin,),),
        ((user,),),
        ((auth_session,),),
    ]
    assert database_session.flush.await_count == 2
