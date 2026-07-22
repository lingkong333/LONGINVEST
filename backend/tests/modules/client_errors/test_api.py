from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from long_invest.modules.client_errors.api import collect_client_error, router
from long_invest.modules.client_errors.contracts import ClientErrorInput
from long_invest.modules.client_errors.service import ClientErrorCollector


def payload() -> dict:
    return {
        "route": "/dashboard",
        "frontend_version": "1.2.3",
        "error_type": "TypeError",
        "message": "render failed",
        "browser_summary": "Chrome 140 on Linux",
        "request_id": "req_12345678",
        "occurred_at": datetime(2026, 7, 22, tzinfo=UTC),
    }


def test_contract_rejects_form_content_or_credentials_as_extra_fields() -> None:
    with pytest.raises(ValidationError):
        ClientErrorInput.model_validate({**payload(), "cookie": "secret"})

    with pytest.raises(ValidationError):
        ClientErrorInput.model_validate({**payload(), "form_data": {"code": "x"}})

    with pytest.raises(ValidationError):
        ClientErrorInput.model_validate({**payload(), "route": "/target?code=secret"})


@pytest.mark.anyio
async def test_endpoint_returns_bounded_receipt_without_echoing_error() -> None:
    response = await collect_client_error(
        ClientErrorInput.model_validate(payload()),
        ClientErrorCollector(),
    )

    assert response["code"] == "CLIENT_ERROR_ACCEPTED"
    assert response["data"]["sampled"] is True
    assert len(response["data"]["fingerprint"]) == 24
    assert "message" not in response["data"]


def test_router_exposes_only_frontend_error_ingest() -> None:
    methods = {
        (route.path, method) for route in router.routes for method in route.methods
    }
    assert methods == {("/api/v1/client-errors", "POST")}

    app = FastAPI()
    app.include_router(router)
    schema = app.openapi()["paths"]["/api/v1/client-errors"]["post"]
    response = schema["responses"]["202"]["content"]["application/json"]
    assert response["schema"]["$ref"].endswith("ClientErrorEnvelope")
