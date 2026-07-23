import pytest
from pydantic import ValidationError

from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.positions.api import (
    BatchPositionRequest,
    PositionChangeRequest,
    _position_data,
    router,
)
from long_invest.modules.positions.contracts import PositionStatus, PositionView


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


def test_position_response_exposes_backend_allowed_actions() -> None:
    holding = PositionView(
        security_id="00000000-0000-0000-0000-000000000001",
        symbol="600000.SH",
        status=PositionStatus.HOLDING,
        version=2,
    )
    not_holding = holding.model_copy(
        update={"status": PositionStatus.NOT_HOLDING}
    )

    assert _position_data(holding)["allowed_actions"] == ["CLEAR"]
    assert _position_data(not_holding)["allowed_actions"] == ["HOLD"]


def test_position_lists_publish_server_pagination_parameters() -> None:
    parameters = {
        parameter.name
        for route in router.routes
        if route.path in {
            "/api/v1/positions",
            "/api/v1/position-history",
            "/api/v1/positions/{symbol}/history",
        }
        for parameter in route.dependant.query_params
    }

    assert parameters == {"page", "page_size"}
