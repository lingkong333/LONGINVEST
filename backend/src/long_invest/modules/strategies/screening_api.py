from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.strategies.contracts import (
    StrategyScreeningAction,
    StrategyScreeningCreateRequest,
    StrategyScreeningPeriod,
    StrategyScreeningResultStatus,
)
from long_invest.modules.strategies.screening import StrategyScreeningApplication
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.json_snapshot import thaw_json_value

router = APIRouter(
    prefix="/api/v1/strategy-screenings", tags=["strategy-screenings"]
)
_application_factory: Callable[[], StrategyScreeningApplication] | None = None


def configure_strategy_screening_application(
    factory: Callable[[], StrategyScreeningApplication],
) -> None:
    global _application_factory
    _application_factory = factory


def get_strategy_screening_application() -> StrategyScreeningApplication:
    if _application_factory is None:
        raise AppError(
            code="STRATEGY_SCREENING_NOT_CONFIGURED",
            message="策略筛选服务尚未完成生产装配",
            status_code=503,
        )
    return _application_factory()


Application = Annotated[
    StrategyScreeningApplication, Depends(get_strategy_screening_application)
]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[
    AuthenticatedRequest, Depends(require_verified_write_request)
]


class CreateScreeningBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    strategy_version_id: UUID
    parameter_snapshot: dict[str, Any] = Field(default_factory=dict)
    periods: list[StrategyScreeningPeriod] = Field(min_length=1, max_length=20)
    concurrency: int = Field(default=4, ge=1, le=64)
    confirm: StrictBool
    reason: str = Field(min_length=1, max_length=500)


class ScreeningControlBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    confirm: StrictBool
    reason: str = Field(min_length=1, max_length=500)


def idempotency_key(
    value: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=160),
    ],
) -> str:
    key = value.strip()
    if not key:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="策略筛选写操作需要有效的幂等键",
            status_code=422,
        )
    return key


IdempotencyKey = Annotated[str, Depends(idempotency_key)]


@router.get("")
async def list_screenings(
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    result = await application.list_batches(page=page, page_size=page_size)
    return success_response(data=result.model_dump(mode="json"))


@router.post("", status_code=202)
async def create_screening(
    body: CreateScreeningBody,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirmed(body.confirm)
    request = StrategyScreeningCreateRequest(
        strategy_version_id=body.strategy_version_id,
        parameter_snapshot=thaw_json_value(body.parameter_snapshot),
        periods=tuple(body.periods),
        concurrency=body.concurrency,
        idempotency_key=key,
    )
    result = await application.create(
        request,
        request_id=identity.audit_context.request_id,
        actor_user_id=str(identity.user.id),
    )
    return success_response(
        data=result.model_dump(mode="json"),
        code="JOB_ACCEPTED",
        message="全市场策略筛选已受理",
    )


@router.get("/{batch_id}")
async def get_screening(
    batch_id: UUID,
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    result = await application.get(batch_id)
    return success_response(data=result.model_dump(mode="json"))


@router.get("/{batch_id}/results")
async def list_screening_results(
    batch_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
    symbol: Annotated[
        str | None, Query(pattern=r"^[0-9]{6}\.(SH|SZ|BJ)$")
    ] = None,
    period_id: UUID | None = None,
    status: StrategyScreeningResultStatus | None = None,
) -> dict[str, Any]:
    result = await application.list_results(
        batch_id,
        page=page,
        page_size=page_size,
        symbol=symbol,
        period_id=period_id,
        status=status,
    )
    return success_response(data=result.model_dump(mode="json"))


@router.post("/{batch_id}/{action}", status_code=202)
async def control_screening(
    batch_id: UUID,
    action: StrategyScreeningAction,
    body: ScreeningControlBody,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    _confirmed(body.confirm)
    result = await application.control(
        batch_id,
        action,
        idempotency_key=key,
        actor_user_id=str(identity.user.id),
        reason=body.reason.strip(),
    )
    return success_response(
        data=result.model_dump(mode="json"),
        code="JOB_ACCEPTED",
        message="策略筛选控制请求已受理",
    )


def _confirmed(value: bool) -> None:
    if not value:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认本次策略筛选操作",
            status_code=422,
        )
