from typing import Annotated, Any

from fastapi import APIRouter, Depends, status

from long_invest.modules.client_errors.contracts import (
    ClientErrorInput,
    ClientErrorReceipt,
)
from long_invest.modules.client_errors.service import ClientErrorCollector
from long_invest.platform.http.responses import success_response
from long_invest.platform.http.schemas import SuccessEnvelope

router = APIRouter(prefix="/api/v1/client-errors", tags=["client-errors"])
_collector = ClientErrorCollector()


def get_client_error_collector() -> ClientErrorCollector:
    return _collector


Collector = Annotated[ClientErrorCollector, Depends(get_client_error_collector)]


class ClientErrorEnvelope(SuccessEnvelope):
    data: ClientErrorReceipt


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ClientErrorEnvelope,
)
async def collect_client_error(
    body: ClientErrorInput,
    collector: Collector,
) -> dict[str, Any]:
    receipt = collector.collect(body)
    return success_response(
        data=receipt.model_dump(mode="json"),
        code="CLIENT_ERROR_ACCEPTED",
        message="前端异常已接收",
    )
