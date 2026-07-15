import io
import json

import pytest

from long_invest.modules.calendar.cli import read_calendar_import, run_calendar_import
from long_invest.platform.errors import AppError


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
