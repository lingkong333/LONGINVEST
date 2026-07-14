from typing import Annotated, Any

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from long_invest.modules.health.service import (
    ReadinessService,
    get_readiness_service,
)
from long_invest.platform.http.responses import failure_response, success_response

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, Any]:
    return success_response(
        data={"status": "live"},
        message="服务运行正常",
    )


@router.get("/ready")
async def ready(
    service: Annotated[ReadinessService, Depends(get_readiness_service)],
) -> JSONResponse:
    report = await service.check()
    if report.http_status == 503:
        content = failure_response(
            code="SERVICE_NOT_READY",
            message="服务尚未就绪",
            details={"dependencies": report.dependencies},
        )
    else:
        content = success_response(
            data={
                "status": report.status,
                "dependencies": report.dependencies,
            },
            message="服务已就绪" if report.status == "ready" else "服务降级运行",
        )
    return JSONResponse(status_code=report.http_status, content=content)
