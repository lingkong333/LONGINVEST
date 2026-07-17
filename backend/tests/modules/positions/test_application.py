from types import SimpleNamespace

import pytest

from long_invest.modules.positions.application import PositionApplication
from long_invest.modules.positions.contracts import PositionStatus, PositionView
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
