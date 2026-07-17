import pytest
from pydantic import ValidationError

from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.positions.api import (
    BatchPositionRequest,
    PositionChangeRequest,
    router,
)


def test_position_router_exposes_v31_routes_with_correct_auth() -> None:
    routes = {
        (method, route.path): route
        for route in router.routes
        for method in route.methods
    }
    assert set(routes) == {
        ("GET", "/api/v1/positions"),
        ("GET", "/api/v1/positions/{symbol}"),
        ("GET", "/api/v1/positions/{symbol}/history"),
        ("GET", "/api/v1/position-history"),
        ("POST", "/api/v1/positions/{symbol}/hold"),
        ("POST", "/api/v1/positions/{symbol}/clear"),
        ("POST", "/api/v1/positions/batch"),
    }
    for (method, _path), route in routes.items():
        dependencies = {item.call for item in route.dependant.dependencies}
        assert (
            require_authenticated_request
            if method == "GET"
            else require_verified_write_request
        ) in dependencies
        if method == "POST":
            assert "idempotency_key" in {
                field.name for field in route.dependant.header_params
            }
        assert route.response_model not in {None, dict}


@pytest.mark.parametrize("request_type", [PositionChangeRequest, BatchPositionRequest])
def test_position_write_reason_rejects_only_whitespace(request_type) -> None:
    data = {"reason": "   "}
    if request_type is BatchPositionRequest:
        data["items"] = [{"symbol": "600000.SH", "target": "HOLDING"}]

    with pytest.raises(ValidationError):
        request_type.model_validate(data)


def test_position_note_is_trimmed_before_500_character_limit() -> None:
    request = PositionChangeRequest.model_validate(
        {"reason": "确认持仓", "note": f"  {'x' * 500}  "}
    )

    assert request.note == "x" * 500
