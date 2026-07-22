from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any
from uuid import UUID, uuid5

from fastapi import APIRouter, Depends, Header, Query
from pydantic import BaseModel, Field, StrictBool

from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    require_verified_write_request,
)
from long_invest.modules.backtests.application import BacktestApplication
from long_invest.modules.backtests.contracts import (
    BacktestAction,
    BacktestCreateRequest,
    BacktestItemSummaryView,
    BacktestResultView,
    BacktestSummaryView,
    BacktestTaskListItemView,
    BacktestTaskStatus,
)
from long_invest.modules.backtests.service import BacktestCommandContext
from long_invest.platform.errors import AppError
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import Pagination, SuccessEnvelope
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


class BacktestCommandBody(BaseModel):
    confirm: StrictBool
    reason: str = Field(min_length=1, max_length=200)


class BacktestTaskPageData(BaseModel):
    items: list[BacktestTaskListItemView]
    pagination: Pagination


class BacktestTaskPageResponse(SuccessEnvelope):
    data: BacktestTaskPageData


class BacktestSummaryResponse(SuccessEnvelope):
    data: BacktestSummaryView


class BacktestItemPageData(BaseModel):
    items: list[BacktestItemSummaryView]
    pagination: Pagination


class BacktestItemPageResponse(SuccessEnvelope):
    data: BacktestItemPageData


class BacktestControlData(BaseModel):
    task_id: UUID
    status: BacktestTaskStatus
    allowed_actions: list[BacktestAction]


class BacktestControlResponse(SuccessEnvelope):
    data: BacktestControlData


def idempotency_key(
    value: Annotated[
        str,
        Header(alias="Idempotency-Key", min_length=1, max_length=160),
    ],
) -> str:
    value = value.strip()
    if not value or len(value) > 160:
        raise AppError(
            code="IDEMPOTENCY_KEY_REQUIRED",
            message="回测写操作需要有效的幂等键",
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


@router.get("", response_model=BacktestTaskPageResponse)
async def list_backtests(
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    result = await application.list_tasks(page=page, page_size=page_size)
    return success_response(
        data={
            "items": [_model(item) for item in result.items],
            "pagination": {
                "page": result.page,
                "page_size": result.page_size,
                "total": result.total,
            },
        }
    )


@router.get("/{task_id}")
async def get_backtest(
    task_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data=_execution(await application.get_execution(task_id)))


@router.get("/{task_id}/summary", response_model=BacktestSummaryResponse)
async def get_backtest_summary(
    task_id: UUID, application: Application, _identity: ReadIdentity
) -> dict[str, Any]:
    return success_response(data=_model(await application.get_summary(task_id)))


@router.get("/{task_id}/items", response_model=BacktestItemPageResponse)
async def list_backtest_items(
    task_id: UUID,
    application: Application,
    _identity: ReadIdentity,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=200)] = 50,
) -> dict[str, Any]:
    values = await application.list_items(task_id)
    start = (page - 1) * page_size
    return success_response(
        data={
            "items": [_model(item) for item in values[start : start + page_size]],
            "pagination": {"page": page, "page_size": page_size, "total": len(values)},
        }
    )


@router.post(
    "/{task_id}/pause", status_code=202, response_model=BacktestControlResponse
)
async def pause_backtest(
    task_id: UUID,
    body: BacktestCommandBody,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    context = _command_context(body, identity, key, "暂停")
    await application.pause(task_id, context)
    return await _command_response(task_id, application, "回测暂停请求已受理")


@router.post(
    "/{task_id}/resume", status_code=202, response_model=BacktestControlResponse
)
async def resume_backtest(
    task_id: UUID,
    body: BacktestCommandBody,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    context = _command_context(body, identity, key, "继续")
    await application.resume(task_id, context)
    return await _command_response(task_id, application, "回测继续请求已受理")


@router.post(
    "/{task_id}/cancel", status_code=202, response_model=BacktestControlResponse
)
async def cancel_backtest(
    task_id: UUID,
    body: BacktestCommandBody,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    context = _command_context(body, identity, key, "取消")
    await application.cancel(task_id, context)
    return await _command_response(task_id, application, "回测取消请求已受理")


@router.post(
    "/{task_id}/retry-failed",
    status_code=202,
    response_model=BacktestControlResponse,
)
async def retry_failed_backtest(
    task_id: UUID,
    body: BacktestCommandBody,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    context = _command_context(body, identity, key, "重试失败项")
    await application.retry_failed(task_id, context)
    return await _command_response(task_id, application, "失败项重试请求已受理")


@router.post(
    "/{task_id}/rerun", status_code=202, response_model=BacktestControlResponse
)
async def rerun_backtest(
    task_id: UUID,
    body: BacktestCommandBody,
    application: Application,
    identity: WriteIdentity,
    key: IdempotencyKey,
) -> dict[str, Any]:
    context = _command_context(body, identity, key, "重新运行")
    new_task_id = uuid5(_TASK_NAMESPACE, f"rerun:{task_id}:{key}")
    await application.rerun(task_id, new_task_id, context)
    return await _command_response(new_task_id, application, "回测重新运行请求已受理")


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


def _model(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json")


def _command_context(
    body: BacktestCommandBody,
    identity: AuthenticatedRequest,
    key: str,
    action_name: str,
) -> BacktestCommandContext:
    if not body.confirm:
        raise AppError(
            code="AUTH_CONFIRMATION_REQUIRED",
            message=f"请确认本次回测{action_name}操作",
            status_code=422,
        )
    reason = body.reason.strip()
    if not reason:
        raise AppError(
            code="BACKTEST_INPUT_INVALID",
            message="操作原因不能为空",
            status_code=422,
        )
    session = getattr(identity, "session", None)
    audit_context = identity.audit_context
    return BacktestCommandContext(
        request_id=audit_context.request_id,
        idempotency_key=key,
        actor_user_id=str(identity.user.id),
        reason=reason,
        session_id=str(session.id) if session is not None else None,
        trusted_ip=getattr(audit_context, "trusted_ip", None),
    )


async def _command_response(
    task_id: UUID, application: BacktestApplication, message: str
) -> dict[str, Any]:
    summary = await application.get_summary(task_id)
    return success_response(
        data={
            "task_id": str(task_id),
            "status": summary.status.value,
            "allowed_actions": [action.value for action in summary.allowed_actions],
        },
        code="JOB_ACCEPTED",
        message=message,
    )
