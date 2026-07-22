from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Request
from pydantic import Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.backtests.application import BacktestApplication
from long_invest.modules.backtests.contracts import (
    BacktestCreateRequest,
    BacktestResultView,
)
from long_invest.modules.backtests.service import BacktestCommandContext
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.json_snapshot import thaw_json_value

router = APIRouter(prefix="/api/v1/backtests", tags=["backtests"])
_TASK_NAMESPACE = UUID("f204bc8f-9228-5d92-a923-c2c46dd775e4")
_application_factory: Callable[[], BacktestApplication] | None = None


def configure_backtest_application(
    factory: Callable[[], BacktestApplication],
) -> None:
    global _application_factory
    _application_factory = factory


def get_backtest_application() -> BacktestApplication:
    if _application_factory is None:
        raise AppError(
            code="BACKTEST_NOT_CONFIGURED",
            message="回测服务尚未完成生产装配",
            status_code=503,
        )
    return _application_factory()


Application = Annotated[BacktestApplication, Depends(get_backtest_application)]
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
WriteIdentity = Annotated[
    AuthenticatedRequest, Depends(require_verified_write_request)
]


class CreateBacktestBody(BacktestCreateRequest):
    confirm: StrictBool
    reason: str = Field(min_length=1, max_length=200)


def idempotency_key(request: Request) -> str:
    value = request.headers.get("Idempotency-Key", "").strip()
    if not value or len(value) > 160:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="回测创建需要有效的幂等键",
            status_code=422,
        )
    return value


IdempotencyKey = Annotated[str, Depends(idempotency_key)]


@router.post("")
async def create_backtest(
    body: CreateBacktestBody,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    if not body.confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message="请确认本次回测创建操作",
            status_code=422,
        )
    if not body.reason.strip():
        raise AppError(
            code="BACKTEST_INPUT_INVALID",
            message="操作原因不能为空",
            status_code=422,
        )
    request = BacktestCreateRequest(
        symbol=body.symbol,
        date_range=body.date_range,
        strategy_version_id=body.strategy_version_id,
        draft_id=body.draft_id,
        draft_version=body.draft_version,
        strategy_metadata=(
            thaw_json_value(body.strategy_metadata)
            if body.strategy_metadata is not None
            else None
        ),
        parameter_schema=(
            thaw_json_value(body.parameter_schema)
            if body.parameter_schema is not None
            else None
        ),
        parameter_snapshot=thaw_json_value(body.parameter_snapshot),
        initial_capital=body.initial_capital,
    )
    state = await application.create(
        task_id=uuid5(_TASK_NAMESPACE, key),
        request=request,
        context=BacktestCommandContext(
            request_id=identity.audit_context.request_id,
            idempotency_key=key,
            actor_user_id=str(identity.user.id),
            reason=body.reason.strip(),
        ),
    )
    return success_response(
        data=_execution(state),
        code="BACKTEST_CREATED",
        message="回测任务已创建",
    )


@router.get("/{task_id}")
async def get_backtest(
    task_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data=_execution(await application.get_execution(task_id)))


@router.get("/{task_id}/items/{item_id}")
async def get_backtest_item(
    task_id: UUID,
    item_id: UUID,
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    result = await application.get_result(task_id, item_id)
    return success_response(data=_result(result))


@router.get("/{task_id}/items/{item_id}/target-adjustments")
async def get_target_adjustments(
    task_id: UUID,
    item_id: UUID,
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    result = await application.get_result(task_id, item_id)
    return success_response(data=_models(result.adjustments))


@router.get("/{task_id}/items/{item_id}/orders")
async def get_orders(
    task_id: UUID,
    item_id: UUID,
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    result = await application.get_result(task_id, item_id)
    return success_response(data=_models(result.orders))


@router.get("/{task_id}/items/{item_id}/trades")
async def get_trades(
    task_id: UUID,
    item_id: UUID,
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    result = await application.get_result(task_id, item_id)
    return success_response(data=_models(result.trades))


@router.get("/{task_id}/items/{item_id}/daily-results")
async def get_daily_results(
    task_id: UUID,
    item_id: UUID,
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    result = await application.get_result(task_id, item_id)
    return success_response(data=_models(result.daily_results))


@router.get("/{task_id}/items/{item_id}/metric")
async def get_metric(
    task_id: UUID,
    item_id: UUID,
    application: Application,
    _identity: ReadIdentity,
) -> dict[str, Any]:
    result = await application.get_result(task_id, item_id)
    data = result.metric.model_dump(mode="json") if result.metric is not None else None
    return success_response(data=data)


def _execution(state) -> dict[str, Any]:
    return {
        "task": state.task.model_dump(mode="json"),
        "item_id": str(state.item_id),
        "item_status": state.item_status.value,
        "forecast": (
            state.forecast.model_dump(mode="json")
            if state.forecast is not None
            else None
        ),
    }


def _result(result: BacktestResultView) -> dict[str, Any]:
    return result.model_dump(mode="json")


def _models(values) -> list[dict[str, Any]]:
    return [value.model_dump(mode="json") for value in values]
