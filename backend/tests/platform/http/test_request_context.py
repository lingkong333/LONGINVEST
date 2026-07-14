import json
from io import StringIO

import pytest
from fastapi import Request
from httpx import ASGITransport, AsyncClient

from long_invest.bootstrap.app import create_app
from long_invest.platform.http.request_context import get_request_context
from long_invest.platform.logging.configure import configure_logging


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.mark.anyio
async def test_request_context_contains_safe_request_metadata() -> None:
    app = create_app()

    @app.get("/_test/context/{item_id}")
    async def context_route(_request: Request, item_id: str) -> dict[str, object]:
        context = get_request_context()
        return {
            "item_id": item_id,
            "client_ip": context.client_ip,
            "route_template": context.route_template,
            "start_time": context.start_time.isoformat(),
            "idempotency_key": context.idempotency_key,
        }

    transport = ASGITransport(app=app, client=("127.0.0.9", 12345))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/_test/context/abc?token=must-not-be-captured",
            headers={"Idempotency-Key": "idem-123"},
        )

    data = response.json()
    assert data["route_template"] == "/_test/context/{item_id}"
    assert data["client_ip"] == "127.0.0.9"
    assert data["idempotency_key"] == "idem-123"
    assert "must-not-be-captured" not in json.dumps(data)


@pytest.mark.anyio
async def test_access_log_omits_query_string_and_sensitive_headers() -> None:
    stream = StringIO()
    configure_logging(level="INFO", stream=stream, use_queue=False)
    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.get(
            "/health/live?token=must-not-be-logged",
            headers={"Cookie": "session=must-not-be-logged"},
        )

    records = [json.loads(line) for line in stream.getvalue().splitlines()]
    access = next(record for record in records if record["event"] == "http_request")
    rendered = json.dumps(access)
    assert access["route_template"] == "/health/live"
    assert access["method"] == "GET"
    assert access["status_code"] == 200
    assert "must-not-be-logged" not in rendered
