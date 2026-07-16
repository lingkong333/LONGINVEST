from uuid import uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from long_invest.modules.watchlists.contracts import (
    WatchlistBatchInput,
    WatchlistBatchItem,
    WatchlistBatchStatus,
    WatchlistItemView,
    WatchlistMutation,
    WatchlistView,
)


def test_watchlist_contracts_are_frozen_and_strict() -> None:
    command = WatchlistMutation(
        name="  长线观察  ",
        description=" 核心分组 ",
        display_order=2,
        reason="调整分组",
        idempotency_key="watchlist-1",
        expected_version=1,
    )
    assert command.name == "长线观察"
    assert command.description == "核心分组"
    with pytest.raises(ValidationError):
        command.name = "other"
    with pytest.raises(ValidationError):
        WatchlistMutation(
            name="x",
            display_order=0,
            reason="r",
            idempotency_key="k",
            unexpected=True,
        )


def test_watchlist_batch_and_views_are_frozen() -> None:
    item = WatchlistItemView(
        id=uuid4(), watchlist_id=uuid4(), security_id=uuid4(), symbol="600000.SH"
    )
    view = WatchlistView(
        id=item.watchlist_id,
        owner_user_id=uuid4(),
        name="观察",
        description=None,
        display_order=1,
        version=1,
        archived=False,
        items=(item,),
    )
    batch = WatchlistBatchItem(
        symbol="600000.SH", status=WatchlistBatchStatus.CREATED, item=item
    )
    assert view.items == (item,)
    assert batch.status is WatchlistBatchStatus.CREATED
    assert {status.value for status in WatchlistBatchStatus} == {
        "CREATED",
        "REUSED",
        "REJECTED",
        "FAILED",
    }


def test_watchlist_rejects_blank_text_and_empty_batch() -> None:
    with pytest.raises(ValidationError):
        WatchlistMutation(
            name="   ",
            display_order=0,
            reason="reason",
            idempotency_key="key",
        )
    with pytest.raises(ValidationError):
        WatchlistMutation(
            name="name",
            display_order=0,
            reason="   ",
            idempotency_key="key",
        )
    with pytest.raises(ValidationError):
        WatchlistBatchInput(symbols=[])
    values = ["600000.SH"]
    batch = WatchlistBatchInput(symbols=values)
    values.append("000001.SZ")
    assert batch.symbols == ("600000.SH",)


@pytest.mark.parametrize("value", [None, "600000.SH", 123])
def test_watchlist_batch_rejects_null_and_scalar_values(value) -> None:
    with pytest.raises(ValidationError):
        WatchlistBatchInput(symbols=value)


@pytest.mark.parametrize("field", ["name", "description", "reason", "idempotency_key"])
def test_watchlist_text_validators_reject_wrong_json_types(field) -> None:
    values = {
        "name": "name",
        "description": "description",
        "display_order": 0,
        "reason": "reason",
        "idempotency_key": "key",
    }
    values[field] = 123
    with pytest.raises(ValidationError):
        WatchlistMutation(**values)


@pytest.mark.anyio
async def test_wrong_watchlist_json_type_returns_fastapi_422() -> None:
    app = FastAPI()

    @app.post("/watchlists")
    def create_watchlist(command: WatchlistMutation) -> dict[str, str]:
        return {"name": command.name}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/watchlists",
            json={
                "name": 123,
                "display_order": 0,
                "reason": "reason",
                "idempotency_key": "key",
            },
        )
    assert response.status_code == 422
