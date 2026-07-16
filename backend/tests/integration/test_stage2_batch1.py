from uuid import uuid4

import pytest

from long_invest.bootstrap.app import create_app
from long_invest.bootstrap.jobs import security_master_refresh
from long_invest.entrypoints.cli import build_parser
from long_invest.platform.jobs.contracts import JobExecutionContext


def test_reviewed_stage2_routes_are_registered() -> None:
    paths = create_app().openapi()["paths"]

    assert "get" in paths["/api/v1/securities"]
    assert "post" in paths["/api/v1/securities/refresh"]
    assert "get" in paths["/api/v1/trading-calendar"]
    assert "post" in paths["/api/v1/trading-calendar/import"]
    assert "get" in paths["/api/v1/providers"]
    assert "post" in paths["/api/v1/providers/quote-diagnostics"]


def test_market_data_routes_are_registered() -> None:
    paths = create_app().openapi()["paths"]

    assert "get" in paths["/api/v1/quote-cycles"]
    assert "get" in paths["/api/v1/quote-cycles/{cycle_id}/items"]
    assert "post" in paths["/api/v1/quote-cycles/manual"]
    assert "post" in paths["/api/v1/quotes/diagnose"]
    assert "get" in paths["/api/v1/daily-data/batches"]
    assert "get" in paths["/api/v1/daily-data/batches/{batch_id}/missing"]
    assert "post" in paths["/api/v1/daily-data/batches/{batch_id}/retry"]
    assert "get" in paths["/api/v1/daily-bars/{symbol}"]
    assert "get" in paths["/api/v1/daily-bars/{symbol}/revisions"]


def test_market_data_success_responses_publish_concrete_schemas() -> None:
    paths = create_app().openapi()["paths"]
    operations = (
        ("/api/v1/quote-cycles", "get", "200"),
        ("/api/v1/quote-cycles/{cycle_id}/items", "get", "200"),
        ("/api/v1/quote-cycles/manual", "post", "202"),
        ("/api/v1/quotes/diagnose", "post", "202"),
        ("/api/v1/daily-data/batches", "get", "200"),
        ("/api/v1/daily-data/batches/{batch_id}/missing", "get", "200"),
        ("/api/v1/daily-data/batches/{batch_id}/retry", "post", "202"),
        ("/api/v1/daily-bars/{symbol}", "get", "200"),
        ("/api/v1/daily-bars/{symbol}/revisions", "get", "200"),
    )

    for path, method, status in operations:
        schema = paths[path][method]["responses"][status]["content"][
            "application/json"
        ]["schema"]
        assert "$ref" in schema, f"{method.upper()} {path} has no response model"


def test_daily_retry_publishes_required_idempotency_header() -> None:
    operation = create_app().openapi()["paths"][
        "/api/v1/daily-data/batches/{batch_id}/retry"
    ]["post"]
    header = next(
        parameter
        for parameter in operation["parameters"]
        if parameter["in"] == "header" and parameter["name"] == "Idempotency-Key"
    )

    assert header["required"] is True


def test_calendar_import_is_available_from_the_main_cli() -> None:
    args = build_parser().parse_args(
        ["calendar", "import", "--file", "calendar.json"]
    )

    assert args.group == "calendar"
    assert args.command == "import"
    assert args.file == "calendar.json"


@pytest.mark.anyio
async def test_security_refresh_worker_rejects_missing_frozen_context() -> None:
    result = await security_master_refresh(
        JobExecutionContext(
            job_id=uuid4(),
            fence_token=uuid4(),
            config={"source": "eastmoney"},
        )
    )

    assert result.success is False
    assert result.code == "SECURITY_REFRESH_CONFIG_INVALID"
