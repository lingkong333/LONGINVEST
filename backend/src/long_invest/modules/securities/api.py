from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.securities.application import (
    SecurityApplication,
    get_security_application,
)
from long_invest.modules.securities.models import Security
from long_invest.platform.errors import AppError
from long_invest.platform.http.request_id import get_request_id
from long_invest.platform.http.responses import success_response

router = APIRouter(prefix="/api/v1/securities", tags=["securities"])
SecurityApplicationDependency = Annotated[
    SecurityApplication, Depends(get_security_application)
]
ReadIdentity = Annotated[
    AuthenticatedRequest, Depends(require_authenticated_request)
]
WriteIdentity = Annotated[
    AuthenticatedRequest, Depends(require_verified_write_request)
]


class RefreshRequest(BaseModel):
    confirm: bool


@router.get("")
async def list_securities(
    application: SecurityApplicationDependency,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    items, total = await application.list(page=page, page_size=page_size)
    return _page_response(items, page=page, page_size=page_size, total=total)


@router.get("/search")
async def search_securities(
    application: SecurityApplicationDependency,
    _identity: ReadIdentity,
    q: Annotated[str, Query(min_length=1, max_length=160)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    if not q.strip():
        raise AppError(
            code="SECURITY_SEARCH_QUERY_INVALID",
            message="股票搜索词不能为空",
            status_code=422,
        )
    items, total = await application.search(
        query=q.strip(), page=page, page_size=page_size
    )
    return _page_response(items, page=page, page_size=page_size, total=total)


@router.get("/{symbol}")
async def get_security(
    symbol: str,
    application: SecurityApplicationDependency,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    return success_response(data=_security_data(await application.get(symbol)))


@router.post("/refresh", status_code=202)
async def refresh_securities(
    body: RefreshRequest,
    application: SecurityApplicationDependency,
    identity: WriteIdentity,
    idempotency_key: Annotated[
        str | None, Header(alias="Idempotency-Key", max_length=160)
    ] = None,
) -> dict[str, Any]:
    if not body.confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认刷新股票主数据",
            status_code=422,
        )
    if idempotency_key is None or not idempotency_key.strip():
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="刷新股票主数据需要幂等键",
            status_code=422,
        )
    job = await application.refresh(
        idempotency_key=idempotency_key,
        request_id=get_request_id(),
        created_by_user_id=str(identity.user.id),
    )
    return success_response(
        data={
            "job_id": str(job.id),
            "job_type": job.job_type,
            "status": job.status,
        },
        message="股票主数据刷新任务已创建",
    )


def _page_response(
    items: list[Security], *, page: int, page_size: int, total: int
) -> dict[str, Any]:
    return success_response(
        data={
            "items": [_security_data(item) for item in items],
            "pagination": {"page": page, "page_size": page_size, "total": total},
        }
    )


def _security_data(security: Security) -> dict[str, Any]:
    return {
        "symbol": security.symbol,
        "exchange_code": security.exchange_code,
        "name": security.name,
        "market": security.market,
        "security_type": security.security_type,
        "listed_on": security.listed_on,
        "delisted_on": security.delisted_on,
        "listing_status": security.listing_status,
        "is_st": security.is_st,
        "is_suspended": security.is_suspended,
        "provider_codes": security.provider_codes,
        "master_version": security.master_version,
        "updated_at": security.updated_at,
    }
