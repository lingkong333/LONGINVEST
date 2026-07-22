from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import StreamingResponse

from long_invest.modules.auth.application import AuthApplication, get_auth_application
from long_invest.modules.auth.contracts import RequestActivity
from long_invest.modules.auth.dependencies import (
    AuthenticatedRequest,
    require_authenticated_request,
    session_token,
)
from long_invest.platform.events.application import get_event_stream_service
from long_invest.platform.events.service import EventStreamService

router = APIRouter(prefix="/api/v1/events", tags=["events"])
ReadIdentity = Annotated[AuthenticatedRequest, Depends(require_authenticated_request)]
StreamService = Annotated[EventStreamService, Depends(get_event_stream_service)]
AuthService = Annotated[AuthApplication, Depends(get_auth_application)]


@router.get("/stream", response_class=StreamingResponse)
async def stream_events(
    request: Request,
    authenticated: ReadIdentity,
    stream_service: StreamService,
    auth_service: AuthService,
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    cursor = await stream_service.resolve_cursor(last_event_id)
    token = session_token(request)

    async def validate_session() -> None:
        await auth_service.authenticate(
            session_token=token,
            activity=RequestActivity.BACKGROUND,
            client_ip=authenticated.audit_context.trusted_ip,
            audit_context=authenticated.audit_context,
        )

    return StreamingResponse(
        stream_service.stream(
            cursor=cursor,
            is_disconnected=request.is_disconnected,
            validate_session=validate_session,
        ),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
