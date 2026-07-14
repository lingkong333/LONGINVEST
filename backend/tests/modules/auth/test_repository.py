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
    auth_session = MagicMock(id=uuid4(), user_id=user.id)
    database_session = MagicMock()
    database_session.scalar = AsyncMock(side_effect=[user, True, auth_session])
    database_session.scalars = AsyncMock(return_value=[auth_session])
    database_session.flush = AsyncMock()
    repository = SqlAlchemyAuthRepository(database_session)

    assert await repository.find_user_by_username("admin") is user
    assert await repository.has_any_user() is True
    assert await repository.get_session(auth_session.id) is auth_session
    assert await repository.list_sessions(user.id) == [auth_session]
    assert await repository.add_user(user) is user
    assert await repository.add_session(auth_session) is auth_session
    await repository.flush()

    assert database_session.add.call_args_list == [
        ((user,),),
        ((auth_session,),),
    ]
    database_session.flush.assert_awaited_once()
