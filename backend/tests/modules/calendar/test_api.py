from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.calendar.api import router


def test_calendar_router_exposes_exactly_the_nine_v31_routes() -> None:
    routes = {
        (method, route.path) for route in router.routes for method in route.methods
    }

    assert routes == {
        ("GET", "/api/v1/trading-calendar"),
        ("GET", "/api/v1/trading-calendar/{date}"),
        ("GET", "/api/v1/trading-calendar/coverage"),
        ("GET", "/api/v1/trading-calendar/next-trading-day"),
        ("GET", "/api/v1/trading-calendar/previous-trading-day"),
        ("PATCH", "/api/v1/trading-calendar/{date}"),
        ("POST", "/api/v1/trading-calendar/import"),
        ("GET", "/api/v1/trading-calendar/versions"),
        ("POST", "/api/v1/trading-calendar/versions/{version_id}/restore"),
    }


def test_read_and_write_routes_use_the_published_auth_dependencies() -> None:
    for route in router.routes:
        dependency_calls = {item.call for item in route.dependant.dependencies}
        if "GET" in route.methods:
            assert require_authenticated_request in dependency_calls
        else:
            assert require_verified_write_request in dependency_calls


def test_write_schemas_require_confirm_reason_and_idempotency_header() -> None:
    for route in router.routes:
        if route.methods & {"POST", "PATCH"}:
            dependency_names = {
                field.name for field in route.dependant.header_params
            }
            assert "idempotency_key" in dependency_names
