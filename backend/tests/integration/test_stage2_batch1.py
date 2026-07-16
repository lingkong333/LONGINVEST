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
