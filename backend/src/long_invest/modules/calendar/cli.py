import json
import sys
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError

from long_invest.modules.calendar.contracts import (
    CalendarAuditContext,
    CalendarImport,
)
from long_invest.modules.calendar.service import TradingCalendarService
from long_invest.platform.errors import AppError


def read_calendar_import(
    *,
    source: Path | None = None,
    stdin: BinaryIO | None = None,
) -> CalendarImport:
    if source is not None and stdin is not None:
        raise _invalid_file("只能选择文件或标准输入中的一种")
    try:
        input_stream = stdin or sys.stdin.buffer
        raw = source.read_bytes() if source is not None else input_stream.read()
        text = raw.decode("utf-8", errors="strict")
        payload = json.loads(text)
        if not isinstance(payload, dict):
            raise ValueError("顶层必须是 JSON 对象")
        return CalendarImport.model_validate(payload)
    except (
        OSError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
    ) as exc:
        raise _invalid_file(str(exc)) from exc


async def run_calendar_import(
    service: TradingCalendarService,
    *,
    source: Path | None = None,
    stdin: BinaryIO | None = None,
):
    command = read_calendar_import(source=source, stdin=stdin)
    context = CalendarAuditContext(
        request_id=f"cli_{uuid4().hex}",
        idempotency_key=command.idempotency_key,
        actor_user_id="local-cli",
        session_id="local-cli",
        trusted_ip="local-cli",
    )
    try:
        return await service.import_version(
            command.model_copy(update={"audit_context": context})
        )
    except SQLAlchemyError as exc:
        raise AppError(
            code="CALENDAR_BACKEND_UNAVAILABLE",
            message="交易日历服务暂时不可用",
            status_code=503,
        ) from exc


def _invalid_file(detail: str) -> AppError:
    return AppError(
        code="CALENDAR_IMPORT_FILE_INVALID",
        message="日历导入文件必须是有效的 UTF-8 JSON",
        status_code=422,
        details={"reason": detail[:500]},
    )
