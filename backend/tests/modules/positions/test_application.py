from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from long_invest.modules.positions.application import (
    PositionApplication,
    get_position_snapshot,
)
from long_invest.modules.positions.contracts import (
    PositionSnapshot,
    PositionStatus,
    PositionView,
)
from long_invest.platform.errors import AppError


@pytest.mark.anyio
async def test_database_timeout_maps_to_stable_503() -> None:
    class BrokenDatabase:
        def transaction(self):
            raise TimeoutError

    identity = SimpleNamespace(
        security_id="00000000-0000-0000-0000-000000000001", symbol="600000.SH"
    )
    securities = SimpleNamespace(resolve_identity=lambda _symbol: identity)
    application = PositionApplication(BrokenDatabase(), security_application=securities)

    with pytest.raises(AppError) as caught:
        await application.set_status(
            symbol="600000.SH",
            target=PositionStatus.HOLDING,
            note=None,
            reason="确认持仓",
            source="manual",
            expected_version=None,
            idempotency_key="idem",
            request_id="req",
            actor_user_id="user",
            session_id="session",
            trusted_ip="127.0.0.1",
        )

    assert caught.value.code == "POSITION_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503


@pytest.mark.anyio
async def test_batch_isolates_each_item_failure() -> None:
    application = object.__new__(PositionApplication)
    calls = []

    async def set_status(**kwargs):
        calls.append(kwargs["symbol"])
        if kwargs["symbol"] == "000002.SZ":
            raise AppError(
                code="POSITION_SYMBOL_INVALID", message="bad", status_code=422
            )
        if kwargs["symbol"] == "000003.SZ":
            raise AppError(
                code="POSITION_BACKEND_UNAVAILABLE",
                message="down",
                status_code=503,
            )
        return SimpleNamespace(
            code="POSITION_CHANGED",
            position=PositionView(
                security_id="00000000-0000-0000-0000-000000000001",
                symbol=kwargs["symbol"],
                status=PositionStatus.HOLDING,
                version=1,
            ),
        )

    application.set_status = set_status
    result = await application.batch_set(
        items=(
            ("600000.SH", PositionStatus.HOLDING, None, None),
            ("000002.SZ", PositionStatus.HOLDING, None, None),
            ("000003.SZ", PositionStatus.HOLDING, None, None),
        ),
        source="manual",
        reason="批量确认",
        idempotency_key="batch",
        request_id="req",
        actor_user_id="user",
        session_id="session",
        trusted_ip="127.0.0.1",
    )

    assert calls == ["600000.SH", "000002.SZ", "000003.SZ"]
    assert [item.status for item in result] == [
        "CHANGED",
        "REJECTED",
        "FAILED",
    ]


@pytest.mark.anyio
async def test_caller_session_reads_frozen_position_snapshot() -> None:
    security_id = uuid4()
    session = SimpleNamespace()

    class Repository:
        def __init__(self, received_session):
            assert received_session is session

        async def get_current(self, received_id):
            assert received_id == security_id
            return SimpleNamespace(
                security_id=security_id,
                status="HOLDING",
                version=7,
            )

    snapshot = await get_position_snapshot(
        session, security_id, repository_factory=Repository
    )

    assert snapshot == PositionSnapshot(
        security_id=security_id,
        status=PositionStatus.HOLDING,
        version=7,
    )
    with pytest.raises(ValidationError):
        snapshot.version = 8
