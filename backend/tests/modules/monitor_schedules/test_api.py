from datetime import time
from types import SimpleNamespace
from typing import get_origin

from fastapi.routing import APIRoute

from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.monitor_schedules.api import _revision_data, router


def test_api_exposes_authenticated_schedule_lifecycle_contract() -> None:
    routes = {
        (route.path, method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert ("/api/v1/monitor-schedules", "GET") in routes
    assert ("/api/v1/monitor-schedules", "POST") in routes
    assert ("/api/v1/monitor-schedules/{schedule_id}", "GET") in routes
    assert ("/api/v1/monitor-schedules/{schedule_id}", "PATCH") in routes
    assert ("/api/v1/monitor-schedules/{schedule_id}/archive", "POST") in routes
    assert ("/api/v1/monitor-schedules/{schedule_id}/versions", "GET") in routes
    assert (
        "/api/v1/monitor-schedules/{schedule_id}/versions/{revision_id}/restore",
        "POST",
    ) in routes
    assert all(
        route.response_model is not None
        and get_origin(route.response_model) is not dict
        for route in router.routes
        if isinstance(route, APIRoute)
    )


def test_every_write_route_requires_idempotency_header() -> None:
    writes = [
        route
        for route in router.routes
        if isinstance(route, APIRoute)
        and route.methods & {"POST", "PATCH", "PUT", "DELETE"}
    ]
    for route in writes:
        assert any(
            param.alias == "Idempotency-Key" and param.field_info.is_required()
            for param in route.dependant.header_params
        ), route.path
        assert any(
            dependency.call is require_verified_write_request
            for dependency in route.dependant.dependencies
        ), route.path


def test_every_read_route_requires_login() -> None:
    reads = [
        route
        for route in router.routes
        if isinstance(route, APIRoute) and "GET" in route.methods
    ]
    for route in reads:
        assert any(
            dependency.call is require_authenticated_request
            for dependency in route.dependant.dependencies
        ), route.path


def test_revision_times_are_exact_hh_mm_strings() -> None:
    data = _revision_data(
        SimpleNamespace(
            id="revision",
            schedule_id="schedule",
            revision_no=1,
            times=(time(9, 45), time(14, 30)),
            timezone="Asia/Shanghai",
            reason="初始配置",
            created_at="now",
        )
    )
    assert data["times"] == ["09:45", "14:30"]
