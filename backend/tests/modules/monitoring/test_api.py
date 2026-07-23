from typing import get_origin
from uuid import uuid4

import pytest
from fastapi.routing import APIRoute

from long_invest.modules.auth.dependencies import (
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.monitoring.api import (
    TransitionRequest,
    _owner,
    check_now,
    router,
)
from long_invest.modules.monitoring.contracts import SubscriptionStatus
from long_invest.platform.errors import AppError


def test_api_exposes_concrete_authenticated_lifecycle() -> None:
    paths = {
        (r.path, m) for r in router.routes if isinstance(r, APIRoute) for m in r.methods
    }
    expected = {
        ("/api/v1/monitor-subscriptions", "GET"),
        ("/api/v1/monitor-subscriptions", "POST"),
        ("/api/v1/monitor-subscriptions/{subscription_id}", "PATCH"),
        ("/api/v1/monitor-subscriptions/{subscription_id}/enable", "POST"),
        ("/api/v1/monitor-subscriptions/{subscription_id}/disable", "POST"),
        ("/api/v1/monitor-subscriptions/{subscription_id}/archive", "POST"),
        ("/api/v1/monitor-subscriptions/{subscription_id}/restore", "POST"),
        ("/api/v1/monitor-subscriptions/{subscription_id}/check-now", "POST"),
        ("/api/v1/monitor-subscriptions/{subscription_id}/diagnose", "POST"),
        (
            "/api/v1/monitor-subscriptions/{subscription_id}/notification-policy",
            "GET",
        ),
        (
            "/api/v1/monitor-subscriptions/{subscription_id}/notification-policy",
            "PATCH",
        ),
    }
    assert expected <= paths
    for route in (r for r in router.routes if isinstance(r, APIRoute)):
        assert get_origin(route.response_model) is not dict
        dependency = (
            require_authenticated_request
            if route.methods == {"GET"}
            else require_verified_write_request
        )
        assert any(item.call is dependency for item in route.dependant.dependencies)
        if route.methods & {"POST", "PATCH", "PUT", "DELETE"}:
            assert any(
                p.alias == "Idempotency-Key" and p.field_info.is_required()
                for p in route.dependant.header_params
            )


@pytest.mark.anyio
async def test_check_now_requires_confirmation_before_capability_error() -> None:
    with pytest.raises(AppError) as caught:
        await check_now(
            uuid4(),
            TransitionRequest(expected_version=1, reason="检查", confirm=False),
            object(),
            object(),
            "key",
        )
    assert caught.value.status_code == 422


def test_subscription_response_includes_backend_allowed_actions() -> None:
    owner = type(
        "Owner",
        (),
        {
            "id": uuid4(),
            "security_id": uuid4(),
            "symbol": "600000.SH",
            "status": SubscriptionStatus.ENABLED,
            "version": 3,
            "current_revision_id": uuid4(),
            "archived_at": None,
        },
    )()

    assert _owner(owner)["allowed_actions"] == [
        "DISABLE",
        "CHECK_NOW",
        "DIAGNOSE",
    ]
