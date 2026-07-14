from datetime import UTC, datetime
from typing import Any

from long_invest.platform.http.request_id import get_request_id


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def success_response(
    *,
    data: Any,
    message: str = "操作成功",
    code: str = "OK",
) -> dict[str, Any]:
    return {
        "success": True,
        "code": code,
        "message": message,
        "data": data,
        "request_id": get_request_id(),
        "server_time": utc_now_iso(),
    }


def failure_response(
    *,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "code": code,
        "message": message,
        "data": None,
        "details": details,
        "request_id": get_request_id(),
        "server_time": utc_now_iso(),
    }
