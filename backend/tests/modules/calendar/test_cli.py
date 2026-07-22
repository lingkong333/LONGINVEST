import io
import json
from pathlib import Path

import pytest
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.calendar.cli import read_calendar_import, run_calendar_import
from long_invest.platform.errors import AppError

PROJECT_ROOT = Path(__file__).parents[4]


def valid_payload() -> dict:
    return {
        "market": "CN_A",
        "source": "git",
        "source_version": "2026.1",
        "idempotency_key": "calendar-2026",
        "days": [
            {
                "trade_date": "2026-07-15",
                "is_trading_day": True,
                "status": "CONFIRMED",
                "sessions": [{"starts_at": "09:30", "ends_at": "15:00"}],
            }
        ],
    }


def test_cli_reads_utf8_json_from_stdin_or_file(tmp_path) -> None:
    encoded = json.dumps(valid_payload(), ensure_ascii=False).encode("utf-8")
    source = tmp_path / "calendar.json"
    source.write_bytes(encoded)

    assert read_calendar_import(source=source).market == "CN_A"
    assert read_calendar_import(stdin=io.BytesIO(encoded)).days[0].is_trading_day


def test_bundled_2026_calendar_matches_official_closures() -> None:
    command = read_calendar_import(
        source=PROJECT_ROOT / "deploy/data/trading-calendar/CN_A-2026.json"
    )
    days = {day.trade_date.isoformat(): day for day in command.days}

    assert command.market == "CN_A"
    assert command.source == "SSE_OFFICIAL"
    assert len(days) == 365
    assert days["2026-02-14"].is_trading_day is False
    assert days["2026-02-23"].is_trading_day is False
    assert days["2026-02-24"].is_trading_day is True
    assert days["2026-10-07"].is_trading_day is False
    assert days["2026-10-08"].is_trading_day is True


@pytest.mark.parametrize(
    "payload",
    [
        b"\xff\xfe",
        json.dumps({**valid_payload(), "script": "weekday()"}).encode(),
        b"weekday()",
    ],
)
def test_cli_rejects_invalid_encoding_unknown_fields_and_scripts(payload) -> None:
    with pytest.raises(AppError) as caught:
        read_calendar_import(stdin=io.BytesIO(payload))

    assert caught.value.code == "CALENDAR_IMPORT_FILE_INVALID"


@pytest.mark.anyio
async def test_cli_handler_returns_service_result_without_committing() -> None:
    class Service:
        def __init__(self) -> None:
            self.command = None

        async def import_version(self, command):
            self.command = command
            return {"created": True}

    service = Service()
    encoded = json.dumps(valid_payload()).encode()

    result = await run_calendar_import(service, stdin=io.BytesIO(encoded))

    assert result == {"created": True}
    assert service.command.market == "CN_A"
    assert service.command.audit_context.request_id.startswith("cli_")
    assert service.command.audit_context.actor_user_id == "local-cli"
    assert service.command.audit_context.session_id == "local-cli"
    assert service.command.audit_context.trusted_ip == "local-cli"


@pytest.mark.anyio
async def test_cli_maps_database_failure_to_stable_503() -> None:
    class Service:
        async def import_version(self, _command):
            raise SQLAlchemyError("database unavailable")

    with pytest.raises(AppError) as caught:
        await run_calendar_import(
            Service(),
            stdin=io.BytesIO(json.dumps(valid_payload()).encode()),
        )

    assert caught.value.code == "CALENDAR_BACKEND_UNAVAILABLE"
    assert caught.value.status_code == 503
