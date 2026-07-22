from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from long_invest.modules.auth.dependencies import require_authenticated_request
from long_invest.modules.dashboard import api as dashboard_api
from long_invest.modules.dashboard.api import get_dashboard_application, router
from long_invest.modules.dashboard.contracts import (
    DashboardStatus,
    DashboardSummary,
    DashboardTimeline,
    SectionSnapshot,
    SectionStatus,
    TimelineEntry,
)
from long_invest.platform.errors import AppError
from long_invest.platform.http.exception_handlers import register_exception_handlers

NOW = datetime(2026, 7, 22, 8, 30, tzinfo=UTC)


def test_dashboard_requires_explicit_production_configuration(monkeypatch) -> None:
    monkeypatch.setattr(dashboard_api, "_application_factory", None)

    with pytest.raises(AppError) as error:
        dashboard_api.get_dashboard_application()

    assert error.value.code == "DASHBOARD_NOT_CONFIGURED"


def _application():
    sections = {
        "system": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "quote_batches": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "monitoring": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "positions": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "signals": SectionSnapshot(SectionStatus.OK, NOW, {"today": 2}),
        "daily_data": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "targets": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "jobs": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "notifications": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "providers": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "infrastructure": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
        "alerts": SectionSnapshot(SectionStatus.EMPTY, NOW, {}),
    }
    return SimpleNamespace(
        summary=AsyncMock(
            return_value=DashboardSummary(
                DashboardStatus.HEALTHY,
                NOW,
                sections,
            )
        ),
        timeline=AsyncMock(
            return_value=DashboardTimeline(
                items=(
                    TimelineEntry(
                        id="event-1",
                        event_type="signal",
                        object_type="subscription",
                        object_id="sub-1",
                        title="Signal zone changed",
                        occurred_at=NOW,
                        details={"after_zone": "LOW_WATCH"},
                    ),
                ),
                generated_at=NOW,
            )
        ),
    )


def _app(application=None, *, authenticated=True):
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    app.dependency_overrides[get_dashboard_application] = lambda: (
        application or _application()
    )
    if authenticated:
        app.dependency_overrides[require_authenticated_request] = lambda: (
            SimpleNamespace(user=SimpleNamespace(id=uuid4()))
        )
    else:

        async def reject():
            raise AppError(code="AUTH_REQUIRED", message="login", status_code=401)

        app.dependency_overrides[require_authenticated_request] = reject
    return app


def test_dashboard_routes_require_login_and_have_typed_contracts() -> None:
    paths = {route.path: route for route in router.routes}

    for path in ("/api/v1/dashboard/summary", "/api/v1/dashboard/timeline"):
        dependencies = {item.call for item in paths[path].dependant.dependencies}
        assert require_authenticated_request in dependencies
        assert paths[path].response_model not in {None, dict}
        assert TestClient(_app(authenticated=False)).get(path).status_code == 401


def test_summary_returns_section_status_time_data_and_error() -> None:
    response = TestClient(_app()).get("/api/v1/dashboard/summary")

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "HEALTHY"
    assert data["generated_at"] == "2026-07-22T08:30:00Z"
    assert len(data["sections"]) == 12
    assert data["sections"]["signals"] == {
        "status": "OK",
        "updated_at": "2026-07-22T08:30:00Z",
        "data": {"today": 2, "low_zone": None, "high_zone": None},
        "error": None,
    }


def test_timeline_forwards_bounded_cursor_and_returns_stable_fields() -> None:
    application = _application()
    response = TestClient(_app(application)).get(
        "/api/v1/dashboard/timeline",
        params={"limit": 25, "before": "2026-07-22T08:30:00Z"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["items"][0] == {
        "id": "event-1",
        "event_type": "signal",
        "object_type": "subscription",
        "object_id": "sub-1",
        "title": "Signal zone changed",
        "occurred_at": "2026-07-22T08:30:00Z",
        "details": {"after_zone": "LOW_WATCH"},
    }
    application.timeline.assert_awaited_once_with(limit=25, before=NOW)


def test_timeline_rejects_invalid_limit() -> None:
    client = TestClient(_app())

    assert client.get("/api/v1/dashboard/timeline?limit=0").status_code == 422
    assert client.get("/api/v1/dashboard/timeline?limit=201").status_code == 422


def test_dashboard_openapi_has_distinct_typed_operations() -> None:
    paths = _app().openapi()["paths"]
    summary = paths["/api/v1/dashboard/summary"]["get"]
    timeline = paths["/api/v1/dashboard/timeline"]["get"]

    assert summary["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("DashboardSummaryEnvelope")
    assert timeline["responses"]["200"]["content"]["application/json"]["schema"][
        "$ref"
    ].endswith("DashboardTimelineEnvelope")
