from typing import Any

from fastapi import APIRouter

from long_invest.platform.http.responses import success_response

router = APIRouter(prefix="/health", tags=["health"])


@router.get("/live")
async def live() -> dict[str, Any]:
    return success_response(
        data={"status": "live"},
        message="服务运行正常",
    )

