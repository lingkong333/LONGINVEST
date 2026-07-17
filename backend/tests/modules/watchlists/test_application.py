from contextlib import asynccontextmanager
from uuid import uuid4

import pytest
from sqlalchemy.exc import OperationalError

from long_invest.modules.securities.contracts import (
    ListingStatus,
    Market,
    SecurityIdentity,
    SecurityType,
)
from long_invest.modules.watchlists.application import WatchlistApplication
from long_invest.modules.watchlists.contracts import (
    WatchlistBatchInput,
    WatchlistBatchStatus,
    WatchlistItemView,
)
from long_invest.modules.watchlists.service import WatchlistService
from long_invest.platform.errors import AppError


class Database:
    def __init__(self):
        self.rollbacks = 0

    @asynccontextmanager
    async def transaction(self):
        try:
            yield object()
        except Exception:
            self.rollbacks += 1
            raise

    @asynccontextmanager
    async def session(self):
        yield object()


class Securities:
    async def resolve_identity(self, symbol):
        if symbol == "BAD":
            raise AppError(code="SECURITY_NOT_FOUND", message="bad", status_code=404)
        if symbol == "BOOM":
            raise RuntimeError("unexpected")
        return SecurityIdentity(
            security_id=uuid4(),
            symbol=symbol,
            market=Market.SH,
            security_type=SecurityType.A_SHARE,
            listing_status=ListingStatus.LISTED,
            is_suspended=False,
            is_st=False,
            listed_on=None,
            delisted_on=None,
            master_version=1,
        )


class Service:
    version = 1

    async def add_item(self, watchlist_id, **values):
        result = type("Result", (), {})()
        result.created = True
        result.version = self.version + 1
        result.item = WatchlistItemView(
            id=uuid4(),
            watchlist_id=watchlist_id,
            security_id=values["security"].security_id,
            symbol=values["security"].symbol,
            source=values["source"],
        )
        self.version += 1
        return result


@pytest.mark.anyio
async def test_batch_uses_one_transaction_per_item_and_returns_concrete_statuses():
    database = Database()
    application = WatchlistApplication(
        database,
        security_application=Securities(),
        service_factory=lambda *args, **kwargs: Service(),
    )
    results = await application.add_batch(
        uuid4(),
        owner_user_id=uuid4(),
        batch=WatchlistBatchInput(symbols=("600000.SH", "BAD", "BOOM")),
        source="manual",
        reason="批量添加",
        idempotency_key="batch",
        expected_version=1,
    )
    assert [item.status for item in results] == [
        WatchlistBatchStatus.CREATED,
        WatchlistBatchStatus.REJECTED,
        WatchlistBatchStatus.FAILED,
    ]


@pytest.mark.anyio
async def test_batch_classifies_backend_unavailable_as_failed():
    class UnavailableSecurities(Securities):
        async def resolve_identity(self, symbol):
            raise AppError(
                code="MONITOR_BACKEND_UNAVAILABLE", message="down", status_code=503
            )

    application = WatchlistApplication(
        Database(),
        security_application=UnavailableSecurities(),
        service_factory=lambda *args, **kwargs: Service(),
    )
    results = await application.add_batch(
        uuid4(),
        owner_user_id=uuid4(),
        batch=WatchlistBatchInput(symbols=("600000.SH",)),
        source="manual",
        reason="批量添加",
        idempotency_key="batch",
        expected_version=1,
    )
    assert results[0].status is WatchlistBatchStatus.FAILED


@pytest.mark.anyio
async def test_database_timeout_maps_to_stable_503():
    class BrokenDatabase(Database):
        @asynccontextmanager
        async def session(self):
            raise OperationalError("select", {}, TimeoutError())
            yield

    application = WatchlistApplication(
        BrokenDatabase(), security_application=Securities()
    )
    with pytest.raises(AppError) as caught:
        await application.list(owner_user_id=uuid4())
    assert caught.value.code == "MONITOR_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503


@pytest.mark.anyio
async def test_outbox_failure_rolls_back_transaction():
    database = Database()

    record = type(
        "Record",
        (),
        {
            "id": uuid4(),
            "owner_user_id": uuid4(),
            "name": "观察",
            "description": None,
            "display_order": 0,
            "version": 1,
            "archived_at": None,
        },
    )()

    class Repository:
        async def find_replay(self, key):
            return None

        async def get(self, watchlist_id, *, lock=False):
            return record

        async def archive(self, watchlist_id, *, expected_version):
            record.version = 2
            record.archived_at = object()
            return record

        async def list_items(self, watchlist_id):
            return ()

    class Audit:
        async def append(self, event):
            return None

    class BrokenEvents:
        async def updated(self, **values):
            raise RuntimeError("outbox failed")

    service = WatchlistService(Repository(), Audit(), BrokenEvents())
    application = WatchlistApplication(
        database,
        security_application=Securities(),
        service_factory=lambda *args, **kwargs: service,
    )
    with pytest.raises(RuntimeError):
        await application.archive(
            record.id,
            owner_user_id=record.owner_user_id,
            reason="归档",
            idempotency_key="archive",
            expected_version=1,
        )
    assert database.rollbacks == 1
