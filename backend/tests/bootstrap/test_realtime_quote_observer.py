from contextlib import asynccontextmanager

import pytest

from long_invest.bootstrap import realtime_quotes
from long_invest.bootstrap.realtime_quotes import DatabaseProviderObserver


@pytest.mark.anyio
async def test_provider_observer_leaves_transaction_ownership_to_repository(
    monkeypatch,
) -> None:
    calls = []

    class Database:
        @asynccontextmanager
        async def session(self):
            yield "session"

        @asynccontextmanager
        async def transaction(self):
            raise AssertionError("observer must not nest repository transactions")
            yield

    class Repository:
        def __init__(self, session, **dependencies):
            assert session == "session"
            assert dependencies

        async def record_outcome(self, setting, **values):
            calls.append((setting, values))

    monkeypatch.setattr(realtime_quotes, "ProviderRepository", Repository)

    await DatabaseProviderObserver(Database()).record_outcome(
        "tencent-setting", success=True
    )

    assert calls == [("tencent-setting", {"success": True})]
