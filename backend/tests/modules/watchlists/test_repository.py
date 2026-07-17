from uuid import uuid4

import pytest

from long_invest.modules.watchlists.repository import WatchlistRepository
from long_invest.platform.errors import AppError


class Result:
    rowcount = 0


class Session:
    def __init__(self):
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        return Result()


@pytest.mark.anyio
async def test_zero_row_version_update_raises_stable_conflict():
    repository = WatchlistRepository(Session())
    with pytest.raises(AppError) as caught:
        await repository.update_version(
            uuid4(), expected_version=3, name="new", description=None, display_order=0
        )
    assert caught.value.code == "WATCHLIST_VERSION_CONFLICT"
    assert caught.value.status_code == 409


@pytest.mark.anyio
async def test_security_membership_lock_uses_transaction_advisory_lock():
    session = Session()
    repository = WatchlistRepository(session)
    await repository.lock_security_memberships(uuid4())
    sql = str(session.statements[0])
    assert "pg_advisory_xact_lock" in sql
    assert "hashtextextended" in sql
