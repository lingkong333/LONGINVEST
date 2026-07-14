from typing import Any

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from long_invest.platform.errors import AppError
from long_invest.platform.http.request_id import REQUEST_ID_HEADER
from long_invest.platform.http.responses import failure_response

logger = structlog.get_logger(__name__)

VALIDATION_MESSAGES = {
    "missing": "必填项不能为空",
    "greater_than_equal": "数值低于允许范围",
    "less_than_equal": "数值高于允许范围",
    "int_parsing": "必须填写整数",
    "string_type": "必须填写文本",
}


def _validation_fields(exc: RequestValidationError) -> dict[str, str]:
    fields: dict[str, str] = {}
    for error in exc.errors():
        location = ".".join(str(part) for part in error["loc"])
        fields[location] = VALIDATION_MESSAGES.get(
            str(error["type"]),
            "输入格式不正确",
        )
    return fields


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return _failure_json_response(
        status_code=exc.status_code,
        code=exc.code,
        message=exc.message,
        details=exc.details,
    )


async def validation_error_handler(
    _request: Request,
    exc: RequestValidationError,
) -> JSONResponse:
    return _failure_json_response(
        status_code=422,
        code="VALIDATION_ERROR",
        message="请求参数校验失败",
        details={"fields": _validation_fields(exc)},
    )


async def unknown_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    logger.exception("unhandled_request_error", error_type=type(exc).__name__)
    return _failure_json_response(
        status_code=500,
        code="INTERNAL_ERROR",
        message="服务器内部错误",
        include_request_id_header=True,
    )


def _failure_json_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict[str, Any] | None = None,
    include_request_id_header: bool = False,
) -> JSONResponse:
    content = failure_response(
        code=code,
        message=message,
        details=details,
    )
    headers = (
        {REQUEST_ID_HEADER: content["request_id"]}
        if include_request_id_header
        else None
    )
    return JSONResponse(
        status_code=status_code,
        content=content,
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(AppError, app_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(  # type: ignore[arg-type]
        RequestValidationError,
        validation_error_handler,
    )
    app.add_exception_handler(Exception, unknown_error_handler)
